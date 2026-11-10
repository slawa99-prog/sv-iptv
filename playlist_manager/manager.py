#!/usr/bin/env python3
"""Replace whole IPTV playlists behind stable URLs. Python 3.10+, no dependencies."""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 12 * 1024 * 1024
SLOT_IDS = ("tv1", "tv2", "tv3", "tv4")
LOCAL_SOURCES = {"repo:tv.m3u": "tv.m3u", "repo:cloud.m3u": "cloud.m3u"}
PRESETS = {
    "SV IPTV — проверка ПК": "repo:tv.m3u",
    "SV IPTV — облачная проверка": "repo:cloud.m3u",
    "iptv-org — Россия": "https://iptv-org.github.io/iptv/countries/ru.m3u",
    "ngrch — Россия": "https://ngrch.github.io/iptv/ru.m3u",
    "dearbulut — Россия": "https://dearbulut.github.io/iptv/playlists/country/ru.m3u",
}
STREAM_SCHEMES = {"http", "https", "rtsp", "rtmp", "rtp", "udp"}


class PlaylistError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, default=None):
    if not path.exists() and default is not None:
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def write_json(path, value):
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def check_url(url, resolve=False):
    if not isinstance(url, str) or any(ord(c) < 33 for c in url):
        raise PlaylistError("В ссылке есть пробелы или управляющие символы.")
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError as exc:
        raise PlaylistError("Некорректная ссылка.") from exc
    if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
        raise PlaylistError("Нужна общедоступная HTTP(S)-ссылка без логина и пароля.")
    host = u.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise PlaylistError("Локальные адреса не подходят для общей публикации.")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = []
    if resolve:
        try:
            addresses = [ipaddress.ip_address(x[4][0]) for x in socket.getaddrinfo(
                host, port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)]
        except OSError as exc:
            raise PlaylistError("Не удалось определить адрес сервера источника.") from exc
    if any(not a.is_global for a in addresses):
        raise PlaylistError("Локальные и служебные адреса источников запрещены.")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, resolve=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(source, root):
    if source in LOCAL_SOURCES:
        p = root / LOCAL_SOURCES[source]
        if p.stat().st_size > MAX_BYTES:
            raise PlaylistError("Плейлист слишком большой.")
        return p.read_bytes(), None
    check_url(source, resolve=True)
    req = urllib.request.Request(source, headers={
        "User-Agent": "SV-IPTV-Playlist-Manager/1.0",
        "Accept": "application/x-mpegURL, audio/x-mpegurl, text/plain, */*",
        "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.build_opener(SafeRedirect()).open(req, timeout=20) as response:
            check_url(response.geturl(), resolve=True)
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise PlaylistError("Источник вернул веб-страницу вместо M3U.")
            chunks, size, started = [], 0, time.monotonic()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise PlaylistError("Плейлист превышает 12 МиБ.")
                if time.monotonic() - started > 60:
                    raise PlaylistError("Превышено время загрузки плейлиста.")
                chunks.append(chunk)
            return b"".join(chunks), response.geturl()
    except urllib.error.HTTPError as exc:
        raise PlaylistError(f"Источник вернул HTTP {exc.code}; прежний список сохранён.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PlaylistError("Не удалось загрузить источник; прежний список сохранён.") from None


def absolute_url(value, base):
    if not value:
        return value
    address, sep, options = value.partition("|")
    parsed = urllib.parse.urlsplit(address)
    if not parsed.scheme:
        if base is None:
            raise PlaylistError("В локальном плейлисте найдена относительная ссылка.")
        address = urllib.parse.urljoin(base, address)
        parsed = urllib.parse.urlsplit(address)
    if parsed.scheme.lower() not in STREAM_SCHEMES or not parsed.netloc:
        raise PlaylistError("В плейлисте есть неподдерживаемая или локальная ссылка.")
    return address + sep + options


def normalise_attributes(line, base):
    # Preserve headers, channel IDs, groups, names and playback options.
    def replace(match):
        key, val = match.group(1), match.group(2)
        if not val:
            return match.group(0)
        values = val.split(",") if key.lower() in {"url-tvg", "x-tvg-url", "tvg-url"} else [val]
        return key + '="' + ",".join(absolute_url(v.strip(), base) for v in values) + '"'
    return re.sub(r'\b(tvg-logo|url-tvg|x-tvg-url|tvg-url)="([^"]*)"', replace, line, flags=re.I)


def parse_playlist(data, base=None, title="SV ТВ"):
    if len(data) > MAX_BYTES:
        raise PlaylistError("Плейлист превышает 12 МиБ.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251")
    if "\x00" in text:
        raise PlaylistError("Вместо M3U получен двоичный файл.")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not re.match(r"^#EXTM3U(?:\s|$)", lines[0]):
        raise PlaylistError("Файл должен начинаться с #EXTM3U.")
    if any(line.startswith("#EXT-X-") for line in lines):
        raise PlaylistError("Это поток одного канала HLS, а не список IPTV-каналов.")
    if any(c in title for c in "\r\n\x00"):
        raise PlaylistError("Некорректное название плейлиста.")
    result = [normalise_attributes(lines[0], base), "#PLAYLIST:" + title]
    pending, count = False, 0
    for line in lines[1:]:
        if line.startswith("#PLAYLIST:"):
            continue
        if line.startswith("#EXTINF:"):
            if pending or "," not in line:
                raise PlaylistError("Повреждённая запись канала EXTINF.")
            pending = True
            result.append(normalise_attributes(line, base))
        elif line.startswith("#"):
            # In particular keep EXTVLCOPT, EXTHTTP and KODIPROP verbatim.
            result.append(line)
        else:
            if not pending:
                raise PlaylistError("Ссылка без названия канала EXTINF.")
            result.append(absolute_url(line, base))
            pending = False
            count += 1
    if pending or count == 0:
        raise PlaylistError("Плейлист пустой или последняя запись не завершена.")
    return "\n".join(result) + "\n", count


class Manager:
    def __init__(self, root, fetcher=fetch):
        self.root = Path(root)
        self.folder = self.root / "managed"
        self.config_path = self.folder / "config.json"
        self.state_path = self.folder / "status.json"
        self.config = read_json(self.config_path)
        if self.config.get("version") != 1 or set(self.config["slots"]) != set(SLOT_IDS):
            raise PlaylistError("Некорректная конфигурация заменяемых плейлистов.")
        self.state = read_json(self.state_path, {"version": 1, "slots": {}})
        self.fetcher = fetcher

    def output(self, slot):
        if slot not in SLOT_IDS:
            raise PlaylistError("Выберите ТВ 1, ТВ 2, ТВ 3 или ТВ 4.")
        return self.folder / (slot + ".m3u")

    def prepare(self, source, slot):
        if source not in LOCAL_SOURCES:
            check_url(source)
            # A slot must not fetch itself or another managed slot in this repository.
            if "/slawa99-prog/sv-iptv/" in source and "/managed/" in source:
                raise PlaylistError("Нужен исходный плейлист автора, а не ссылка на заменяемый список.")
        data, base = self.fetcher(source, self.root)
        return parse_playlist(data, base, self.config["slots"][slot]["title"])

    def publish(self, slot, source, text, count):
        target = self.output(slot)
        previous = self.state["slots"].get(slot, {})
        changed = not target.exists() or target.read_text(encoding="utf-8") != text
        source_changed = source != self.config["slots"][slot].get("source")
        if target.exists() and (changed or source_changed):
            write_json(self.folder / "previous" / (slot + ".json"), {
                "slot": slot,
                "source": self.config["slots"][slot].get("source"),
                "auto_refresh": self.config["slots"][slot].get("auto_refresh", True),
                "playlist": target.read_text(encoding="utf-8"),
                "saved_at": now(),
            })
        if changed:
            write(target, text)
        self.config["slots"][slot]["source"] = source
        self.state["slots"][slot] = {
            "source": source, "entries": count,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "checked_at": now(), "updated_at": now() if changed else previous.get("updated_at", now()),
            "result": "updated" if changed else "unchanged",
            "validation": "M3U structure only; playback of every channel is not verified",
        }

    def save(self):
        write_json(self.config_path, self.config)
        write_json(self.state_path, self.state)
        self.write_catalog()

    def refresh(self, slot="all"):
        selected = SLOT_IDS if slot == "all" else (slot,)
        failures = []
        for key in selected:
            self.output(key)
            cfg = self.config["slots"][key]
            if not cfg.get("source") or not cfg.get("auto_refresh", True):
                continue
            old = self.state["slots"].get(key, {})
            try:
                text, count = self.prepare(cfg["source"], key)
                previous_count = old.get("entries", 0)
                if self.output(key).exists():
                    _, previous_count = parse_playlist(self.output(key).read_bytes())
                if previous_count and count < previous_count * self.config.get("minimum_retained_fraction", 0.5):
                    raise PlaylistError("Число записей упало более чем вдвое; прежний список сохранён.")
                self.publish(key, cfg["source"], text, count)
            except (PlaylistError, OSError, UnicodeError, ValueError) as exc:
                self.state["slots"][key] = dict(old, checked_at=now(), result="error",
                    error=str(exc) if isinstance(exc, PlaylistError) else "Ошибка обработки источника.")
                failures.append(key)
        self.save()
        return failures

    def replace(self, slot, source):
        self.output(slot)
        # Nothing, including the selected source, changes before successful validation.
        text, count = self.prepare(source, slot)
        self.publish(slot, source, text, count)
        self.config["slots"][slot]["auto_refresh"] = True
        self.save()

    def rollback(self, slot):
        self.output(slot)
        backup = self.folder / "previous" / (slot + ".json")
        if not backup.exists():
            raise PlaylistError("Для этого списка ещё нет предыдущей версии.")
        old = read_json(backup)
        if old.get("slot") != slot:
            raise PlaylistError("Резервная версия относится к другому плейлисту.")
        text, count = parse_playlist(old["playlist"].encode(), title=self.config["slots"][slot]["title"])
        self.publish(slot, old["source"], text, count)
        # Freeze after rollback so a broken upstream update cannot undo it next hour.
        self.config["slots"][slot]["auto_refresh"] = False
        self.state["slots"][slot]["result"] = "rolled_back_paused"
        self.save()

    def resume(self, slot):
        self.output(slot)
        cfg = self.config["slots"][slot]
        if not cfg.get("source"):
            raise PlaylistError("Сначала выберите источник.")
        text, count = self.prepare(cfg["source"], slot)
        old_count = self.state["slots"].get(slot, {}).get("entries", 0)
        if old_count and count < old_count * self.config.get("minimum_retained_fraction", 0.5):
            raise PlaylistError("Число записей резко уменьшилось. Проверьте источник или выполните явную замену.")
        self.publish(slot, cfg["source"], text, count)
        cfg["auto_refresh"] = True
        self.save()

    def write_catalog(self):
        base = self.config["public_base_url"].rstrip("/")
        lines = ["# SV ТВ — постоянные ссылки", "",
            "[Заменить плейлист](https://github.com/slawa99-prog/sv-iptv/actions/workflows/playlists.yml) · [Инструкция](../PLAYLISTS.md)", "",
            "В Televizo добавляйте ссылки из таблицы как M3U по URL. Название позиции остаётся прежним при смене источника.", "",
            "| Название | Ссылка для Televizo | Записей | Последняя загрузка UTC | Состояние |",
            "|---|---|---:|---|---|" ]
        labels = {"updated": "Обновлён", "unchanged": "Без изменений", "error": "Ошибка; сохранена предыдущая версия", "rolled_back_paused": "Восстановлен; автообновление на паузе"}
        for key in SLOT_IDS:
            cfg = self.config["slots"][key]
            s = self.state["slots"].get(key, {})
            url = f"{base}/{key}.m3u"
            link = f"[{key}.m3u]({url})" if self.output(key).exists() else "Ещё не опубликован"
            label = labels.get(s.get("result"), "Источник ещё не загружен")
            if not cfg.get("auto_refresh", True) and s.get("result") != "rolled_back_paused":
                label += "; автообновление на паузе"
            lines.append(f"| {cfg['title']} | {link} | {s.get('entries', '—')} | {s.get('checked_at', '—')} | {label} |")
        lines += ["", "Количество — записи в файле автора, не количество подтверждённых рабочих или уникальных каналов. Проверяется загрузка и структура M3U; полный эфир каждого канала здесь не проверяется.", "",
            "При ошибке первой загрузки ссылка не публикуется. При последующих ошибках сохраняется предыдущий файл. Обновление у зрителя зависит от Televizo и его настроек; смена текущего эфира посреди просмотра не гарантируется.", ""]
        write(self.folder / "README.md", "\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--action", choices=["refresh", "replace", "rollback", "resume"], default="refresh")
    parser.add_argument("--slot", choices=["all", *SLOT_IDS], default="all")
    parser.add_argument("--source", default="")
    args = parser.parse_args()
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        args.action = {"Заменить источник": "replace", "Обновить сейчас": "refresh",
            "Вернуть предыдущую версию": "rollback", "Возобновить автообновление": "resume"}.get(os.environ.get("INPUT_OPERATION"), "")
        args.slot = os.environ.get("INPUT_SLOT", "").split(" ")[0]
        custom = os.environ.get("INPUT_SOURCE", "").strip()
        preset = os.environ.get("INPUT_PRESET", "")
        args.source = custom or PRESETS.get(preset, "")
    try:
        manager = Manager(args.root)
        if args.action not in {"refresh", "replace", "rollback", "resume"}:
            raise PlaylistError("Неизвестная операция.")
        if args.action != "refresh" and args.slot == "all":
            raise PlaylistError("Для замены или восстановления выберите одну позицию ТВ.")
        if args.action == "replace":
            if not args.source:
                raise PlaylistError("Укажите ссылку автора или выберите готовый источник.")
            manager.replace(args.slot, args.source)
        elif args.action == "rollback":
            manager.rollback(args.slot)
        elif args.action == "resume":
            manager.resume(args.slot)
        else:
            failed = manager.refresh(args.slot)
            if failed:
                raise PlaylistError("Не удалось обновить: " + ", ".join(failed) + ". Подробности в managed/status.json.")
        print("Готово. Постоянные ссылки сохранены.")
        return 0
    except (PlaylistError, OSError, ValueError, KeyError) as exc:
        print(str(exc) if isinstance(exc, PlaylistError) else "Ошибка конфигурации или локального файла.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
