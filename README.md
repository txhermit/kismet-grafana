# kismet-grafana

Live Grafana dashboards for a Kismet wardriving rig. Portable by design:
`git clone`, one setup script, `docker compose up`.

```
Kismet (native on laptop, owns the radio)
   |  REST API :2501  +  eventbus websocket
   v
kismet-exporter (container, :9102)   <- the only custom piece (~400 lines Python)
   |  Prometheus metrics
   v
Prometheus (container, :9090)
   v
Grafana (container, :3000)  <- datasource + "Kismet Live" dashboard provisioned
```

Kismet is deliberately **not** containerized: it needs monitor-mode USB
hardware (Alfa, etc.), and passing that through Docker is pain with no payoff.
The stack only consumes Kismet's HTTP interface, so Kismet can run anywhere
reachable -- same laptop, another box on the LAN, whatever `KISMET_URL` says.

## Quickstart

```bash
git clone <this-repo> && cd kismet-grafana
./up.sh            # resolves your Kismet API key, writes .env, starts everything
```

Then open http://localhost:3000 (admin / `wardrive` unless you changed
`GRAFANA_PASSWORD`). The **Kismet Live** dashboard starts populating within
about 15 seconds. Prometheus is at :9090, raw exporter metrics at
:9102/metrics.

No Kismet handy? Validate the whole stack with synthetic data:

```bash
./up.sh --demo
```

Demo mode fakes ~45 wardriving devices, a moving GPS track around San Antonio
(override with `DEMO_LAT` / `DEMO_LON`), and occasional alerts. Re-run
`./up.sh` without `--demo` when you want the real radio.

## How the exporter works

- **REST polling (primary):** every `POLL_INTERVAL` seconds (default 5) it
  POSTs `devices/views/all/devices.json` with a field-simplified field list,
  plus `gps/location.json` and `system/status.json`. This is the same
  interface the Kismet web UI uses, so it tracks Kismet releases.
- **Eventbus websocket (enhancement):** subscribes to `ALERT` and `GPS` for
  low-latency updates. If the bus is unreachable the exporter keeps working on
  REST alone -- it never crashes the poll loop.
- **Read-only by design.** It never touches the `.kismet` SQLite log (which
  Kismet flushes on its own ~30s schedule anyway) and never sends commands.

Metrics exposed (see `:9102/metrics`):

| metric | what |
|---|---|
| `kismet_up` | 1 if Kismet answered the last poll |
| `kismet_devices{phy,crypt,type}` | tracked devices, aggregated |
| `kismet_new_devices_total` | counter of first-seen devices |
| `kismet_device_signal_dbm{mac,name,phy}` | loudest N devices (`TOP_N`, default 15) |
| `kismet_device_latitude/longitude{mac,name,phy}` | positions of loudest N (for map experiments) |
| `kismet_gps_latitude/longitude/altitude_m/speed_ms/fix` | receiver position |
| `kismet_alerts_total{alert}` | alerts via eventbus |
| `kismet_probe_requests_total{ssid}` | probe requests via the `DOT11_PROBED_SSID` eventbus topic (same feed the Kismet probe-dashboard plugin uses); `(broadcast)` = wildcard probe |

### Probe requests

Clients constantly shout probe requests for networks they remember -- the
exporter counts them per SSID straight off the eventbus, no REST polling
involved. The dashboard shows the top probed SSIDs and the probe rate. On the
first live run the exporter logs the raw event's field names at info level so
you can confirm the SSID field mapping against your Kismet build; anything
unrecognized lands in `(unknown)` rather than being guessed at.

## Kismet API key

The exporter authenticates with HTTP basic auth, API key as the username and a
blank password (the standard Kismet convention for scripts). `up.sh` resolves
the key in this order: `$KISMET_API_KEY` env -> `~/.kismet/kismet_httpd.conf` ->
interactive prompt. It lands in `.env`, which is git-ignored -- never commit
it.

If your Kismet build instead wants the key as the basic-auth *password*, set
`KISMET_APIKEY_AS_PASSWORD=1` in `.env` and restart the exporter.

## Configuration

All in `.env` (see `.env.example`): `KISMET_URL`, `KISMET_API_KEY`,
`EXPORTER_PORT` (9102), `POLL_INTERVAL` (5), `TOP_N` (15), `GRAFANA_PASSWORD`.

Running the exporter outside Docker (e.g. straight on the laptop):

```bash
pip install -r exporter/requirements.txt
KISMET_URL=http://localhost:2501 KISMET_API_KEY=<key> python3 exporter/kismet_exporter.py
```

## Notes / limitations

- **Field use:** Grafana's default basemap needs internet. In the field with no
  uplink, the map tiles won't load (the graphs still work). Pre-caching tiles
  or pointing at a local tile server is future work.
- **Geomap panel:** the live-position marker is wired to the GPS gauges. If it
  doesn't render on your Grafana version, edit the panel: layer Location mode
  -> Coords, latitude `kismet_gps_latitude`, longitude `kismet_gps_longitude`.
- **Per-device map dots:** lat/lon for the loudest devices is exported
  (`kismet_device_latitude/longitude`) but not yet wired into a Geomap layer --
  joining two Prometheus series into one marker layer is fiddly. PRs welcome.
- **Scale:** this is a laptop rig, not a fleet. The all-devices REST poll is
  fine into the tens of thousands of devices; beyond that, raise
  `POLL_INTERVAL`.

## Troubleshooting

- Panels empty, `kismet_up == 0`: `docker logs kismet-exporter`. Usually the
  API key (401) or Kismet not listening where `KISMET_URL` points.
- `host.docker.internal` doesn't resolve: the compose file sets
  `extra_hosts: host.docker.internal:host-gateway`, which covers Docker on
  Linux. If your setup is exotic, set `KISMET_URL` to the host's LAN IP.
- Port conflicts: 3000/9090/9102 are the only host bindings; change them in
  `docker-compose.yml`.

## License

MIT -- do what you want with it.
