#!/usr/bin/env python3
"""
kismet-exporter: Kismet live data -> Prometheus metrics.

Polls Kismet's REST API (the same interface the Kismet web UI itself uses) for
device snapshots, GPS position, and server status, and exposes the results for
Prometheus to scrape.

A background eventbus websocket thread additionally subscribes to the ALERT and
GPS topics for low-latency updates. If the eventbus is unreachable, the
exporter keeps working on REST polling alone -- the eventbus is an
enhancement, not a dependency.

    python3 kismet_exporter.py            # live mode, needs KISMET_API_KEY
    python3 kismet_exporter.py --demo     # synthetic data, no Kismet needed

Environment:
    KISMET_URL                  base URL of Kismet server
                                default: http://host.docker.internal:2501
    KISMET_API_KEY              Kismet API key (required in live mode).
                                Create one in the Kismet web UI under
                                Settings -> API Keys.
    KISMET_APIKEY_AS_PASSWORD   set to 1 if your Kismet build expects the API
                                key as the HTTP basic-auth password instead of
                                the username (default: key as username, blank
                                password).
    EXPORTER_PORT               default: 9102
    POLL_INTERVAL               seconds between REST polls, default: 5
    TOP_N                       how many of the loudest devices get individual
                                signal/location series, default: 15
"""

import base64
import json
import logging
import math
import os
import random
import sys
import threading
import time

import requests
from requests.auth import HTTPBasicAuth
from prometheus_client import Counter, Gauge, Info, start_http_server

try:
    import websocket  # websocket-client
    HAS_WS = True
except ImportError:  # pragma: no cover
    HAS_WS = False

LOG = logging.getLogger("kismet-exporter")

KISMET_URL = os.environ.get("KISMET_URL", "http://host.docker.internal:2501").rstrip("/")
API_KEY = os.environ.get("KISMET_API_KEY", "")
KEY_AS_PASSWORD = os.environ.get("KISMET_APIKEY_AS_PASSWORD", "") == "1"
PORT = int(os.environ.get("EXPORTER_PORT", "9102"))
POLL = float(os.environ.get("POLL_INTERVAL", "5"))
TOP_N = int(os.environ.get("TOP_N", "15"))
DEMO = "--demo" in sys.argv or os.environ.get("DEMO_MODE") == "1"

# Field-simplified device record, mirroring the field list used by the
# Kismet web UI plugins (nested signal path aliased to "last_signal",
# geopoint is [lon, lat]).
FIELDS = [
    "kismet.device.base.key",
    "kismet.device.base.macaddr",
    "kismet.device.base.commonname",
    "kismet.device.base.phyname",
    "kismet.device.base.type",
    "kismet.device.base.crypt",
    "kismet.device.base.first_time",
    "kismet.device.base.last_time",
    ["kismet.device.base.signal/kismet.common.signal.last_signal", "last_signal"],
    "kismet.device.base.location",
]

# ---------------------------------------------------------------- metrics ---

UP = Gauge("kismet_up", "1 if the last Kismet poll succeeded, else 0")
DEVICES = Gauge("kismet_devices", "Devices currently tracked by Kismet",
                ["phy", "crypt", "type"])
NEW_DEVICES = Counter("kismet_new_devices_total",
                      "Devices first seen since the exporter started")
SIGNAL = Gauge("kismet_device_signal_dbm",
               "Last signal (dBm) of the loudest devices",
               ["mac", "name", "phy"])
DEV_LAT = Gauge("kismet_device_latitude", "Approx latitude of loudest devices",
                ["mac", "name", "phy"])
DEV_LON = Gauge("kismet_device_longitude", "Approx longitude of loudest devices",
                ["mac", "name", "phy"])
GPS_LAT = Gauge("kismet_gps_latitude", "GPS latitude in decimal degrees")
GPS_LON = Gauge("kismet_gps_longitude", "GPS longitude in decimal degrees")
GPS_ALT = Gauge("kismet_gps_altitude_m", "GPS altitude in meters")
GPS_SPEED = Gauge("kismet_gps_speed_ms", "GPS speed in meters per second")
GPS_FIX = Gauge("kismet_gps_fix", "GPS fix type (0=none, 2=2D, 3=3D)")
ALERTS = Counter("kismet_alerts_total", "Kismet alerts seen", ["alert"])
KINFO = Info("kismet", "Kismet server info")

