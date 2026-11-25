"""Bounded checks for continuous HTTP MPEG-TS/FLV, without network decoders."""
from __future__ import annotations

import ipaddress
import json
import math
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class UnsupportedMedia(ValueError):
    """An unsupported/ambiguous format is not proof of a dead TV stream."""


class DirectMedia(UnsupportedMedia):
    """The response needs a bounded binary transport probe."""


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


class PublicMediaRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Validate redirected destinations before urllib follows them. FFmpeg only
# receives bounded local files and cannot follow URLs embedded in a manifest.
urlopen = build_opener(PublicMediaRedirect()).open


def transport_kind(data):
    if data.startswith(b'FLV'):
        return 'flv'
    for packet in (188, 192, 204):
        for offset in range(min(packet, max(0, len(data) - 2 * packet))):
            if data[offset] == data[offset + packet] == data[offset + 2 * packet] == 0x47:
                return 'mpegts'
    return None


def capture(item, path, cfg, validate):
    validate(item['url'])
    target = max(12, float(cfg.get('test_seconds', 12)))
    byte_limit = int(cfg.get('sample_max_bytes', 70_000_000))
    headers = {'User-Agent': 'Mozilla/5.0 SV-IPTV/3.0', 'Accept-Encoding': 'identity',
               **item.get('headers', {}), 'Cache-Control': 'no-cache'}
    started, total, prefix, kind = time.monotonic(), 0, b'', None
    # Joining a TS broadcast can land between keyframes. Allow time for the
    # next random-access point and a complete test window after it.
    capture_limit = target + min(8, target / 2)
    with urlopen(Request(item['url'], headers=headers), timeout=cfg.get('network_timeout_seconds', 8)) as response:
        validate(response.url)
        response_headers = getattr(response, 'headers', {})
        if 'Content-Length' in response_headers or 'Content-Range' in response_headers:
            raise UnsupportedMedia('finite media: live status requires review')
        with path.open('wb') as out:
            while time.monotonic() - started < capture_limit:
                chunk = response.read1(65536)
                if not chunk:
                    raise UnsupportedMedia('finite media: live status requires review')
                total += len(chunk)
                if total > byte_limit:
                    raise UnsupportedMedia('continuous sample byte budget reached')
                if kind is None:
                    prefix += chunk
                    kind = transport_kind(prefix)
                    if kind is None and (len(prefix) >= 4096 or prefix.lstrip().startswith((b'<', b'{', b'#'))):
                        raise UnsupportedMedia('format requires a separate adapter or access review')
                out.write(chunk)
    if kind is None:
        raise UnsupportedMedia('unrecognized continuous transport')
    return {'transport': kind, 'bytes': total, 'capture_seconds': round(time.monotonic() - started, 2)}


def playback_start(report, video_id, audio_id, target):
    packets = {video_id: [], audio_id: []}
    for packet in report.get('packets', ()):
        group = packets.get(packet.get('stream_index'))
        if group is None or 'pts_time' not in packet:
            continue
        at = float(packet['pts_time'])
        duration = float(packet.get('duration_time', 0))
        if math.isfinite(at) and math.isfinite(duration) and 0 <= duration <= 1:
            group.append((at, duration, packet.get('flags', '')))
    for group in packets.values():
        group.sort()
    audio = packets[audio_id]
    if not audio:
        raise UnsupportedMedia('continuous audio packets missing; format review required')
    # Choose the first decodable interval from the original transport. Never
    # jump over a corrupt packet inside that interval to obtain a success.
    start = next((at for at, _, flags in packets[video_id] if 'K' in flags and at >= audio[0][0] - .1), None)
    if start is None:
        raise UnsupportedMedia('continuous video keyframe missing; format review required')
    end = start + target
    for group in packets.values():
        covered = start
        for at, duration, flags in group:
            if at + duration < start - .05 or at >= end:
                continue
            if 'C' in flags or at - covered > .25:
                raise UnsupportedMedia('continuous sample contains a gap or damaged packet')
            covered = max(covered, at + duration)
        if covered < end - .1:
            raise UnsupportedMedia('continuous sample ends before the full audio/video window')
    origin = float(report['format']['start_time'])
    if not math.isfinite(origin):
        raise UnsupportedMedia('continuous transport timestamps require review')
    return max(0, start - origin - .025)


