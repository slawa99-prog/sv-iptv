"""Read explicitly advertised segments from clear, dynamic MPEG-DASH timelines."""
from __future__ import annotations

from collections import deque
from fractions import Fraction
import re
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from direct_probe import UnsupportedMedia


def _number(value, *, minimum=0):
    if not re.fullmatch(r'-?[0-9]{1,19}', str(value)) or not minimum <= int(value) < 2**63:
        raise UnsupportedMedia('unsupported DASH timeline value')
    return int(value)


def _url(pattern, base, values):
    def substitute(match):
        if match[0] == '$$':
            return '$'
        key, width = match.group(1, 2)
        value = values.get(key)
        if value is None or (width and (not isinstance(value, int) or int(width) > 20)):
            raise UnsupportedMedia('unsupported DASH segment URL template')
        return str(value).zfill(int(width or 0))

    if not pattern:
        raise UnsupportedMedia('DASH initialization and media templates required')
    return urljoin(base, re.sub(r'\$\$|\$(RepresentationID|Bandwidth|Time|Number)(?:%0([1-9][0-9]?)d)?\$|\$', substitute, pattern))


def _track(root, period, adaptation, rep, manifest_url):
    base, attrs, timeline = manifest_url, {}, None
    for level in (root, period, adaptation, rep):
        child = level.find('BaseURL')
        if child is not None and child.text:
            base = urljoin(base, child.text.strip())
        if level.find('SegmentBase') is not None or level.find('SegmentList') is not None:
            raise UnsupportedMedia('DASH segment addressing requires review')
        template = level.find('SegmentTemplate')
        if template is not None:
            attrs.update(template.attrib)
            window = template.find('SegmentTimeline')
            if window is not None:
                timeline = window
    if timeline is None or not len(timeline):
        raise UnsupportedMedia('DASH live SegmentTimeline required')
    scale = _number(attrs.get('timescale', 1), minimum=1)
    offset = _number(attrs.get('presentationTimeOffset', 0))
    number = _number(attrs.get('startNumber', 1))
    end = 0
    values = {'RepresentationID': rep.get('id'), 'Bandwidth': _number(rep.get('bandwidth', 0))}
    init = _url(attrs.get('initialization'), base, values)
    # Keep only the live edge. A long-running broadcast may advertise millions
    # of repeated segments in a single S element.
    runs = deque(maxlen=24)
    for index, row in enumerate(timeline):
        if row.tag != 'S':
            raise UnsupportedMedia('unsupported DASH timeline element')
        start = _number(row.get('t', end))
        duration = _number(row.get('d'), minimum=1)
        if start < end or duration > 32 * scale:
            raise UnsupportedMedia('unsupported DASH segment timing')
        repeat = _number(row.get('r', 0), minimum=-1)
        if repeat == -1:
            following = timeline[index + 1].get('t') if index + 1 < len(timeline) else None
            if following is None:
                raise UnsupportedMedia('open-ended DASH timeline requires live timing review')
            gap = _number(following) - start
            if gap <= 0 or gap % duration:
                raise UnsupportedMedia('ambiguous DASH repeat boundary')
            count = gap // duration
        else:
            count = repeat + 1
        end = start + count * duration
        if end >= 2**63 or number + count >= 2**63:
            raise UnsupportedMedia('unsupported DASH timeline span')
        runs.append((start, duration, number, count))
        number += count
    remaining, segments = 24, []
    for start, duration, number, count in reversed(runs):
        for index in range(count - 1, max(0, count - remaining) - 1, -1):
            t = start + index * duration
            uri = _url(attrs.get('media'), base, {**values, 'Number': number + index, 'Time': t})
            segments.append({'url': uri, 'init': init, 'seconds': duration / scale, 'sequence': t,
                             'start': Fraction(t - offset, scale), 'end': Fraction(t + duration - offset, scale)})
        remaining -= min(remaining, count)
        if not remaining:
            break
    segments.reverse()
    if not segments or len({s['url'] for s in segments}) != len(segments):
        raise UnsupportedMedia('DASH timeline needs distinct media segments')
    identity = (root.get('availabilityStartTime'), period.get('id'), period.get('start'),
                adaptation.get('id'), rep.get('id'), values['Bandwidth'], scale, offset)
    return {'segments': segments, 'target': max(s['seconds'] for s in segments), 'track_id': identity}


