#!/usr/bin/env python3
"""SV IPTV 3: unique channels, bounded live HLS checks, persistent observations.

Python 3.10+ and ffmpeg/ffprobe; no third-party Python packages.
Media is downloaded through Python's normal system proxy settings, decoded
locally and deleted. No video is hosted or retained by this project.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import gzip
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ATTR = re.compile(r'([\w-]+)=(?:"([^"]*)"|\'([^\']*)\')')
QUALITY = re.compile(r'^(?:SD|HD|FHD|UHD|4K|8K|\d{3,4}[pi])$', re.I)
HOST_LOCKS: dict[str, threading.Semaphore] = {}
LOCK = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                     dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        f.write(value)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def clean(value: str) -> str:
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(value)).replace('"', "'").strip()


def header_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return re.sub(r'[\x00-\x1f\x7f]', '', value)


def name_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r'\s*\((?:\d{3,4}[pi]|sd|hd|fhd|uhd|4k)\)\s*$', '', value)
    value = re.sub(r'\s+(?:sd|hd|fhd|uhd|4k)\s*$', '', value)
    return re.sub(r'[^\w]', '', value)


def base_id(tvg_id: str) -> str:
    """Only collapse picture-quality feeds. A time shift is not a backup."""
    channel, sep, feed = tvg_id.strip().partition("@")
    return (channel if not sep or QUALITY.fullmatch(feed) else tvg_id).casefold()


def split_info(line: str) -> tuple[str, str]:
    quote = None
    for pos, ch in enumerate(line):
        if ch in ('"', "'"):
            if quote == ch:
                quote = None
            elif quote is None:
                quote = ch
        elif ch == "," and quote is None:
            return line[:pos], line[pos + 1:]
    return line, "Канал"


def validate_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("unsupported URL")
    host = p.hostname.casefold()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("local endpoint")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if not ip.is_global:
        raise ValueError("non-public endpoint")


def parse_m3u(text: str) -> list[dict]:
    if not text.lstrip("\ufeff \t\r\n").startswith("#EXTM3U"):
        raise ValueError("invalid M3U header")
    streams, info, extras = [], None, []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            info, extras = line, []
        elif line.startswith(("#EXTVLCOPT:", "#KODIPROP:")) and info:
            extras.append(line)
        elif not line or line.startswith("#"):
            continue
        elif info:
            try:
                validate_url(line)
            except ValueError:
                info = None
                continue
            prefix, name = split_info(info)
            attrs = {k: a or b for k, a, b in ATTR.findall(prefix)}
            headers = {}
            mapping = {"http-user-agent": "User-Agent", "http-referrer": "Referer"}
            for key, target in mapping.items():
                if attrs.get(key):
                    headers[target] = clean(attrs[key])
                for extra in extras:
                    if extra.startswith(f"#EXTVLCOPT:{key}="):
                        headers[target] = header_value(extra.split("=", 1)[1])
            unsupported = any("license" in x.casefold() or "drm" in x.casefold()
                              for x in extras if x.startswith("#KODIPROP:"))
            streams.append({"url": line, "attrs": attrs, "name": name.strip(),
                            "headers": headers, "unsupported": unsupported})
            info, extras = None, []
    return streams


def stream_key(item: dict) -> str:
    payload = json.dumps([item["url"], item.get("headers", {})], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def fetch_bytes(url: str, headers=None, timeout=12, limit=4_000_000) -> bytes:
    validate_url(url)
    request_headers = {"User-Agent": "Mozilla/5.0 SV-IPTV/3.0", "Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    request_headers["Cache-Control"] = "no-cache"
    started = time.monotonic()
    with urlopen(Request(url, headers=request_headers), timeout=timeout) as response:
        validate_url(response.url)
        chunks, size = [], 0
        while size <= limit:
            if time.monotonic() - started > timeout:
                raise TimeoutError("total response deadline")
            chunk = response.read1(min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b''.join(chunks)
    if len(data) > limit:
        raise ValueError("response size limit")
    if data.startswith(b"\x1f\x8b"):
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise ValueError("uncompressed size limit")
    return data


def manifest(url: str, headers: dict, cfg: dict) -> str:
    data = fetch_bytes(url, headers, cfg.get("network_timeout_seconds", 10), 2_000_000)
    text = data.decode("utf-8-sig")
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError("not an HLS manifest")
    return text


def hls_attrs(line: str) -> dict:
    return {m.group(1): m.group(2) if m.group(2) is not None else m.group(3)
            for m in re.finditer(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', line)}


def choose_media(url: str, headers: dict, cfg: dict) -> tuple[str, str, str | None]:
    selected_audio = None
    for _ in range(5):
        text = manifest(url, headers, cfg)
        if "#EXT-X-STREAM-INF:" not in text:
            return url, text, selected_audio
        pending, variants = None, []
        external_audio = {}
        for line in text.splitlines():
            if line.startswith("#EXT-X-MEDIA:"):
                attrs = hls_attrs(line)
                if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
                    group = attrs.get("GROUP-ID")
                    rank = (attrs.get("LANGUAGE", "").casefold() in ("ru", "rus"), attrs.get("DEFAULT") == "YES")
                    if group not in external_audio or rank > external_audio[group][0]:
                        external_audio[group] = (rank, urljoin(url, attrs["URI"]))
        for line in text.splitlines():
            if line.startswith("#EXT-X-STREAM-INF:"):
                pending = hls_attrs(line)
            elif pending and line and not line.startswith("#"):
                resolution = pending.get("RESOLUTION", "0x0").split("x")
                height = int(resolution[-1]) if resolution[-1].isdigit() else 0
                bitrate = int(pending.get("AVERAGE-BANDWIDTH", pending.get("BANDWIDTH", 0)))
                audio = external_audio.get(pending.get("AUDIO"))
                variants.append((height, bitrate, urljoin(url, line.strip()), audio[1] if audio else None))
                pending = None
        if not variants:
            raise ValueError("no playable HLS rendition")
        cap_h, cap_b = cfg.get("preferred_max_height", 720), cfg.get("preferred_max_bitrate", 4_000_000)
        suitable = [v for v in variants if v[0] <= cap_h and v[1] <= cap_b]
        chosen = max(suitable, key=lambda v: (v[0], v[1])) if suitable else min(variants, key=lambda v: v[1])
        url = chosen[2]
        selected_audio = chosen[3] or selected_audio
    raise ValueError("HLS nesting limit")


def media_segments(text: str, url: str) -> dict:
    if "#EXT-X-ENDLIST" in text:
        raise ValueError("ended stream / VOD")
    seq, target, duration, init, segments, discontinuity = 0, 6.0, None, None, [], False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY:") and hls_attrs(line).get("METHOD") != "NONE":
            raise ValueError("encrypted stream: excluded")
        if line.startswith("#EXT-X-BYTERANGE"):
            raise ValueError("byte-range HLS: unsupported")
        if line.startswith("#EXT-X-MAP:"):
            attrs = hls_attrs(line)
            if "BYTERANGE" in attrs:
                raise ValueError("byte-range initialization: unsupported")
            init = urljoin(url, attrs["URI"])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"):
            discontinuity = True
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",")[0])
        elif line and not line.startswith("#") and duration is not None:
            segments.append({"url": urljoin(url, line), "seconds": duration,
                             "sequence": seq + len(segments), "init": init, "discontinuity": discontinuity})
            duration, discontinuity = None, False
    if not segments:
        raise ValueError("no media segments")
    return {"target": target, "segments": segments}


def run_command(args: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                                encoding="utf-8", errors="replace")
        return result.returncode, result.stdout, result.stderr[-1200:]
    except subprocess.TimeoutExpired:
        return 124, "", "decode timeout"


def decode_sample(segments: list[dict], headers: dict, cfg: dict, audio_segments: list[dict] | None = None) -> dict:
    """Download complete segments, then verify both tracks and actual decoded frames."""
    with tempfile.TemporaryDirectory(prefix="sv-iptv-") as tmp:
        folder = Path(tmp)
        init_files, total_bytes, download_started = {}, 0, time.monotonic()
        paths = []
        groups = [segments] + ([audio_segments] if audio_segments is not None else [])
        for track, parts in enumerate(groups):
            lines = ["#EXTM3U", "#EXT-X-VERSION:7", f'#EXT-X-TARGETDURATION:{math.ceil(max(x["seconds"] for x in parts))}',
                     "#EXT-X-MEDIA-SEQUENCE:0"]
            for pos, seg in enumerate(parts):
                if seg.get("init"):
                    if seg["init"] not in init_files:
                        init_name = f"init{len(init_files)}.mp4"
                        (folder / init_name).write_bytes(fetch_bytes(seg["init"], headers,
                            cfg.get("network_timeout_seconds", 10), 4_000_000))
                        init_files[seg["init"]] = init_name
                    lines.append(f'#EXT-X-MAP:URI="{init_files[seg["init"]]}"')
                data = fetch_bytes(seg["url"], headers, cfg.get("network_timeout_seconds", 10),
                                   cfg.get("segment_max_bytes", 20_000_000))
                total_bytes += len(data)
                if total_bytes > cfg.get("sample_max_bytes", 70_000_000):
                    raise ValueError("sample byte limit")
                if len(data) < 188:
                    raise ValueError("empty media segment")
                name = f"seg{track}_{pos}." + ("m4s" if seg.get("init") else "ts")
                (folder / name).write_bytes(data)
                if seg.get("discontinuity"):
                    lines.append("#EXT-X-DISCONTINUITY")
                lines += [f'#EXTINF:{seg["seconds"]},', name]
            lines.append("#EXT-X-ENDLIST")
            path = folder / f"sample{track}.m3u8"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            paths.append(path)
        download_seconds = time.monotonic() - download_started
        safe_input = ["-protocol_whitelist", "file,crypto,data", "-allowed_extensions", "ALL"]
        tracks = []
        for path in paths:
            rc, out, err = run_command(["ffprobe", "-v", "error"] + safe_input + [
                "-show_entries", "stream=codec_type,width,height", "-of", "json", str(path)], 15)
            if rc:
                raise ValueError("sample probe failed")
            tracks.append(json.loads(out).get("streams", []))
        videos = [x for x in tracks[0] if x.get("codec_type") == "video"]
        audios = [x for x in tracks[-1] if x.get("codec_type") == "audio"]
        if not videos or not audios:
            raise ValueError("missing video or audio")
        inputs = [arg for path in paths for arg in safe_input + ["-i", str(path)]]
        rc, out, err = run_command(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
            "-threads", "1"] + inputs + ["-map", "0:v:0", "-map", f"{len(paths)-1}:a:0",
            "-threads", "1", "-shortest", "-progress", "pipe:1", "-f", "null", "-"], cfg.get("decode_timeout_seconds", 30))
        times = re.findall(r"^out_time_us=(\d+)$", out, re.M)
        frames = re.findall(r"^frame=(\d+)$", out, re.M)
        seconds = max((int(x) for x in times), default=0) / 1_000_000
        expected = min(sum(x["seconds"] for x in parts) for parts in groups)
        if rc or seconds < expected * .85 or max((int(x) for x in frames), default=0) < expected * 5:
            raise ValueError("incomplete video/audio decode")
        # The null output muxer can complain about duplicate DTS while decoded
        # frames/audio are complete. This is not a decoder corruption error.
        errors = [line for line in err.splitlines() if line.strip() and
                  'non monotonically increasing dts to muxer' not in line]
        if errors:
            summary = re.sub(r'\[[^\]]+\]', '', errors[-1]).strip()
            raise ValueError("decode errors: " + summary[:110])
        if download_seconds > max(8, expected * cfg.get("max_download_duration_ratio", 1.0)):
            raise ValueError("media download slower than playback")
        return {"pixels": max(int(x.get("width", 0)) * int(x.get("height", 0)) for x in videos),
                "played_seconds": round(seconds, 2), "download_seconds": round(download_seconds, 2),
                "bytes": total_bytes}


def check_stream(item: dict, cfg: dict) -> dict:
    host = urlparse(item["url"]).hostname
    with LOCK:
        semaphore = HOST_LOCKS.setdefault(host, threading.Semaphore(cfg.get("per_host_workers", 2)))
    with semaphore:
        started = time.monotonic()
        try:
            media_url, text, audio_url = choose_media(item["url"], item.get("headers", {}), cfg)
            first = media_segments(text, media_url)
            first_audio = media_segments(manifest(audio_url, item.get("headers", {}), cfg), audio_url) if audio_url else None
            chosen, seconds = [], 0
            for seg in reversed(first["segments"]):
                chosen.insert(0, seg)
                seconds += seg["seconds"]
                if seconds >= cfg.get("test_seconds", 12):
                    break
            if seconds < 1 or len(chosen) > 12:
                raise ValueError("insufficient live sample")
            audio_chosen = None
            if first_audio:
                audio_chosen, audio_seconds = [], 0
                for seg in reversed(first_audio["segments"]):
                    audio_chosen.insert(0, seg)
                    audio_seconds += seg["seconds"]
                    if audio_seconds >= cfg.get("test_seconds", 12):
                        break
                if audio_seconds < 1 or len(audio_chosen) > 12:
                    raise ValueError("insufficient audio sample")
            sample = decode_sample(chosen, item.get("headers", {}), cfg, audio_chosen)
            played, last_read = sample["played_seconds"], started
            # Short live windows are common for low-latency channels. Accumulate
            # real decoded media across updates rather than accepting a short test.
            for _ in range(6):
                target = max(first["target"], first_audio["target"] if first_audio else 0)
                wait = min(32, target * 1.15 + 1) - (time.monotonic() - last_read)
                if wait > 0:
                    time.sleep(wait)
                second = media_segments(manifest(media_url, item.get("headers", {}), cfg), media_url)
                last_read = time.monotonic()
                old_urls = {x["url"] for x in first["segments"]}
                fresh = [s for s in second["segments"] if s["url"] not in old_urls
                         and s["sequence"] > first["segments"][-1]["sequence"]]
                if not fresh:
                    raise ValueError("live manifest did not advance")
                fresh_audio = None
                if first_audio:
                    second_audio = media_segments(manifest(audio_url, item.get("headers", {}), cfg), audio_url)
                    old_audio_urls = {s["url"] for s in first_audio["segments"]}
                    fresh_audio = [s for s in second_audio["segments"] if s["url"] not in old_audio_urls
                                   and s["sequence"] > first_audio["segments"][-1]["sequence"]]
                    if not fresh_audio:
                        raise ValueError("audio manifest did not advance")
                later = decode_sample(fresh[-1:], item.get("headers", {}), cfg, fresh_audio[-1:] if fresh_audio else None)
                played += later["played_seconds"]
                if played >= cfg.get("test_seconds", 12):
                    break
                first = second
                if first_audio:
                    first_audio = second_audio
            else:
                raise ValueError("insufficient live sample across updates")
            elapsed = time.monotonic() - started
            return {"ok": True, "reason": "decoded live samples; manifest advancing",
                    "pixels": sample["pixels"], "played_seconds": round(played, 2),
                    "elapsed": round(elapsed, 2), "download_seconds": sample["download_seconds"],
                    "media_url": media_url, "audio_url": audio_url, "advanced": True}
        except HTTPError as exc:
            reason = f"HTTP {exc.code}"
        except (TimeoutError, URLError):
            reason = "network timeout or unavailable"
        except Exception as exc:
            reason = str(exc)[:160]
        return {"ok": False, "reason": reason, "elapsed": round(time.monotonic() - started, 2), "pixels": 0}


def load_sources(cfg: dict) -> tuple[list[dict], list[dict]]:
    records, reports = [], []
    for source in cfg["sources"]:
        url = source["url"] if isinstance(source, dict) else source
        seed = source.get("fallback") if isinstance(source, dict) else None
        try:
            if url.startswith(("https://", "http://")):
                data = fetch_bytes(url, timeout=25, limit=5_000_000).decode("utf-8-sig")
            else:
                path = (ROOT / url).resolve()
                if not path.is_relative_to(ROOT.resolve()):
                    raise ValueError("source outside project")
                data = path.read_text(encoding="utf-8-sig")
            if isinstance(source, dict) and source.get("format") == "iptv-org-streams":
                allowed = {c["id"] for c in read_json(ROOT / "channels.json", [])}
                lines = ["#EXTM3U"]
                for row in json.loads(data):
                    if row.get("channel") not in allowed:
                        continue
                    identity = row["channel"] + ("@" + row["feed"] if row.get("feed") else "")
                    labels = ''.join(' [' + clean(v) + ']' for v in row.get("labels", []))
                    lines.append(f'#EXTINF:-1 tvg-id="{clean(identity)}",{clean(row["title"])}{labels}')
                    if row.get("user_agent"):
                        lines.append('#EXTVLCOPT:http-user-agent=' + header_value(row["user_agent"]))
                    if row.get("referrer"):
                        lines.append('#EXTVLCOPT:http-referrer=' + header_value(row["referrer"]))
                    lines.append(row["url"])
                data = '\n'.join(lines) + '\n'
            items = parse_m3u(data)
            state = "fresh"
        except Exception as exc:
            items, state = [], "failed"
            if seed:
                snapshot = read_json(ROOT / seed, {})
                age = (time.time() - timestamp(snapshot["fetched_utc"])) / 3600 if snapshot else 1e6
                if age <= cfg.get("source_fallback_hours", 72):
                    items = parse_m3u(snapshot["m3u"])
                    state = "snapshot"
            reports.append({"source": url, "state": state, "records": len(items), "error": str(exc)[:120]})
        else:
            reports.append({"source": url, "state": state, "records": len(items)})
            if seed and items:
                write_json(ROOT / seed, {"source": url, "fetched_utc": utcnow(), "m3u": data})
        for item in items:
            if isinstance(source, dict):
                mapped_id = source.get("id_map", {}).get(item["attrs"].get("tvg-id", ""))
                if mapped_id:
                    item["attrs"]["tvg-id"] = mapped_id
            item["match_by_name"] = isinstance(source, dict) and source.get("match_by_name", False)
            item["source_priority"] = source.get("priority", 20) if isinstance(source, dict) else 0
            item["source"] = url
        excluded_urls = source.get("excluded_urls", []) if isinstance(source, dict) else []
        records.extend(item for item in items if item["url"] not in excluded_urls)
    return records, reports


def group_candidates(records: list[dict], catalog: list[dict], cfg: dict) -> tuple[dict, dict]:
    found = {c["id"].casefold(): [] for c in catalog}
    by_name = {}
    for channel in catalog:
        for name in [channel["name"]] + channel.get("aliases", []):
            key = name_key(name)
            if key in by_name and by_name[key] != channel["id"].casefold():
                by_name[key] = None  # An ambiguous name must never silently merge channels.
            else:
                by_name[key] = channel["id"].casefold()
    excluded = Counter()
    tagged_geo = {i["url"] for i in records if "[geo-blocked]" in i["name"].casefold()}
    tagged_part_time = {i["url"] for i in records if "[not 24/7]" in i["name"].casefold()}
    seen = set()
    for item in records:
        if item.get("unsupported"):
            excluded["drm_or_unsupported"] += 1
            continue
        if cfg.get("skip_tagged_geo_blocked", True) and item["url"] in tagged_geo:
            excluded["geo_blocked"] += 1
            continue
        if cfg.get("skip_tagged_not_24_7", True) and item["url"] in tagged_part_time:
            excluded["not_24_7"] += 1
            continue
        identity = item["attrs"].get("tvg-id", "")
        channel_id = base_id(identity) if identity else by_name.get(name_key(item["name"]))
        if channel_id not in found and item.get("match_by_name") and "@" not in identity:
            channel_id = by_name.get(name_key(item["name"]))
        if channel_id not in found:
            excluded["outside_catalog_or_nonmain_feed"] += 1
            continue
        key = (channel_id, stream_key(item))
        if key in seen:
            excluded["duplicate_candidate"] += 1
            continue
        seen.add(key)
        found[channel_id].append(item)
    return found, dict(excluded)


def observation(old: dict, result: dict, now: str, cfg: dict) -> dict:
    checks = list(old.get("checks", []))
    # v2 boolean histories have no observation times; do not inflate v3 confidence.
    checks = [c for c in checks if isinstance(c, dict) and "at" in c]
    cutoff = timestamp(now) - cfg.get("history_days", 14) * 86400
    checks = [c for c in checks if timestamp(c["at"]) >= cutoff]
    entry = {"at": now, "ok": bool(result["ok"])}
    if checks and timestamp(now) - timestamp(checks[-1]["at"]) < cfg.get("sample_spacing_hours", 2) * 3600:
        # Repeating a command within minutes does not create days of reliability.
        entry["at"] = checks[-1]["at"]
        # A quick retry must not erase a failure already seen in this interval.
        entry["ok"] = bool(checks[-1]["ok"] and entry["ok"])
        checks[-1] = entry
    else:
        checks.append(entry)
    checks = checks[-cfg.get("history_size", 56):]
    rate = sum(c["ok"] for c in checks) / len(checks)
    span = (timestamp(now) - timestamp(checks[0]["at"])) / 3600
    stable = (result["ok"] and len(checks) >= cfg.get("stable_min_samples", 8)
              and span >= cfg.get("stable_min_hours", 42)
              and rate >= cfg.get("minimum_success_rate", .9))
    return {"checks": checks, "last_checked": now, "last_result": result,
            "samples": len(checks), "success_rate": round(rate, 4),
            "observation_hours": round(span, 2), "stable": bool(stable)}


def select_candidates(items: list[dict], old: dict, previous: str | None, cfg: dict, now: str) -> list[dict]:
    limit = cfg.get("max_variants_per_channel", 4)
    def rank(item):
        h = old.get(stream_key(item), {})
        healthy = h.get("last_result", {}).get("ok", False)
        return (stream_key(item) == previous, healthy, h.get("success_rate", 0),
                -item.get("source_priority", 20),
                item["url"].startswith("https://"))
    ordered = sorted(items, key=rank, reverse=True)
    if len(ordered) <= limit:
        return ordered
    # Reserve discovery capacity even when all old candidates used to be healthy.
    keep = ordered[:max(1, limit - 1)]
    rotation = ordered[len(keep):]
    rotation.sort(key=lambda i: (old.get(stream_key(i), {}).get("last_checked", ""), stream_key(i)))
    return keep + rotation[:limit - len(keep)]


def render_m3u(pairs: list[tuple[dict, dict]], cfg: dict, *, unverified=False) -> str:
    header = '#EXTM3U x-playlist-name="SV IPTV"'
    if cfg.get("epg_url"):
        header += f' x-tvg-url="{clean(cfg["epg_url"])}"'
    lines = [header]
    if unverified:
        lines.append("# UNVERIFIED CANDIDATES: not a tested or stable playlist")
    for channel, item in pairs:
        attrs = f'tvg-id="{clean(channel["id"])}" tvg-name="{clean(channel["name"])}" group-title="{clean(channel["group"])}"'
        if channel.get("logo"):
            attrs += f' tvg-logo="{clean(channel["logo"])}"'
        for key, value in item.get("headers", {}).items():
            attrs += f' {"http-user-agent" if key == "User-Agent" else "http-referrer"}="{clean(value)}"'
        lines.append(f'#EXTINF:-1 {attrs},{clean(channel["name"])}')
        for key, value in item.get("headers", {}).items():
            lines.append(f'#EXTVLCOPT:{"http-user-agent" if key == "User-Agent" else "http-referrer"}={clean(value)}')
        lines.append(item["url"])
    return "\r\n".join(lines) + "\r\n"


def build(cfg: dict, *, tester=check_stream, now: str | None = None, inventory=False) -> dict:
    now = now or utcnow()
    catalog = read_json(ROOT / "channels.json", [])
    ids = [c["id"].casefold() for c in catalog]
    names = [name_key(c["name"]) for c in catalog]
    if not catalog or len(set(ids)) != len(ids) or len(set(names)) != len(names):
        raise ValueError("empty catalog or duplicate channel IDs/names")
    records, source_reports = load_sources(cfg)
    found, excluded = group_candidates(records, catalog, cfg)
    if inventory:
        pairs, used = [], set()
        for channel in catalog:
            item = next((i for i in found[channel["id"].casefold()] if i["url"] not in used), None)
            if item:
                pairs.append((channel, item))
                used.add(item["url"])
        atomic_write(ROOT / "candidates-unverified.m3u", render_m3u(pairs, cfg, unverified=True))
        report = {"generated_utc": now, "unverified_channels": len(pairs), "candidate_streams": sum(map(len, found.values())),
                  "source_reports": source_reports, "excluded": excluded}
        write_json(ROOT / "inventory.json", report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return report
    if tester is check_stream and (not shutil.which("ffmpeg") or not shutil.which("ffprobe")):
        raise RuntimeError("Install ffmpeg and ffprobe before checking streams")
    historic = read_json(ROOT / "history.json", {"streams": {}, "last_selected": {}})
    old, previous = historic.get("streams", {}), historic.get("last_selected", {})
    chosen = {cid: select_candidates(items, old, previous.get(cid), cfg, now) for cid, items in found.items()}
    all_items = {stream_key(i): i for items in chosen.values() for i in items}
    print(f'Testing {len(all_items)} streams for {sum(bool(v) for v in chosen.values())} channels', flush=True)
    results = {}
    signature = hashlib.sha256(json.dumps({"checker": "3.0.2", **{k: cfg.get(k) for k in
        ("test_seconds", "preferred_max_height", "preferred_max_bitrate", "network_timeout_seconds",
         "max_download_duration_ratio", "decode_timeout_seconds")}}, sort_keys=True).encode()).hexdigest()
    if tester is check_stream and cfg.get("resume_checks"):
        checkpoint = read_json(ROOT / "probe-progress.json", {})
        if checkpoint.get("signature") == signature:
            for key, result in checkpoint.get("results", {}).items():
                if result.get("reason") in ("insufficient live sample", "separate audio rendition: not supported by this checker"):
                    continue  # Compatible extension: retry formats older checks could not test.
                if result.get("ok") and result.get("played_seconds", 0) < cfg.get("test_seconds", 12):
                    continue
                if key in all_items and result.get("checked_utc") and time.time() - timestamp(result["checked_utc"]) < 3600:
                    results[key] = result
            print(f'Resumed {len(results)} recent probe results', flush=True)
    by_host = {}
    for key, item in all_items.items():
        if key not in results:
            by_host.setdefault(urlparse(item["url"]).hostname, []).append((key, item))
    queue = []
    while any(by_host.values()):
        for bucket in by_host.values():
            if bucket:
                queue.append(bucket.pop(0))
    with ThreadPoolExecutor(max_workers=max(1, min(cfg.get("workers", 8), 16))) as pool:
        futures = {pool.submit(tester, item, cfg): key for key, item in queue}
        for f in as_completed(futures):
            key = futures[f]
            try:
                results[key] = f.result()
            except Exception as exc:
                results[key] = {"ok": False, "reason": str(exc)[:120], "pixels": 0}
            results[key]["checked_utc"] = utcnow() if tester is check_stream else now
            if tester is check_stream and (len(results) % 5 == 0 or len(results) == len(all_items)):
                write_json(ROOT / "probe-progress.json", {"signature": signature, "started_utc": now, "results": results})
            if len(results) % 25 == 0 or len(results) == len(all_items):
                print(f'Checked {len(results)}/{len(all_items)}; passed {sum(bool(r["ok"]) for r in results.values())}', flush=True)
    updated = {k: v for k, v in old.items() if k in all_items or
               (v.get("last_checked") and timestamp(now) - timestamp(v["last_checked"]) < cfg.get("history_days", 14) * 86400)}
    for key, result in results.items():
        updated[key] = observation(old.get(key, {}), result, result.get("checked_utc", now), cfg)
    pairs, selected, unavailable, backups, used_urls, used_media = [], [], [], {}, set(), set()
    next_previous = {}
    for channel in catalog:
        cid = channel["id"].casefold()
        eligible = []
        for item in chosen[cid]:
            key = stream_key(item)
            h, result = updated[key], results[key]
            if not result["ok"]:
                continue
            if h["samples"] >= cfg.get("minimum_samples_for_rating", 4) and h["success_rate"] < cfg.get("minimum_success_rate", .9):
                continue
            eligible.append(item)
        def ranking(i):
            h, r = updated[stream_key(i)], results[stream_key(i)]
            return (h["stable"], h["success_rate"], stream_key(i) == previous.get(cid),
                    -r.get("download_seconds", 100), min(r.get("pixels", 0), 1280 * 720))
        eligible.sort(key=ranking, reverse=True)
        winner = next((i for i in eligible if i["url"] not in used_urls and
                       results[stream_key(i)].get("media_url", i["url"]) not in used_media), None)
        if winner is None:
            unavailable.append({"id": channel["id"], "channel": channel["name"],
                                "reason": "no eligible unique stream", "candidates": len(chosen[cid])})
            continue
        key, h = stream_key(winner), updated[stream_key(winner)]
        # Keep the stable entry URL. A CDN rendition discovered inside a master
        # can carry an expiring signature; the player should refresh it itself.
        playable = winner
        used_urls.add(winner["url"])
        used_media.add(results[key].get("media_url", winner["url"]))
        pairs.append((channel, playable))
        next_previous[cid] = key
        selected.append({"id": channel["id"], "channel": channel["name"], "group": channel["group"],
                         "url": playable["url"], "source_url": winner["url"], "ok_last_check": True,
                         "rating": "observed_stable" if h["stable"] else "probation",
                         "success_rate": h["success_rate"], "samples": h["samples"],
                         "observation_hours": h["observation_hours"], "test": results[key],
                         "alternatives_passed": len(eligible) - 1})
        backups[channel["id"]] = [{"url": i["url"],
                                    "headers": i.get("headers", {}), "success_rate": updated[stream_key(i)]["success_rate"]}
                                   for i in eligible if stream_key(i) != key]
    old_status = read_json(ROOT / "status.json", {})
    prior_count = old_status.get("last_published_count", old_status.get("channels_published", 0))
    minimum = cfg.get("minimum_channels", 100)
    guard = None
    if len(pairs) < minimum:
        guard = f"only {len(pairs)} passed unique channels; minimum is {minimum}"
    elif prior_count and len(pairs) < prior_count * cfg.get("minimum_retained_fraction", .75):
        guard = "large simultaneous loss; previous playlist retained"
    fresh_count = sum(r["state"] == "fresh" and r["source"].startswith("http") for r in source_reports)
    if not fresh_count and cfg.get("require_fresh_remote_source", True):
        guard = "all remote catalogs unavailable; previous playlist retained"
    finished = utcnow() if tester is check_stream else now
    report = {"version": "3.0", "started_utc": now, "checked_utc": finished,
              "publication": "blocked" if guard else "published",
              "publication_reason": guard, "minimum_channels": minimum,
              "candidate_streams_tested": len(results), "streams_passed": sum(bool(r["ok"]) for r in results.values()),
              "unique_channels_passed": len(pairs), "stable_channels": sum(x["rating"] == "observed_stable" for x in selected),
              "probation_channels": sum(x["rating"] == "probation" for x in selected),
              "last_published_count": prior_count if guard else len(pairs),
              "last_published_utc": old_status.get("last_published_utc") if guard else finished,
              "source_reports": source_reports, "excluded": excluded,
              "failure_reasons": dict(Counter(r["reason"] for r in results.values() if not r["ok"])),
              "selected": selected, "unavailable": unavailable}
    # Always save failed observations. Otherwise a bad day would disappear from ratings.
    write_json(ROOT / "history.json", {"version": 3, "streams": updated,
               "last_selected": previous if guard else next_previous})
    if not guard:
        atomic_write(ROOT / "tv.m3u", render_m3u(pairs, cfg))
        write_json(ROOT / "backups.json", {"checked_utc": finished, "channels": backups})
    write_json(ROOT / "status.json", report)
    print(f'{report["publication"].upper()}: {len(pairs)} unique channels; {report["stable_channels"]} with accumulated history', flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description="SV IPTV: check live streams and publish unique channels")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--inventory", action="store_true", help="export explicitly unverified candidates; no media tests")
    parser.add_argument("--resume", action="store_true", help="reuse matching probe results less than one hour old")
    args = parser.parse_args()
    try:
        cfg = read_json(ROOT / args.config, {})
        cfg["resume_checks"] = args.resume
        report = build(cfg, inventory=args.inventory)
        if report.get("publication") == "blocked":
            raise SystemExit(2)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