def check_direct(item, cfg):
    # Deferred import avoids a module cycle and uses the same validated local
    # decoder/placeholder criteria as HLS. ffmpeg gets local files only.
    import sv_iptv as sv
    with tempfile.TemporaryDirectory(prefix='sv-iptv-direct-') as tmp:
        path = Path(tmp) / 'sample.bin'
        captured = capture(item, path, cfg, sv.validate_url)
        target = max(12, float(cfg.get('test_seconds', 12)))
        safe = ['-protocol_whitelist', 'file']
        source = safe + ['-f', captured['transport']]
        rc, out, _ = sv.run_command(['ffprobe', '-v', 'error'] + source + [
            '-read_intervals', f'%+{target + 10}', '-show_packets', '-show_entries',
            'format=start_time:stream=index,codec_type,width,height:packet=stream_index,pts_time,duration_time,flags',
            '-of', 'json', str(path)], 15)
        if rc:
            raise UnsupportedMedia('continuous sample probe failed')
        report = json.loads(out)
        video = next((s for s in report.get('streams', ()) if s.get('codec_type') == 'video'
                      and s.get('width', 0) > 0 and s.get('height', 0) > 0), None)
        audio = next((s for s in report.get('streams', ()) if s.get('codec_type') == 'audio'), None)
        if not video or not audio:
            raise UnsupportedMedia('continuous media is missing a video or audio track')
        if not report.get('format', {}).get('start_time'):
            raise UnsupportedMedia('continuous transport timestamps require review')
        start = playback_start(report, video['index'], audio['index'], target)
        # Stream-copy the bounded window so pre-join decoder errors and trailing
        # partial packets cannot taint a clean GOP. Decode the result strictly.
        window = path.with_name('window.mkv')
        rc, _, _ = sv.run_command(['ffmpeg', '-nostdin', '-loglevel', 'error'] + source +
            ['-i', str(path), '-ss', str(start), '-t', str(target + .4), '-map', '0:v:0',
             '-map', '0:a:0', '-c', 'copy', '-avoid_negative_ts', 'make_zero', '-f', 'matroska', str(window)],
            cfg.get('decode_timeout_seconds', 30))
        if rc:
            raise UnsupportedMedia('continuous sample could not be isolated for decoding')
        safe += ['-f', 'matroska']
        rc, out, err = sv.run_command(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror',
            '-threads', '1'] + safe + ['-i', str(window), '-t', str(target), '-map', '0:v:0',
            '-map', '0:a:0', '-shortest', '-threads', '1', '-progress', 'pipe:1', '-f', 'null', '-'],
            cfg.get('decode_timeout_seconds', 30))
        seconds = max((int(x) / 1_000_000 for x in re.findall(r'out_time_us=(\d+)', out)), default=0)
        frames = max((int(x) for x in re.findall(r'frame=(\d+)', out)), default=0)
        # A timed capture can end between packets, but it must contain a real
        # continuous decoded interval. Decoder errors remain inconclusive here.
        errors = [s for s in err.splitlines() if s.strip() and 'non monotonically increasing dts' not in s]
        if rc or seconds < target - .15 or frames < target * 5 or errors:
            raise UnsupportedMedia('continuous sample decode incomplete; format review required')
        inspection = sv.inspect_sample(window, cfg, seconds, safe)
        return {'ok': True, 'reason': 'decoded continuous HTTP transport; live identity review required',
                'pixels': video['width'] * video['height'],
                'played_seconds': round(seconds, 2), 'download_seconds': captured['capture_seconds'],
                'elapsed': captured['capture_seconds'], 'media_url': item['url'],
                'advanced': False, 'continuous_transport': captured['transport'],
                'identity_review_required': True, 'live_broadcast_review_required': True,
                'visual_criteria': sv.visual_signature(), 'frame_inspection': inspection,
                'bytes': captured['bytes']}
