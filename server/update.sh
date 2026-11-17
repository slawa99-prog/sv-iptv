#!/usr/bin/env bash
# Refresh only the owned checker and timer; retain results, token and backups.
set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then
  echo 'Run this update from the root console or with sudo.' >&2
  exit 1
fi
revision="${1:-}"
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Pass the exact reviewed 40-character GitHub commit SHA.' >&2
  exit 1
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
systemctl daemon-reload
systemctl enable --now sv-iptv-ru.timer
systemctl start --no-block sv-iptv-ru.service
restart_needed=0
echo "Updated checker. Previous results retained at: $backup"
echo 'The service fetches main before each batch. Credentials and prior observations were not replaced.'
systemctl show sv-iptv-ru.service -p ActiveState -p SubState -p ExecMainStartTimestamp
journalctl -u sv-iptv-ru.service -n 15 --no-pager
