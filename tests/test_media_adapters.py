"""Playback fixtures use generated video and audio. No campaign samples are stored."""
import io
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit
from unittest.mock import patch
from xml.etree import ElementTree as ET

import dash_probe
import direct_probe
import sv_iptv as sv


ITEM = {'url': 'https://station.example/live', 'headers': {'Referer': 'https://station.example/player'}}
CDN = 'https://media.example/channel/'


def response(data, url, status=200, **headers):
    stream = io.BytesIO(data)
    stream.url, stream.status, stream.headers = url, status, headers
    return stream


class MediaBoundaries(unittest.TestCase):
    def test_access_limits_and_actual_network_failures_are_distinct(self):
        for status in (401, 403, 407, 429, 451, 404, 503):
            error = HTTPError(ITEM['url'], status, 'fixture', {}, None)
            with self.subTest(status=status), patch.object(sv, 'urlopen', side_effect=error):
                result = sv.check_stream(ITEM, {})
                self.assertEqual(result['inconclusive'], status in (401, 403, 407, 429, 451))
                self.assertFalse(result['ok'])

    def test_pages_encryption_and_unknown_dash_stay_pending_without_secondary_requests(self):
        documents = (
            '<!DOCTYPE html><html>Sign in</html>', '{"status":"login"}', '<playlist/>',
            '<MPD type="static"/>', '<MPD type="dynamic"><Period><ContentProtection/></Period></MPD>',
            '<!DOCTYPE MPD><MPD type="dynamic"/>', '<MPD type="dynamic"><Period/><Period/></MPD>',
            '#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=AES-128,URI="key"\n#EXT-X-STREAM-INF:BANDWIDTH=2\nv.m3u8',
            '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n#EXT-X-ENDLIST',
        )
        for body in documents:
            with self.subTest(body=body), patch.object(sv, 'urlopen', return_value=response(body.encode(), CDN)) as fetch:
                result = sv.check_stream(ITEM, {})
                self.assertTrue(result['inconclusive'], result)
                self.assertFalse(result['ok'])
                self.assertEqual(fetch.call_count, 1)

    def test_local_redirect_is_refused_before_following_it(self):
        with self.assertRaisesRegex(ValueError, 'non-public endpoint'):
            direct_probe.PublicMediaRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/')

    def test_explicit_and_implicit_byte_ranges_and_maps(self):
        text = ('#EXTM3U\n#EXT-X-MAP:URI="one",BYTERANGE="40@0"\n'
                '#EXTINF:2,\n#EXT-X-BYTERANGE:10@40\none\n#EXTINF:2,\n#EXT-X-BYTERANGE:12\none')
        rows = sv.media_segments(text, CDN)['segments']
        self.assertEqual([s['byte_range'] for s in rows], [(40, 10), (50, 12)])
        self.assertEqual([s['init_range'] for s in rows], [(0, 40), (0, 40)])
        for source in (text.replace('10@40', '10'), text[:-3] + 'other', text.replace('10@40', '-1@10')):
            with self.subTest(source=source), self.assertRaises(direct_probe.UnsupportedMedia):
                sv.media_segments(source, CDN)

    def test_byte_range_only_accepts_exact_partial_response(self):
        for status, span, data, coding in ((200, 'bytes 2-5/10', b'abcd', 'identity'),
                (206, 'bytes 1-4/10', b'abcd', 'identity'), (206, 'bytes 2-5/5', b'abcd', 'identity'),
                (206, 'bytes 2-5/10', b'ab', 'identity'), (206, 'bytes 2-5/10', b'abcde', 'identity'),
                (206, 'bytes 2-5/10', b'abcd', 'gzip')):
            with self.subTest(status=status, span=span, size=len(data), coding=coding):
                stream = response(data, CDN, status, **{'Content-Range': span, 'Content-Encoding': coding})
                with patch.object(sv, 'urlopen', return_value=stream), self.assertRaises(direct_probe.UnsupportedMedia):
                    sv.fetch_bytes(CDN, {}, byte_range=(2, 4))
        with patch.object(sv, 'urlopen') as fetch, self.assertRaises(direct_probe.UnsupportedMedia):
            sv.fetch_bytes(CDN, {}, limit=3, byte_range=(2, 4))
        fetch.assert_not_called()

    def test_dash_template_inheritance_and_large_or_open_repeats(self):
        body = """<MPD type="dynamic"><BaseURL>vod/</BaseURL><Period id="1">
          <SegmentTemplate timescale="10" initialization="$RepresentationID$/init"
           media="$RepresentationID$/$Time$-$Number%05d$-$$.m4s"><SegmentTimeline>
          <S t="0" d="20" r="10000000"/></SegmentTimeline></SegmentTemplate>
          <AdaptationSet mimeType="video/mp4"><Representation id="v" bandwidth="10"/></AdaptationSet>
          <AdaptationSet mimeType="audio/mp4"><Representation id="a" bandwidth="2"/></AdaptationSet></Period></MPD>"""
        video, audio = dash_probe.tracks(body, CDN, {})
        self.assertEqual(len(video['segments']), 24)
        self.assertEqual(video['segments'][-1]['url'], CDN+'vod/v/200000000-10000001-$.m4s')
        self.assertEqual(audio['segments'][-1]['init'], CDN+'vod/a/init')
        for other in (body.replace('10000000', '-1'), body.replace('$Time$', '$Unknown$'),
                      body.replace('d="20"', 'd="0"'), body.replace('type="dynamic"', 'type="static"')):
            with self.subTest(body=other), self.assertRaises(direct_probe.UnsupportedMedia):
                dash_probe.tracks(other, CDN, {})

    def test_visual_failure_is_pending_but_known_slate_is_a_real_result(self):
        for state in ('frame_decode_failed', 'blocked_frame'):
            inspection = {'state': state, 'reason': 'fixture'}
            with patch.object(sv, 'choose_media', side_effect=sv.VisualInspectionError(inspection)):
                result = sv.check_stream(ITEM, {})
            self.assertFalse(result['ok'])
            self.assertEqual(result['inconclusive'], state == 'frame_decode_failed')

    def test_exact_live_review_requirement_survives_a_later_failure(self):
        cfg = {'candidate_quality': {'enabled': True, 'require_stable': True}}
        channel = {'id': 'Sample.ru', 'country': 'RU'}
        first = {'ok': True, 'identity_review_required': True, 'live_broadcast_review_required': True}
        h = sv.observation({}, first, '2026-11-24T00:00:00Z', cfg)
        h = sv.observation(h, {'ok': False}, '2026-11-24T03:00:00Z', cfg)
        h['stable'] = True
        review = {'channel_id': channel['id'], 'stream_key': sv.stream_key(ITEM),
                  'reviewed_utc': '2026-11-24T00:00:00Z', 'reviewer': 'fixture', 'evidence': 'fixture',
                  'environment': 'ru-vps', 'identity_matches': True, 'russian_audio': True, 'no_placeholder': True}
        cfg['candidate_quality']['identity_reviews'] = [review]
        now = '2026-11-25T00:00:00Z'
        self.assertEqual(sv.candidate_review_reason(channel, ITEM, h, cfg, now), 'identity_and_language_review_required')
        review['live_broadcast_confirmed'] = True
        self.assertIsNone(sv.candidate_review_reason(channel, ITEM, h, cfg, now))
        other = {**ITEM, 'headers': {'User-Agent': 'different'}}
        self.assertEqual(sv.candidate_review_reason(channel, other, h, cfg, now), 'identity_and_language_review_required')

    def test_finite_direct_file_remains_pending_even_if_the_connection_is_slow(self):
        stream = response(b'FLV'+bytes(256), CDN, **{'Content-Length': '259'})
        with tempfile.TemporaryDirectory() as temp, patch.object(direct_probe, 'urlopen', return_value=stream):
            with self.assertRaisesRegex(direct_probe.UnsupportedMedia, 'finite media'):
                direct_probe.capture(ITEM, Path(temp)/'sample', {}, sv.validate_url)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
