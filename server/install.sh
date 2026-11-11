#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo 'Run this installer with sudo or from the root console.' >&2
  exit 1
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for needed in ru_check.py sv-iptv-ru.service sv-iptv-ru.timer; do
  test -f "$SCRIPT_DIR/$needed" || { echo "Missing $needed" >&2; exit 1; }
done
command -v apt-get >/dev/null
command -v systemctl >/dev/null
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 ffmpeg git ca-certificates
id sv-iptv-ru >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/sv-iptv-ru --shell /usr/sbin/nologin sv-iptv-ru
install -d -o sv-iptv-ru -g sv-iptv-ru -m 0700 /var/lib/sv-iptv-ru
install -d -o root -g root -m 0755 /usr/local/lib/sv-iptv-ru
install -d -o root -g root -m 0700 /etc/sv-iptv-ru
if [[ ! -e /etc/sv-iptv-ru/github-token ]]; then
  install -o root -g root -m 0600 /dev/null /etc/sv-iptv-ru/github-token
fi
install -o root -g root -m 0644 "$SCRIPT_DIR/ru_check.py" /usr/local/lib/sv-iptv-ru/ru_check.py
install -o root -g root -m 0644 "$SCRIPT_DIR/sv-iptv-ru.service" /etc/systemd/system/sv-iptv-ru.service
install -o root -g root -m 0644 "$SCRIPT_DIR/sv-iptv-ru.timer" /etc/systemd/system/sv-iptv-ru.timer
systemctl daemon-reload
systemctl enable --now sv-iptv-ru.timer
systemctl start --no-block sv-iptv-ru.service
echo 'Installed. The first check is starting.'
echo 'Logs: sudo journalctl -u sv-iptv-ru.service -f'
echo 'Results: /var/lib/sv-iptv-ru/results/'
echo 'GitHub publication is enabled only after its credential is configured.'
