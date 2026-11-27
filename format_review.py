"""Bounded evidence for responses held in the Russian frozen TV queue."""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re
from urllib.parse import urldefrag, urljoin, urlsplit

from response_diagnostics import PREFIX_BYTES, after_id3, audio_frame

AUDIO_FORMATS = {'mp3': 'mp3', 'aac_adts': 'aac'}
MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_PAGE_BYTES = 128 * 1024
MAX_PAGE_SOURCES = 8
VOID_ELEMENTS = frozenset('area base br col embed hr img input link meta param source track wbr'.split())


def decoded_audio_window(path, kind, target, run, timeout):
    """Decode consecutive complete frames and inventory the bounded sample's tracks."""
    data = path.read_bytes()
    if data.startswith(b'ID3'):
        data = after_id3(data)
    # A continuous radio response can start or end in the middle of a frame.
    for match in re.finditer(b'\xff(?=[\xe0-\xff])', data[:PREFIX_BYTES]):
        start = pos = match.start()
        frame = audio_frame(data, pos)
        if not frame or frame[0][0] != kind:
            continue
        profile = frame[0]
        frames = 0
        while frame and frame[0] == profile and pos + frame[1] <= len(data):
            pos += frame[1]
            frames += 1
            frame = audio_frame(data, pos)
        if frames >= 3:
            # Unexplained bytes between frames can belong to a multiplex.
            if len(data) - pos >= 9 and (not frame or frame[0] != profile):
                return None
            break
    else:
        return None
    path.write_bytes(data[start:pos])
    rc, out, err = run(['ffprobe', '-v', 'error', '-err_detect', 'explode', '-threads', '1',
                        '-protocol_whitelist', 'file', '-format_whitelist', 'mp3,aac',
                        '-read_intervals', f'%+{target + 1}',
                        '-show_frames', '-show_entries',
                        'format=format_name:stream=index,codec_name,codec_type,sample_rate:frame=stream_index,nb_samples',
                        '-of', 'json', str(path)], timeout)
    if rc or err.strip():
        return None
    info = json.loads(out)
    tracks = info.get('streams', [])
    codec = AUDIO_FORMATS[kind]
    if (info.get('format', {}).get('format_name') != codec or len(tracks) != 1
            or tracks[0].get('codec_type') != 'audio' or tracks[0].get('codec_name') != codec):
        return None
    rate = int(tracks[0].get('sample_rate') or 0)
    samples = sum(int(f.get('nb_samples') or 0) for f in info.get('frames', [])
                  if f.get('stream_index') == tracks[0]['index'])
    if rate > 0 and samples >= target * rate:
        return {'format_review': 'decoded_audio_only', 'audio_codec': codec,
                'decoded_seconds': round(samples / rate, 3), 'video_tracks': 0}
    return None


class VideoSources(HTMLParser):
    """Read declarative HTML video elements without interpreting a player or scripts."""

    def __init__(self):
        super().__init__()
        self.parents = []
        self.inert = []
        self.base = None
        self.sources = []
        self.overflow = False

    def handle_starttag(self, tag, attrs):
        if tag in ('template', 'noscript', 'iframe', 'object', 'script', 'style'):
            self.inert.append(tag)
        if self.inert:
            return
        attrs = dict(attrs)
        if tag == 'base' and self.base is None and attrs.get('href'):
            self.base = attrs['href']
        declared = tag == 'video' or (tag == 'source' and self.parents and self.parents[-1] == 'video')
        if declared and attrs.get('src'):
            src = attrs['src'].strip()
            if src and src not in self.sources:
                if len(self.sources) < MAX_PAGE_SOURCES:
                    self.sources.append(src)
                else:
                    self.overflow = True
        if tag not in VOID_ELEMENTS:
            self.parents.append(tag)

    def handle_endtag(self, tag):
        if self.inert:
            if self.inert[-1] == tag:
                self.inert.pop()
        elif self.parents and self.parents[-1] == tag:
            self.parents.pop()


def html_sources(data, page_url, headers, validate, make_key):
    parser = VideoSources()
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    try:
        parser.feed(data.decode(encoding, errors='replace'))
        parser.close()
        base = urljoin(page_url, parser.base or '')
        origin = urlsplit(page_url)
    except (ValueError, AssertionError):
        return {}, False
    sources = {}
    for raw in parser.sources:
        try:
            if '\ufffd' in raw or re.search(r'[\x00-\x20\x7f\\]', raw):
                continue
            url = urldefrag(urljoin(base, raw)).url
            validate(url)
            dest = urlsplit(url)
            if (dest.username is not None or dest.password is not None or dest.port == 0 or url == page_url
                    or len(url) > 4096 or re.search(r'[\x00-\x20\x7f\\]', url)):
                continue
            # A discovered URL is a separate candidate. Never propagate page credentials.
            request_headers = {}
            if (dest.scheme, dest.netloc) == (origin.scheme, origin.netloc):
                request_headers = {k: v for k, v in headers.items() if k.lower() == 'user-agent'}
            item = {'url': url, 'headers': request_headers}
            sources[make_key(item)] = item
        except ValueError:
            continue
    return sources, parser.overflow