class RealMediaPlayback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.root = Path(scratch.name)
        def ff(*args):
            subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', *args],
                           cwd=cls.root, check=True, timeout=30, capture_output=True)
        ff('-f','lavfi','-i','testsrc2=size=160x96:rate=10','-f','lavfi','-i','sine=sample_rate=48000',
           '-t','22','-c:v','libx264','-preset','ultrafast','-threads','1','-g','20','-keyint_min','20',
           '-sc_threshold','0','-c:a','aac','-f','mpegts','source.ts')
        ff('-i','source.ts','-c','copy','-tag:v','avc1','-tag:a','mp4a','-bsf:a','aac_adtstoasc',
           '-f','dash','-seg_duration','2','-use_template','1','-use_timeline','1',
           '-adaptation_sets','id=0,streams=v id=1,streams=a','source.mpd')
        ff('-i','source.ts','-c','copy','-hls_list_size','0','-hls_time','2','-hls_flags','single_file','range.m3u8')
        ff('-i','source.ts','-c','copy','-tag:v','avc1','-tag:a','mp4a','-bsf:a','aac_adtstoasc',
           '-hls_list_size','0','-hls_time','2','-hls_segment_type','fmp4','-hls_flags','single_file','map.m3u8')
        ff('-i','source.ts','-c','copy','-bsf:a','aac_adtstoasc','-f','flv','source.flv')
        ff('-i','source.ts','-map','0:v:0','-c','copy','-f','mpegts','silent.ts')

    def manifest(self, kind, count):
        if kind == 'dash':
            doc = ET.fromstring((self.root/'source.mpd').read_bytes())
            doc.set('type', 'dynamic')
            for timeline in doc.findall('.//{*}SegmentTimeline'):
                for row in list(timeline)[1:]:
                    timeline.remove(row)
                timeline[0].set('r', str(count-1))
            return ET.tostring(doc)
        lines = (self.root/(kind+'.m3u8')).read_text().splitlines()
        first_segment = next(i for i, line in enumerate(lines) if line.startswith('#EXTINF:'))
        return ('\n'.join(lines[:first_segment + 3*count])+'\n').encode()

    def segmented(self, kind, *, replay=False, ignore_range=False, freeze=False, short_audio=False):
        polls = 0
        ranges = []
        def serve(request, timeout):
            nonlocal polls
            self.assertEqual(request.get_header('Referer'), ITEM['headers']['Referer'])
            if request.full_url == ITEM['url']:
                polls += 1
                return response(self.manifest(kind, 6 if polls == 1 or freeze else 7), CDN+'manifest')
            self.assertTrue(request.full_url.startswith(CDN))
            name = Path(urlsplit(request.full_url).path).name
            if replay and kind == 'dash' and '00007' in name:
                name = name.replace('00007','00006')
            data = (self.root/name).read_bytes()
            if short_audio and 'chunk-stream1' in name:
                data = data[:180]
            if request.get_header('Range') and not ignore_range:
                span = re.fullmatch(r'bytes=(\d+)-(\d+)', request.get_header('Range'))
                left, right = map(int, span.groups())
                ranges.append((left, right))
                return response(data[left:right+1], request.full_url, 206,
                                **{'Content-Range': f'bytes {left}-{right}/{len(data)}'})
            return response(data, request.full_url)
        with patch.object(sv, 'urlopen', side_effect=serve), patch.object(sv.time, 'sleep'):
            result = sv.check_stream(ITEM, {'test_seconds': 12})
        return result, polls, ranges

    def test_advancing_dash_and_byte_range_hls_decode_audio_video_and_frames(self):
        for kind, expected in (('dash', 'dash'), ('range', 'hls_byte_range'), ('map', 'hls_byte_range')):
            with self.subTest(kind=kind):
                result, polls, ranges = self.segmented(kind)
                self.assertTrue(result['ok'], result)
                self.assertTrue(result['advanced'] and result['identity_review_required'])
                self.assertEqual(result['transport'], expected)
                self.assertGreaterEqual(result['played_seconds'], 12)
                self.assertGreaterEqual(result['frame_inspection']['frames_checked'], 4)
                self.assertEqual(polls, 2)
                if kind != 'dash':
                    self.assertGreaterEqual(len(set(ranges)), 7)
                if kind == 'map':
                    self.assertEqual(ranges[0][0], 0)
                    self.assertGreater(ranges[1][0], ranges[0][1])

    def test_frozen_replayed_malformed_or_unserved_segments_do_not_count(self):
        for kind, options in (('dash', {'replay': True}), ('range', {'ignore_range': True}),
                              ('range', {'freeze': True}), ('dash', {'short_audio': True})):
            with self.subTest(kind=kind, options=options):
                result, _, _ = self.segmented(kind, **options)
                self.assertTrue(result['inconclusive'], result)
                self.assertFalse(result['ok'])

    def direct(self, name, kind, offset=0, damage=False):
        def capture(item, path, cfg, validate):
            data = bytearray((self.root/name).read_bytes()[offset:])
            if damage:
                data[len(data)//3:len(data)//2] = bytes(len(data)//2 - len(data)//3)
            path.write_bytes(data)
            return {'transport':kind, 'bytes':len(data), 'capture_seconds':18}
        with patch.object(sv, 'manifest', side_effect=direct_probe.DirectMedia('transport')), \
             patch.object(direct_probe, 'capture', side_effect=capture):
            return sv.check_stream(ITEM, {})

    def test_midpacket_ts_and_flv_require_live_review_after_actual_decode(self):
        for name, kind, offset in (('source.ts', 'mpegts', 238*188+13), ('source.flv', 'flv', 0)):
            with self.subTest(kind=kind):
                result = self.direct(name, kind, offset)
                self.assertTrue(result['ok'], result)
                self.assertGreaterEqual(result['played_seconds'], 11.85)
                self.assertTrue(result['identity_review_required'] and result['live_broadcast_review_required'])
                self.assertFalse(result['advanced'])

    def test_video_only_and_gap_inside_window_stay_pending(self):
        for name, damage in (('silent.ts', False), ('source.ts', True)):
            with self.subTest(name=name):
                result = self.direct(name, 'mpegts', damage=damage)
                self.assertTrue(result['inconclusive'], result)
                self.assertFalse(result['ok'])
