#!/usr/bin/env bash
# One-shot Corkboard setup on a Raspberry Pi (Raspberry Pi OS Bookworm or later).
# Run from the project folder as your normal user:  ./scripts/install.sh
# Safe to run again after pulling updates.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
cd "$DIR"

if [[ "$RUN_USER" == "root" ]]; then
  echo "Run this as your normal user (it uses sudo where needed), not as root." >&2
  exit 1
fi

echo "==> System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-dev sqlite3 fonts-dejavu-core libopenjp2-7 libjpeg-dev zlib1g-dev >/dev/null

PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 10)))')
if [[ "$PY_OK" != "1" ]]; then
  echo "Python 3.10 or newer is needed (found $(python3 --version)). Use Raspberry Pi OS Bookworm." >&2
  exit 1
fi

echo "==> Python environment (.venv)"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> Settings (.env)"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "    created .env from .env.example"
fi
set_if_empty() {
  local key="$1" value="$2"
  if grep -qE "^${key}=$" .env; then
    sed -i "s|^${key}=$|${key}=${value}|" .env
    echo "    generated ${key}"
  fi
}
set_if_empty CORKBOARD_SECRET_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
set_if_empty MCP_SECRET "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
if grep -qE '^CORKBOARD_TIMEZONE=America/New_York$' .env && command -v timedatectl >/dev/null; then
  TZ_NAME=$(timedatectl show -p Timezone --value 2>/dev/null || true)
  if [[ -n "$TZ_NAME" ]]; then
    sed -i "s|^CORKBOARD_TIMEZONE=.*|CORKBOARD_TIMEZONE=${TZ_NAME}|" .env
    echo "    timezone set to ${TZ_NAME} (from the system)"
  fi
fi
chmod 600 .env
mkdir -p data

echo "==> Printer permissions"
sudo install -m 644 deploy/99-h22-printer.rules /etc/udev/rules.d/99-h22-printer.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usbmisc --subsystem-match=usb || true
if ! id -nG "$RUN_USER" | grep -qw lp; then
  sudo usermod -aG lp "$RUN_USER"
  echo "    added $RUN_USER to the lp group (log out and back in for your own shell)"
fi

echo "==> Services"
for unit in corkboard-web corkboard-worker; do
  sed -e "s|@USER@|${RUN_USER}|g" -e "s|@DIR@|${DIR}|g" "deploy/${unit}.service" \
    | sudo tee "/etc/systemd/system/${unit}.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable -q corkboard-web corkboard-worker
sudo systemctl restart corkboard-web corkboard-worker

PORT=$(grep -E '^CORKBOARD_PORT=' .env | cut -d= -f2)
PORT=${PORT:-8000}
HOSTNAME_LOCAL="$(hostname).local"
cat <<MSG

Corkboard is running.
  Board:        http://${HOSTNAME_LOCAL}:${PORT}
  Logs:         journalctl -u corkboard-web -u corkboard-worker -f
  Test print:   sudo systemctl stop corkboard-worker && .venv/bin/python scripts/test_print.py; sudo systemctl start corkboard-worker

Next steps (see README.md):
  - Set CORKBOARD_BASE_URL in .env to the address above so card QR codes work, then:
      sudo systemctl restart corkboard-web corkboard-worker
  - GitHub: put a token in GITHUB_TOKEN.
  - Claude: expose /mcp with Tailscale Funnel or Cloudflare Tunnel and add
      https://<public host>/mcp/<MCP_SECRET from .env> as a custom connector.
MSG
