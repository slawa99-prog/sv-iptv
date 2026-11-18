#!/usr/bin/env bash
# Refresh only the owned checker and timer; retain results, token and backups.
set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then
  echo 'Run this update from the root console or with sudo.' >&2
  exit 1
fi
revision="${1:-}"
workers="${2:-}"
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Pass the exact reviewed 40-character GitHub commit SHA.' >&2
  exit 1
fi
if [[ "$#" -gt 2 || ( -n "$workers" && ! "$workers" =~ ^[1-8]$ ) ]]; then
  echo 'Usage: update.sh COMMIT_SHA [WORKERS: 1-8]' >&2
  exit 1
fi
if [[ -n "$workers" && "$workers" -gt 4 ]]; then
  # Confirm that the VPS upgrade is visible inside the guest before stopping it.
  cpu_count="$(nproc)"
  memory_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
  echo "Available resources: $cpu_count CPUs, $memory_kib KiB RAM; requested workers: $workers"
  if [[ "$cpu_count" -lt 4 || "$memory_kib" -lt 6291456 ]]; then
    echo 'For 5-8 workers, first make sure the guest sees at least 4 CPUs and 6 GiB RAM. Current check was not stopped.' >&2
    exit 1
  fi
fi
command -v git >/dev/null
command -v python3 >/dev/null
command -v ffmpeg >/dev/null
systemctl cat sv-iptv-ru.service >/dev/null
state='/var/lib/sv-iptv-ru'
test -d "$state"
id sv-iptv-ru >/dev/null
stage="$(mktemp -d /tmp/sv-iptv-upgrade.XXXXXX)"
restart_needed=0
installed_changed=0
backup=''
owned=(usr/local/lib/sv-iptv-ru/ru_check.py usr/local/lib/sv-iptv-ru/history_parts.py
       etc/systemd/system/sv-iptv-ru.service etc/systemd/system/sv-iptv-ru.timer)
if [[ -n "$workers" ]]; then
  owned+=(etc/systemd/system/sv-iptv-ru.service.d/90-workers.conf)
fi
finish() {
  result=$?
  trap - EXIT
  if [[ "$result" -ne 0 && "$restart_needed" -eq 1 ]]; then
    if [[ "$installed_changed" -eq 1 ]]; then
      for file in "${owned[@]}"; do
        if [[ -f "$backup/installed/$file" ]]; then
          cp -p -- "$backup/installed/$file" "/$file" || true
        elif [[ -f "$backup/absent/$file" ]]; then
          rm -f -- "/$file" || true
        fi
      done
      systemctl daemon-reload || true
    fi
    systemctl start sv-iptv-ru.timer || true
    systemctl start --no-block sv-iptv-ru.service || true
    echo "Update failed; attempted to restore the previous launcher. Backup: $backup" >&2
  fi
  rm -rf -- "$stage"
  exit "$result"
}
trap finish EXIT
git init -q "$stage/source"
git -C "$stage/source" remote add origin https://github.com/slawa99-prog/sv-iptv.git
git -C "$stage/source" fetch -q --depth 1 origin "$revision"
git -C "$stage/source" -c advice.detachedHead=false checkout -q --detach FETCH_HEAD
test "$(git -C "$stage/source" rev-parse HEAD)" = "$revision"
python3 -m py_compile "$stage/source/server/ru_check.py" "$stage/source/server/history_parts.py"
if [[ -n "$workers" ]]; then
  python3 - "$stage/source" "$workers" "$stage/workers.conf" <<'PY'
import pathlib, sys
root, workers, target = pathlib.Path(sys.argv[1]), int(sys.argv[2]), pathlib.Path(sys.argv[3])
sys.path.insert(0, str(root))
from server.ru_check import MAX_WORKERS
assert 1 <= workers <= MAX_WORKERS, 'Downloaded launcher does not support the requested worker count'
target.write_text('[Service]\nExecStart=\n'
    f'ExecStart=/usr/bin/python3 /usr/local/lib/sv-iptv-ru/ru_check.py --workers {workers}\n'
    'CPUAccounting=yes\nMemoryAccounting=yes\nIPAccounting=yes\n'
    'ExecStopPost=-/usr/bin/systemctl show sv-iptv-ru.service -p ExecMainStartTimestamp '
    '-p ExecMainExitTimestamp -p CPUUsageNSec -p MemoryPeak -p IPIngressBytes -p IPEgressBytes --no-pager\n',
    encoding='utf-8')
PY
fi
python3 - "$stage/source" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
c = json.loads((root / 'config.json').read_text())['expansion_campaign']
for spec in [c['frozen_queue']] + c['additional_frozen_queues'] + c.get('frozen_research_archives', []):
    path = (root / spec['path']).resolve()
    assert path.is_relative_to(root.resolve())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == spec['sha256'], spec['path']
print('Verified frozen inputs:', c['frozen_union_request_count'], 'requests')
PY

# Stop the timer first so it cannot race the update. Existing completed history
# and the latest unfinished checkpoint are copied before installing anything.
backup="$state/upgrade-backups/$(date -u +%Y%m%dT%H%M%SZ)-$revision"
install -d -o sv-iptv-ru -g sv-iptv-ru -m 0700 "$backup"
for file in "${owned[@]}"; do
  if [[ -f "/$file" ]]; then
    mkdir -p "$backup/installed/$(dirname "$file")"
    cp -p -- "/$file" "$backup/installed/$file"
  else
    mkdir -p "$backup/absent/$(dirname "$file")"
    touch "$backup/absent/$file"
  fi
done
restart_needed=1
systemctl stop sv-iptv-ru.timer
systemctl stop sv-iptv-ru.service
for file in history.json status.json node.json config.json probe-progress.json environment.json tv.m3u backups.json; do
  if [[ -f "$state/results/$file" ]]; then
    cp -p -- "$state/results/$file" "$backup/$file"
  fi
done
installed_changed=1
install -o root -g root -m 0644 "$stage/source/server/ru_check.py" /usr/local/lib/sv-iptv-ru/ru_check.py
install -o root -g root -m 0644 "$stage/source/server/history_parts.py" /usr/local/lib/sv-iptv-ru/history_parts.py
install -o root -g root -m 0644 "$stage/source/server/sv-iptv-ru.service" /etc/systemd/system/sv-iptv-ru.service
install -o root -g root -m 0644 "$stage/source/server/sv-iptv-ru.timer" /etc/systemd/system/sv-iptv-ru.timer
if [[ -n "$workers" ]]; then
  install -d -o root -g root -m 0755 /etc/systemd/system/sv-iptv-ru.service.d
  install -o root -g root -m 0644 "$stage/workers.conf" /etc/systemd/system/sv-iptv-ru.service.d/90-workers.conf
fi
systemctl daemon-reload
systemctl enable --now sv-iptv-ru.timer
systemctl start --no-block sv-iptv-ru.service
restart_needed=0
echo "Updated checker. Previous results retained at: $backup"
echo 'The service fetches main before each batch. Credentials and prior observations were not replaced.'
systemctl show sv-iptv-ru.service -p ActiveState -p SubState -p ExecMainStartTimestamp
if [[ -n "$workers" ]]; then
  echo "Requested parallel checks: $workers. Confirm the next completed node.json reports this number."
  systemctl show sv-iptv-ru.service -p ExecStart
fi
journalctl -u sv-iptv-ru.service -n 15 --no-pager
