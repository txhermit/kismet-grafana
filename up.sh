#!/usr/bin/env bash
#
# Stand up the Kismet -> Prometheus -> Grafana stack.
#
#   ./up.sh           bring everything up against your live Kismet
#   ./up.sh --demo    no Kismet needed; the exporter fakes a device stream
#
# Kismet itself is NOT containerized on purpose: it needs monitor-mode USB
# hardware, which stays native on the host. This stack only consumes Kismet's
# REST API + eventbus on port 2501.
#
set -euo pipefail
cd "$(dirname "$0")"

DEMO=0
if [[ "${1:-}" == "--demo" ]]; then DEMO=1; fi

command -v docker >/dev/null 2>&1 || { echo "error: docker not found" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "error: 'docker compose' plugin not found" >&2; exit 1; }

[[ -f .env ]] || cp .env.example .env

set_key() { # set_key NAME VALUE  (rewrites the line in .env)
    local name="$1" value="$2"
    if grep -q "^${name}=" .env; then
        sed -i "s|^${name}=.*|${name}=${value}|" .env
    else
        printf '%s=%s\n' "$name" "$value" >> .env
    fi
}

if [[ "$DEMO" == "1" ]]; then
    set_key DEMO_MODE 1
    echo "demo mode: exporter will generate synthetic data (no Kismet needed)"
else
    set_key DEMO_MODE 0
    key="${KISMET_API_KEY:-}"
    if [[ -z "$key" ]]; then
        # Best-effort: look for a stored key in the Kismet config dir.
        for f in "$HOME/.kismet/kismet_httpd.conf" "$HOME/.kismet/kismet.conf"; do
            if [[ -f "$f" ]]; then
                key="$(grep -i -m1 -oP 'api[_-]?key\s*=\s*\K\S+' "$f" 2>/dev/null || true)"
                [[ -n "$key" ]] && { echo "found API key in $f"; break; }
            fi
        done
    fi
    if [[ -z "$key" ]]; then
        echo "Kismet API key not found."
        echo "Create one in the Kismet web UI: Settings (gear) -> API Keys -> New,"
        echo "then paste it here (it is stored in .env, which is git-ignored)."
        read -r -p "Kismet API key: " key
    fi
    [[ -n "$key" ]] || { echo "error: no API key, aborting" >&2; exit 1; }
    set_key KISMET_API_KEY "$key"
    if [[ -n "${KISMET_URL:-}" ]]; then
        set_key KISMET_URL "$KISMET_URL"
    fi
fi

echo "building + starting..."
docker compose up -d --build

echo
echo "Grafana:    http://localhost:3000  (admin / $(grep '^GRAFANA_PASSWORD=' .env | cut -d= -f2))"
echo "Prometheus: http://localhost:9090"
echo "Exporter:   http://localhost:9102/metrics"
echo
if [[ "$DEMO" == "1" ]]; then
    echo "Demo data is flowing. Re-run './up.sh' (without --demo) against live Kismet when ready."
else
    echo "Dashboard 'Kismet Live' should start populating within ~15 seconds."
    echo "If panels stay empty, check the exporter logs:  docker logs kismet-exporter"
fi