# ------------------------------------------------------------ live source ---


class KismetSource:
    """Reads live state from Kismet's REST API."""

    def __init__(self, base_url, api_key, key_as_password=False):
        self.base = base_url
        self.sess = requests.Session()
        if key_as_password:
            self.sess.auth = HTTPBasicAuth("kismet", api_key)
            self._basic = base64.b64encode(
                ("kismet:%s" % api_key).encode()).decode()
        else:
            self.sess.auth = HTTPBasicAuth(api_key, "")
            self._basic = base64.b64encode(
                ("%s:" % api_key).encode()).decode()

    def devices(self):
        """All currently tracked devices, field-simplified."""
        r = self.sess.post(
            self.base + "/devices/views/all/devices.json",
            data={"json": json.dumps({"fields": FIELDS})},
            timeout=15,
        )
        r.raise_for_status()
        devs = r.json()
        return devs if isinstance(devs, list) else []

    def gps(self):
        r = self.sess.get(self.base + "/gps/location.json", timeout=10)
        r.raise_for_status()
        return r.json()

    def status(self):
        r = self.sess.get(self.base + "/system/status.json", timeout=10)
        r.raise_for_status()
        return r.json()

    def basic_auth_header(self):
        return {"Authorization": "Basic " + self._basic}


# ------------------------------------------------------------ demo source ---

DEMO_PHYS = (["IEEE802.11"] * 70 + ["Bluetooth"] * 15
             + ["BTLE"] * 12 + ["Z-Wave"] * 3)
DEMO_CRYPT = (["WPA2"] * 45 + ["WPA3"] * 10 + ["WPA2"] * 5 + ["WEP"] * 5
              + ["None"] * 25 + ["WPA2 Enterprise"] * 10)
DEMO_OUIS = ["8C:3B:AD", "DC:A6:32", "B8:27:EB", "F0:9F:C2",
             "18:65:90", "A4:CF:12", "00:25:DF", "D8:A0:1D"]
DEMO_SSIDS = ["HOME-5G", "xfinitywifi", "ATT Fiber", "CoffeeShop",
              "NETGEAR-24", "Linksys", "Tesla", None, None, "Hotel WiFi"]
DEMO_ALERTS = ["DEAUTH_FLOOD", "PROBE_REQUEST_FLOOD", "BSSTIMESTAMP_SKEW",
               "CHANCHANGE", "DISASSOC_FLOOD"]


