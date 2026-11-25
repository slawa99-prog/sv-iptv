"""Bounded descriptions of HTTP responses for format review. No payload is retained."""
from __future__ import annotations

import codecs
import re


PREFIX_BYTES = 4096
# Only fixed labels reach the public report. Arbitrary MIME subtypes and
# parameters can contain identifiers supplied by the origin.
KNOWN_MIME = frozenset("""
application/dash+xml application/json application/mpegurl application/mp4
application/octet-stream application/ogg application/smil+xml application/vnd.apple.mpegurl
application/vnd.ms-asf application/vnd.ms-sstr+xml application/x-mpegurl application/xml
audio/aac audio/aacp audio/flac audio/mp4 audio/mpeg audio/mpegurl audio/ogg audio/wav
audio/x-mpegurl image/gif image/jpeg image/png image/webp text/html text/plain text/xml
video/mp2t video/mp4 video/mpeg video/ogg video/webm video/x-flv video/x-matroska
video/x-ms-asf video/x-msvideo
""".split())
FILE_SIGNATURES = (
    (b'\x1a\x45\xdf\xa3', 'ebml'), (b'OggS', 'ogg'), (b'fLaC', 'flac'),
    (bytes.fromhex('3026b2758e66cf11a6d900aa0062ce6c'), 'asf'),
    (b'\x00\x00\x01\xba', 'mpeg_program_stream'), (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'\xff\xd8\xff', 'jpeg'), (b'GIF87a', 'gif'), (b'GIF89a', 'gif'),
    (b'\x1f\x8b\x08', 'gzip'), (b'PK\x03\x04', 'zip'),
)
XML_ROOTS = {'html': 'html', 'head': 'html', 'body': 'html', 'mpd': 'dash',
             'smoothstreamingmedia': 'smooth_streaming', 'smil': 'smil', 'asx': 'asx'}
# These signatures identify responses for which the raw TS/FLV decoder is
# inapplicable. A short or unrecognized prefix still gets its normal probe.
DIRECT_REVIEW_ONLY = {
    'iso_bmff', 'ebml', 'ogg', 'asf', 'mpeg_program_stream', 'avi', 'wave', 'flac',
    'png', 'jpeg', 'gif', 'webp', 'zip', 'pls', 'url_text', 'access_message',
}
# Indexed by (MPEG-1, layer), then by the four-bit bitrate index minus one.
# Free-format MPEG audio needs a different length detector.
MPEG2_LAYER_2_3_BITRATES = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
BITRATES_KBPS = {
    (True, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): MPEG2_LAYER_2_3_BITRATES,
    (False, 3): MPEG2_LAYER_2_3_BITRATES,
}
MPEG_LAYERS = {1: 'mpeg_audio_layer_1', 2: 'mpeg_audio_layer_2', 3: 'mp3'}


