"""Bounded checks for continuous HTTP MPEG-TS/FLV, without network decoders."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import time
from urllib.request import Request, urlopen


class UnsupportedMedia(ValueError):
    """An unsupported/ambiguous format is not proof of a dead TV stream."""


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
    with urlopen(Request(item['url'], headers=headers), timeout=cfg.get('network_timeout_seconds', 8)) as response:
        validate(response.url)
        with path.open('wb') as out:
            while time.monotonic() - started < target:
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


def check_direct(item, cfg):
    # Deferred import avoids a module cycle and uses the same validated local
    # decoder/placeholder criteria as HLS. ffmpeg gets local files only.
    import sv_iptv as sv
    with tempfile.TemporaryDirectory(prefix='sv-iptv-direct-') as tmp:
        path = Path(tmp) / 'sample.bin'
        captured = capture(item, path, cfg, sv.validate_url)
        safe = ['-protocol_whitelist', 'file']
        rc, out, _ = sv.run_command(['ffprobe', '-v', 'error'] + safe +
            ['-show_entries', 'stream=codec_type,width,height', '-of', 'json', str(path)], 15)
        if rc:
            raise ValueError('continuous sample probe failed')
        streams = json.loads(out).get('streams', [])
        videos = [s for s in streams if s.get('codec_type') == 'video'
                  and s.get('width', 0) > 0 and s.get('height', 0) > 0]
        if not videos or not any(s.get('codec_type') == 'audio' for s in streams):
            raise UnsupportedMedia('continuous media is missing a video or audio track')
        target = max(12, float(cfg.get('test_seconds', 12)))
        rc, out, err = sv.run_command(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-threads', '1'] + safe + ['-i', str(path), '-t', str(target), '-map', '0:v:0',
            '-map', '0:a:0', '-threads', '1', '-progress', 'pipe:1', '-f', 'null', '-'],
            cfg.get('decode_timeout_seconds', 30))
        seconds = max((int(x) / 1_000_000 for x in re.findall(r'out_time_us=(\d+)', out)), default=0)
        frames = max((int(x) for x in re.findall(r'frame=(\d+)', out)), default=0)
        # A timed capture can end between packets, but it must contain a real
        # continuous decoded interval. Decoder errors remain inconclusive here.
        errors = [s for s in err.splitlines() if s.strip() and 'non monotonically increasing dts' not in s]
        if rc or seconds < target * .85 or frames < target * 5 or errors:
            raise UnsupportedMedia('continuous sample decode incomplete; format review required')
        inspection = sv.inspect_sample(path, cfg, seconds, safe)
        return {'ok': True, 'reason': 'decoded continuous HTTP transport; live identity review required',
                'pixels': max(int(s.get('width', 0)) * int(s.get('height', 0)) for s in videos),
                'played_seconds': round(seconds, 2), 'download_seconds': captured['capture_seconds'],
                'elapsed': captured['capture_seconds'], 'media_url': item['url'],
                'advanced': False, 'continuous_transport': captured['transport'],
                'identity_review_required': True, 'live_broadcast_review_required': True,
                'visual_criteria': sv.visual_signature(), 'frame_inspection': inspection,
                'bytes': captured['bytes']}