class DemoSource:
    """Synthetic Kismet-like data so the stack can be validated with no radio."""

    def __init__(self):
        self.lat0 = float(os.environ.get("DEMO_LAT", "29.4241"))
        self.lon0 = float(os.environ.get("DEMO_LON", "-98.4936"))
        self.devs = []
        self._t0 = time.time()
        for _ in range(45):
            self._add_device(initial=True)

    def _add_device(self, initial=False):
        now = time.time()
        self.devs.append({
            "mac": "%s:%02X:%02X:%02X" % (
                random.choice(DEMO_OUIS), random.randrange(256),
                random.randrange(256), random.randrange(256)),
            "name": random.choice(DEMO_SSIDS),
            "phy": random.choice(DEMO_PHYS),
            "crypt": random.choice(DEMO_CRYPT),
            "type": "Wi-Fi AP" if random.random() < 0.4 else "Wi-Fi Client",
            "signal": random.uniform(-85, -35),
            "lat": self.lat0 + random.uniform(-0.03, 0.03),
            "lon": self.lon0 + random.uniform(-0.03, 0.03),
            "first": now if not initial else now - random.uniform(0, 3600),
        })

    def devices(self):
        for d in self.devs:
            d["signal"] = max(-95, min(-25,
                                      d["signal"] + random.uniform(-4, 4)))
            d["lat"] += random.uniform(-0.0004, 0.0004)
            d["lon"] += random.uniform(-0.0004, 0.0004)
        # churn: occasionally a new device appears, occasionally one vanishes
        if random.random() < 0.25:
            self._add_device()
        if len(self.devs) > 60 and random.random() < 0.2:
            self.devs.pop(random.randrange(len(self.devs)))
        if random.random() < 0.06:
            ALERTS.labels(alert=random.choice(DEMO_ALERTS)).inc()
        return [{
            "kismet.device.base.macaddr": d["mac"],
            "kismet.device.base.commonname": d["name"] or "",
            "kismet.device.base.phyname": d["phy"],
            "kismet.device.base.type": d["type"],
            "kismet.device.base.crypt": d["crypt"],
            "kismet.device.base.first_time": int(d["first"]),
            "kismet.device.base.last_time": int(time.time()),
            "last_signal": round(d["signal"], 1),
            "kismet.device.base.location": {
                "kismet.common.location.avg_loc": {
                    "kismet.common.location.geopoint": [d["lon"], d["lat"]],
                },
            },
        } for d in self.devs]

    def gps(self):
        # slow driving circle around the center point
        t = (time.time() - self._t0) / 90.0
        return {
            "kismet.common.location.geopoint": [
                self.lon0 + 0.02 * 1.6 * math.cos(t),
                self.lat0 + 0.02 * math.sin(t),
            ],
            "kismet.common.location.fix": 3,
            "kismet.common.location.speed": 14.0,
            "kismet.common.location.alt": 198.0,
        }

    def status(self):
        return {"kismet.system.version": "demo"}


# --------------------------------------------------------------- helpers ---

def device_position(rec):
    """Return (lat, lon) from a device record, or (None, None)."""
    try:
        loc = rec.get("kismet.device.base.location") or {}
        avg = loc.get("kismet.common.location.avg_loc") or {}
        gp = avg.get("kismet.common.location.geopoint") or []
        if len(gp) >= 2 and gp[0] and gp[1]:
            return float(gp[1]), float(gp[0])  # geopoint is [lon, lat]
    except (TypeError, ValueError):
        pass
    return None, None


_prev_agg = set()
_prev_top = set()


def update_devices(devs, poll_start):
    counts = {}
    scored = []
    for d in devs:
        phy = d.get("kismet.device.base.phyname") or "unknown"
        crypt = d.get("kismet.device.base.crypt") or "None"
        typ = d.get("kismet.device.base.type") or "unknown"
        key = (phy, crypt, typ)
        counts[key] = counts.get(key, 0) + 1

        try:
            if int(d.get("kismet.device.base.first_time") or 0) >= poll_start:
                NEW_DEVICES.inc()
        except (TypeError, ValueError):
            pass

        sig = d.get("last_signal")
        if isinstance(sig, (int, float)):
            scored.append((sig, d))

    for labels in _prev_agg - set(counts):
        DEVICES.remove(*labels)
    _prev_agg.clear()
    for (phy, crypt, typ), n in counts.items():
        DEVICES.labels(phy=phy, crypt=crypt, type=typ).set(n)
        _prev_agg.add((phy, crypt, typ))

    scored.sort(key=lambda s: s[0], reverse=True)
    top = scored[:TOP_N]
    seen = set()
    for sig, d in top:
        mac = d.get("kismet.device.base.macaddr") or "unknown"
        name = d.get("kismet.device.base.commonname") or "(unnamed)"
        phy = d.get("kismet.device.base.phyname") or "unknown"
        labels = (mac, name[:48], phy)
        SIGNAL.labels(mac=labels[0], name=labels[1], phy=labels[2]).set(sig)
        lat, lon = device_position(d)
        if lat is not None:
            DEV_LAT.labels(mac=labels[0], name=labels[1], phy=labels[2]).set(lat)
            DEV_LON.labels(mac=labels[0], name=labels[1], phy=labels[2]).set(lon)
        seen.add(labels)
    for labels in _prev_top - seen:
        SIGNAL.remove(*labels)
        DEV_LAT.remove(*labels)
        DEV_LON.remove(*labels)
    _prev_top.clear()
    _prev_top.update(seen)


