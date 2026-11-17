"""Wide intake rules: retain unknown TV identities; never manufacture URLs.

This classifies research input, not playback success. Russia's national
languages are intentionally not a rejection criterion. URL+headers are kept
unchanged, so sv_iptv.stream_key remains the sole request identity.
"""
import base64
import ipaddress
import re
from urllib.parse import parse_qs, unquote, urlsplit

from sv_iptv import validate_url


KNOWN_BLOCKED_PATHS = {
    's7.iptv-24.net': '/blocked/index.m3u8',
    'stream8.cinerama.uz': '/blocked/index.m3u8',
}


def obvious_rejection(item):
    """Explain a deterministic intake error; never infer network availability.

    This is deliberately separate from the frozen campaign parser: previously
    frozen requests remain auditable and do not acquire synthetic observations.
    Public IPv6, IDNs, dynamic PHP endpoints and old expiry parameters survive.
    """
    try:
        validate_url(item['url'])
        p = urlsplit(item['url'])
        if p.port is not None and not 0 < p.port <= 65535:
            return 'invalid_port'
    except (ValueError, KeyError):
        return 'invalid_or_nonpublic_url'
    host = (p.hostname or '').rstrip('.').casefold()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if re.search(r'[\s<>{}\\|"\']', host):
            return 'template_or_invalid_host'
        if '.' not in host or host.endswith(('.lan', '.home', '.home.arpa', '.invalid', '.test')):
            return 'nonpublic_or_reserved_hostname'
        try:
            encoded = host.encode('idna').decode('ascii')
        except UnicodeError:
            return 'invalid_hostname'
        if len(encoded) > 253 or any(not s or len(s) > 63 for s in encoded.split('.')):
            return 'invalid_hostname'
    path = unquote(p.path)
    if KNOWN_BLOCKED_PATHS.get(host) == path:
        return 'known_blocked_slate'
    if any(path == '/' + h + tail for h, tail in KNOWN_BLOCKED_PATHS.items()):
        return 'known_blocked_slate_proxy'
    return None


def disposition(item, source=""):
    """Return a storage/test bucket, without probing the address."""
    try:
        validate_url(item['url'])
        p = urlsplit(item['url'])
    except (ValueError, KeyError):
        return 'nonpublic_or_unsupported_url'
    rejection = obvious_rejection(item)
    if rejection:
        return rejection
    path = unquote(p.path)
    query = parse_qs(p.query)
    if (any(k.casefold() in {'username', 'password', 'passwd', 'pass', 'login', 'token_user'} for k in query)
            or re.search(r'/(?:live|movie|series)/[^/]+/[^/]+/\d+(?:[./]|$)', path, re.I)
            or re.fullmatch(r'/(?!hls/|live/|tv/|stream/)[^/]+/[^/]+/\d+\.(?:ts|m3u8)', path, re.I)
            or any(k.casefold() in {'authorization', 'cookie'} for k in item.get('headers', {}))):
        return 'account_credential_redacted'
    for token in query.get('token', []):
        if token.casefold().startswith('admin_'):
            return 'account_credential_redacted'
        if ':' in token:
            try:
                decoded = base64.b64decode(token.rsplit(':', 1)[-1] + '===').decode('utf8')
                if ':' in decoded:
                    return 'account_credential_redacted'
            except (ValueError, UnicodeError):
                pass
    if (re.search(r'/(?:tests?|fixtures?|samples?|examples?)/', source, re.I)
            or p.hostname in {'example.com', 'example.org', 'example.net'}
            or p.hostname.endswith(('.example', '.invalid', '.test', '.example.com', '.example.org', '.example.net'))):
        return 'synthetic_fixture'
    if re.search(r'/scan_channels[^/]*\.(?:m3u8?|txt)', source, re.I):
        return 'unverified_numeric_enumeration'
    if re.fullmatch(r'\s*(?:(?:\d+\.?\s*)?(?:канал|channel)(?:\s+ID)?\s*\d+|заглушка\s*\d+)\s*', item.get('name', ''), re.I):
        return 'unverified_numeric_enumeration'
    if item.get('unsupported'):
        return 'drm_requires_authorized_player'
    label = item.get('name', '') + ' ' + item.get('attrs', {}).get('group-title', '')
    if (not item.get('name') and not item.get('attrs', {}).get('tvg-id')
            and re.search(r'/tracks-[^/]+/\d{4}/\d\d/\d\d/\d\d/\d\d/[^/]+\.ts$', path)):
        return 'hls_media_segments'
    if (re.search(r'\.(?:mp4|mkv|avi|mov|webm)$', path, re.I)
            or p.hostname.startswith('vod.')
            or re.search(r'(?:^|/)(?:movie|series|vod)(?:/|$)', path, re.I)
            or re.search(r'\bVOD\b|видеотека|сезон\s*\d+|серия\s*\d+', label, re.I)):
        return 'on_demand_video'
    if (re.search(r'\.(?:mp3|aac|ogg|flac|wav)$', path, re.I)
            or re.search(r'\bradio\b|\bradios\b|радиостанци|радиоэфир|\baudio\b', item.get('attrs', {}).get('group-title', ''), re.I)):
        return 'audio_or_radio'
    if re.search(r'webcams?|веб.?камер|камеры наблюдения', label, re.I):
        return 'web_camera'
    # No cut-off by age, naming confidence, Cyrillic spelling, national
    # language, picture quality, part-time schedule or a previous failed probe.
    return 'tv_candidate'


def scope_hint(item, metadata, known_ids, names, excluded_ids):
    """Metadata hints are a work queue, never a visual identity approval."""
    import sv_iptv as sv
    raw = sv.base_id(item.get('attrs', {}).get('tvg-id', ''))
    if raw in excluded_ids:
        return 'previously_excluded_identity', None
    if raw in known_ids:
        return 'existing_catalog', raw
    choices = names.get(sv.name_key(item.get('name', '')), set())
    if len(choices) == 1:
        return 'name_match_requires_review', next(iter(choices))
    meta = metadata.get(raw)
    if meta and meta.get('country') == 'RU':
        return 'russian_domestic_metadata', raw
    if meta and meta.get('country') and meta.get('country') != 'RU':
        return 'foreign_metadata_requires_language_review', raw
    return 'unidentified_requires_review', None