def audio_frame(data, pos):
    """Return the frame's stable profile and encoded length, when the header is valid."""
    if pos + 4 > len(data) or data[pos] != 0xff:
        return None
    flags, rates, mode = data[pos + 1:pos + 4]
    if flags & 0xf6 == 0xf0:  # ADTS syncword and both layer bits
        header_bytes = 7 if flags & 1 else 9
        frequency = (rates >> 2) & 15
        if pos + header_bytes > len(data) or frequency > 12:
            return None
        size = ((mode & 3) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
        if size <= header_bytes:
            return None
        channels = ((rates & 1) << 2) | (mode >> 6)
        return ('aac_adts', flags & 8, rates >> 6, frequency, channels), size
    version, layer = (flags >> 3) & 3, 4 - ((flags >> 1) & 3)
    index, frequency = rates >> 4, (rates >> 2) & 3
    if (flags & 0xe0 != 0xe0 or version == 1 or layer == 4 or
            index in (0, 15) or frequency == 3 or mode & 3 == 2):
        return None
    sample_rate = (44100, 48000, 32000)[frequency] // {3: 1, 2: 2, 0: 4}[version]
    bitrate = BITRATES_KBPS[version == 3, layer][index - 1] * 1000
    padding = (rates >> 1) & 1
    if layer == 1:
        size = (12 * bitrate // sample_rate + padding) * 4
    else:
        slots = 72 if layer == 3 and version != 3 else 144
        size = slots * bitrate // sample_rate + padding
    return (MPEG_LAYERS[layer], version, frequency), size


def framed_audio(data):
    # A live HTTP read can begin in the middle of a frame. Require matching
    # headers at two successive computed boundaries, allowing variable bitrate.
    for match in re.finditer(b'\xff(?=[\xe0-\xff])', data):
        pos = match.start()
        frame = audio_frame(data, pos)
        if not frame:
            continue
        profile, size = frame
        for _ in range(2):
            pos += size
            following = audio_frame(data, pos)
            if not following or following[0] != profile:
                break
            size = following[1]
        else:
            return profile[0]
    return None


def after_id3(data):
    # The length is synchsafe: seven usable bits in each byte. Never look
    # inside a tag's pictures, text or other attachments for audio frames.
    if len(data) < 10 or data[3] not in (2, 3, 4) or any(b & 128 for b in data[6:10]):
        return b''
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | byte
    footer = 10 if data[3] == 4 and data[5] & 16 else 0
    return data[10 + size + footer:]


def transport_kind(data):
    if data.startswith(b'FLV'):
        return 'flv'
    for packet in (188, 192, 204):
        for offset in range(min(packet, max(0, len(data) - 2 * packet))):
            if data[offset] == data[offset + packet] == data[offset + 2 * packet] == 0x47:
                return 'mpegts'
    return None


def body_kind(data):
    if data is None:
        return 'not_read'
    data = data[:PREFIX_BYTES]
    if not data:
        return 'empty'
    tagged = data.startswith(b'ID3')
    if tagged:
        data = after_id3(data)
        if not data or data.startswith(b'ID3'):
            return 'id3'
    for signature, kind in FILE_SIGNATURES:
        if data.startswith(signature):
            return kind
    if len(data) >= 12 and data.startswith(b'RIFF'):
        return {b'AVI ': 'avi', b'WAVE': 'wave', b'WEBP': 'webp'}.get(data[8:12], 'riff')
    if len(data) >= 8:
        size = int.from_bytes(data[:4], 'big')
        if data[4:8] in (b'ftyp', b'styp', b'moof', b'moov', b'mdat') and (size in (0, 1) or size >= 8):
            return 'iso_bmff'
    audio = framed_audio(data)
    if audio:
        return transport_kind(data) or audio
    encoding = 'utf-16' if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else 'utf-8-sig'
    # The prefix can end in the middle of a multibyte character.
    text = codecs.getincrementaldecoder(encoding)(errors='replace').decode(data).lstrip()
    lower = text.lower()
    if lower.startswith('#extm3u'):
        return 'hls' if '#ext-x-' in lower else 'm3u'
    if lower.startswith('[playlist]'):
        return 'pls'
    if lower.startswith(('{', '[')):
        return 'json_like'
    if lower.startswith('<'):
        # Skip leading declarations/comments only. Do not expand XML entities
        # or interpret arbitrary tags inside a player page as a manifest.
        markup = re.sub(r'\A(?:<\?xml\b[^>]*\?>\s*|<!--.*?-->\s*)+', '', lower, flags=re.S)
        if re.match(r'<!doctype\s+html\b', markup):
            return 'html'
        tag = re.match(r'<(?:[\w.-]+:)?([\w.-]+)(?=[\s/>])', markup)
        return XML_ROOTS.get(tag[1], 'xml_or_markup') if tag else 'xml_or_markup'
    if lower.startswith(('http://', 'https://')):
        return 'url_text'
    if lower.strip() in ('access denied', 'forbidden', 'unauthorized', '401 unauthorized',
                         '403 forbidden', 'authentication required'):
        return 'access_message'
    transport = transport_kind(data)
    if transport:
        return transport
    if tagged:
        return 'id3'
    if text and '\ufffd' not in text and all(c.isprintable() or c in '\t\r\n' for c in text):
        return 'text'
    return 'unknown_binary'


def describe_response(response, prefix):
    headers = getattr(response, 'headers', None) or {}
    mime = (headers.get('Content-Type') or '').partition(';')[0].strip().lower()
    content_type = mime or 'unspecified'
    if mime and mime not in KNOWN_MIME:
        content_type = 'other'
    diagnostic = {'body_kind': body_kind(prefix), 'content_type': content_type}
    if prefix is not None:
        diagnostic['prefix_bytes'] = min(len(prefix), PREFIX_BYTES)
    status = getattr(response, 'status', None)
    if isinstance(status, int) and 100 <= status <= 599:
        diagnostic['http_status'] = int(status)
    return diagnostic