def update_gps(g):
    if not isinstance(g, dict):
        return
    gp = g.get("kismet.common.location.geopoint") or []
    if len(gp) >= 2 and gp[0] and gp[1]:
        GPS_LON.set(float(gp[0]))
        GPS_LAT.set(float(gp[1]))
    for key, gauge, cast in (
            ("kismet.common.location.alt", GPS_ALT, float),
            ("kismet.common.location.speed", GPS_SPEED, float),
            ("kismet.common.location.fix", GPS_FIX, int)):
        if g.get(key) is not None:
            try:
                gauge.set(cast(g[key]))
            except (TypeError, ValueError):
                pass


def update_status(s):
    if not isinstance(s, dict):
        return
    ver = s.get("kismet.system.version")
    if ver:
        KINFO.info({"version": str(ver)})


def handle_eventbus_message(msg):
    """Best-effort handling of one eventbus frame. Never raises."""
    try:
        if not isinstance(msg, dict):
            return
        topic = msg.get("event") or msg.get("topic") or ""
        data = msg.get("data", msg)
        if not isinstance(data, dict):
            return
        if topic == "ALERT":
            name = (data.get("kismet.alert.definition")
                    or data.get("kismet.alert.text")
                    or data.get("text") or "unknown")
            ALERTS.labels(alert=str(name)[:48]).inc()
        elif topic == "GPS":
            update_gps(data)
    except Exception as exc:  # never let the bus thread die loudly
        LOG.debug("eventbus message skipped: %s", exc)


def eventbus_loop(source):
    """Subscribe to ALERT + GPS topics. Runs forever; REST polling covers us
    if this thread can never connect."""
    if DEMO or not HAS_WS or not isinstance(source, KismetSource):
        return
    ws_url = (KISMET_URL.replace("https://", "wss://")
                       .replace("http://", "ws://")
              + "/eventbus/events.ws")
    headers = source.basic_auth_header()
    LOG.info("eventbus: connecting to %s", ws_url)
    while True:
        try:
            ws = websocket.create_connection(ws_url, header=headers,
                                             timeout=15)
            for topic in ("ALERT", "GPS"):
                ws.send(json.dumps({"SUBSCRIBE": topic}))
            LOG.info("eventbus: subscribed to ALERT, GPS")
            while True:
                handle_eventbus_message(json.loads(ws.recv()))
        except Exception as exc:
            LOG.warning("eventbus disconnected (%s); retrying in 5s", exc)
            time.sleep(5)


# ------------------------------------------------------------------ main ---

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if DEMO:
        LOG.info("demo mode: generating synthetic Kismet data")
        source = DemoSource()
    else:
        if not API_KEY:
            LOG.error("KISMET_API_KEY is not set (required in live mode). "
                      "Create one in the Kismet web UI under Settings -> API Keys, "
                      "or run with --demo.")
            sys.exit(2)
        source = KismetSource(KISMET_URL, API_KEY, KEY_AS_PASSWORD)
        LOG.info("polling Kismet at %s every %ss", KISMET_URL, POLL)

    start_http_server(PORT)
    LOG.info("exporter listening on :%d", PORT)

    threading.Thread(target=eventbus_loop, args=(source,),
                     daemon=True, name="eventbus").start()

    last_poll = time.time() - max(120, POLL * 3)
    while True:
        poll_start = time.time()
        try:
            update_devices(source.devices(), last_poll)
            UP.set(1)
        except Exception as exc:
            UP.set(0)
            LOG.warning("device poll failed: %s", exc)
        try:
            update_gps(source.gps())
        except Exception as exc:
            LOG.debug("gps poll failed: %s", exc)
        try:
            update_status(source.status())
        except Exception as exc:
            LOG.debug("status poll failed: %s", exc)
        last_poll = poll_start
        time.sleep(POLL)


if __name__ == "__main__":
    main()