def tracks(text, url, cfg):
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
        raise UnsupportedMedia('XML document type requires separate review')
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError) as exc:
        raise UnsupportedMedia('unrecognized XML or player response') from exc
    ns = '{urn:mpeg:dash:schema:mpd:2011}'
    if root.tag not in ('MPD', ns + 'MPD'):
        raise UnsupportedMedia('non-DASH XML or player response')
    for element in root.iter():
        if (element.tag.rsplit('}', 1)[-1].lower() in ('contentprotection', 'pssh', 'pro')
                or any(k.rsplit('}', 1)[-1].lower() == 'default_kid' for k in element.attrib)):
            raise UnsupportedMedia('encrypted media requires an authorized player')
        if '{http://www.w3.org/1999/xlink}href' in element.attrib:
            raise UnsupportedMedia('external DASH manifest reference requires review')
        element.tag = element.tag.removeprefix(ns)
    if root.get('type') != 'dynamic':
        raise UnsupportedMedia('finite DASH presentation requires live review')
    periods = root.findall('Period')
    if len(periods) != 1:
        raise UnsupportedMedia('multi-period DASH requires live timing review')
    period, candidates = periods[0], {'video': [], 'audio': []}
    for adaptation in period.findall('AdaptationSet'):
        for rep in adaptation.findall('Representation'):
            mime = rep.get('mimeType', adaptation.get('mimeType', ''))
            if mime not in ('video/mp4', 'audio/mp4'):
                continue
            kind = mime.split('/')[0]
            height = _number(rep.get('height', adaptation.get('height', 0)))
            bitrate = _number(rep.get('bandwidth', 0))
            if kind == 'video':
                suitable = height <= cfg.get('preferred_max_height', 720) and bitrate <= cfg.get('preferred_max_bitrate', 4_000_000)
                rank = (suitable, height if suitable else -bitrate, bitrate)
            else:
                rank = (adaptation.get('lang', '').lower() in ('ru', 'rus'), bitrate)
            candidates[kind].append((rank, adaptation, rep))
    result = []
    for options in candidates.values():
        if not options:
            raise UnsupportedMedia('DASH requires clear fragmented MP4 video and audio tracks')
        _, adaptation, rep = max(options, key=lambda opt: opt[0])
        result.append(_track(root, period, adaptation, rep, url))
    video, audio = result
    # A representation can advertise its next segment before the companion
    # track. Compare successive *playable* live edges on both tracks.
    audio_start, audio_end = audio['segments'][0]['start'], audio['segments'][-1]['end']
    tolerance = Fraction(1, 20)
    video['segments'] = [s for s in video['segments'] if
                         s['start'] >= audio_start - tolerance and s['end'] <= audio_end + tolerance]
    if not video['segments']:
        raise UnsupportedMedia('DASH audio and video windows do not align')
    first, last = video['segments'][0]['start'], video['segments'][-1]['end']
    audio['segments'] = [s for s in audio['segments'] if s['end'] > first + tolerance and s['start'] < last - tolerance]
    if not audio['segments']:
        raise UnsupportedMedia('DASH audio and video windows do not align')
    return video, audio


def aligned_audio(video, audio):
    """Select audio covering the same presentation interval (a small allowance for audio frame boundaries)."""
    start, end = video[0]['start'], video[-1]['end']
    tolerance = Fraction(1, 20)
    selected = [s for s in audio if s['end'] > start + tolerance and s['start'] < end - tolerance]
    if (not selected or selected[0]['start'] > start + tolerance or selected[-1]['end'] < end - tolerance
            or any(a['end'] != b['start'] for group in (video, selected) for a, b in zip(group, group[1:]))):
        raise UnsupportedMedia('DASH audio and video windows do not align')
    return selected
