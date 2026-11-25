"""Offline HTTP response fixtures. These are not measurements of live streams."""
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import direct_probe as direct
from expansion_check import Campaign, MEDIA_PROBE_VERSION
from response_diagnostics import PREFIX_BYTES, body_kind, describe_response
import sv_iptv as sv


URL = 'https://example.com/live'
ITEM = {'url': URL, 'headers': {}}
MP4 = b'\x00\x00\x00\x18ftypisom' + bytes(5000)


def response(payload, mime, **headers):
    stream = io.BytesIO(payload)
    stream.url = URL
    stream.status = 200
    stream.headers = {'Content-Type': mime, **headers}
    return stream


class ResponseDiagnosticsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required for encoded audio fixtures')
    def test_real_audio_frames_from_the_middle_of_a_stream_remain_on_review(self):
        for encoder, muxer, kind, rate, quality in [
            ('libmp3lame', 'mp3', 'mp3', 48000, ['-b:a', '128k']),
            ('libmp3lame', 'mp3', 'mp3', 24000, ['-q:a', '4']),
            ('libmp3lame', 'mp3', 'mp3', 12000, ['-b:a', '24k']),
            ('mp2', 'mp2', 'mpeg_audio_layer_2', 44100, ['-b:a', '128k']),
            ('aac', 'adts', 'aac_adts', 48000, ['-b:a', '128k']),
        ]:
            with self.subTest(encoder=encoder, rate=rate):
                command = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                           f'sine=frequency=777:sample_rate={rate}', '-t', '2.0',
                           '-c:a', encoder, *quality, '-f', muxer, 'pipe:1']
                encoded = subprocess.run(command, check=True, capture_output=True, timeout=10).stdout
                self.assertEqual(body_kind(encoded), kind)
                joined = encoded[159:]
                self.assertEqual(body_kind(joined), kind)
                # A MIME label is only a claim; neither it nor the audio evidence is a TV trial.
                with patch.object(sv, 'urlopen', return_value=response(b'x' * 900, 'text/plain')), \
                     patch.object(direct, 'urlopen', return_value=response(joined, 'video/mp4')):
                    result = sv.check_stream(ITEM, {})
                self.assertTrue(result['inconclusive'])
                self.assertFalse(result['ok'])
                self.assertEqual(result['response_diagnostic']['body_kind'], kind)
                self.assertEqual(result['response_diagnostic']['content_type'], 'video/mp4')

    def test_audio_requires_matching_successive_frame_boundaries(self):
        # MPEG-1 Layer I, 128 kb/s at 48 kHz. One frame is 128 bytes.
        header = bytes.fromhex('ffff4400')
        frame = header + bytes(124)
        self.assertEqual(body_kind(frame * 3), 'mpeg_audio_layer_1')
        self.assertEqual(body_kind(frame * 2), 'unknown_binary')
        self.assertEqual(body_kind(frame + b'x' + frame + b'x' + frame), 'unknown_binary')
        reserved_frequency = bytes.fromhex('ffff4c00') + bytes(124)
        self.assertEqual(body_kind(reserved_frequency * 3), 'unknown_binary')
        # A tag can contain a picture or unrelated media bytes. These frames are inside the tag.
        tag = b'ID3\x04\x00\x00\x00\x00\x03\x00' + frame * 3
        self.assertEqual(body_kind(tag), 'id3')
        self.assertEqual(body_kind(tag + frame * 3), 'mpeg_audio_layer_1')
        self.assertEqual(body_kind(b'\x47' + bytes(187) + b'\x47' + bytes(187) + b'\x47' + frame * 3), 'mpegts')
        self.assertEqual(body_kind(MP4[:16] + frame * 3), 'iso_bmff')
        self.assertEqual(describe_response(response(b'', 'audio/mpeg'), bytes(700))['body_kind'], 'unknown_binary')

    def test_only_unidentified_binary_from_previous_release_gets_audio_retry(self):
        key = sv.stream_key(ITEM)
        c = Campaign({'id': 'fixture', 'environment': 'ru-vps', 'max_requests_per_run': 1000},
                     {}, {}, {}, {}, sv.stream_key, '2026-11-25T12:00:00Z', verification_items=[ITEM])
        c.stats[key] = {'samples': 0, 'successes': 0, 'last_inconclusive': '2026-11-25T10:00:00Z',
                        'inconclusive_probe_version': '2026-11-25.2'}
        for kind, expected in [('unknown_binary', True), ('id3', True), ('ogg', False), ('not_read', False)]:
            with self.subTest(kind=kind):
                c.stats[key]['inconclusive_reason'] = f'HTTP response ({kind}) requires format or access review'
                self.assertEqual(key in c.batch({key: ITEM}), expected)
        c.stats[key]['inconclusive_reason'] = 'HTTP response (unknown_binary) requires format or access review'
        c.now = '2026-11-25T11:59:59Z'
        self.assertNotIn(key, c.batch({key: ITEM}))
        c.now = '2026-11-25T12:00:00Z'
        c.stats[key]['inconclusive_probe_version'] = MEDIA_PROBE_VERSION
        self.assertNotIn(key, c.batch({key: ITEM}))

    def test_body_signatures_are_independent_of_mime_and_bounded(self):
        fixtures = [
            (MP4, 'iso_bmff'), (b'\x1a\x45\xdf\xa3' + bytes(10), 'ebml'),
            (b'RIFF' + bytes(4) + b'AVI ', 'avi'), (b'OggS' + bytes(10), 'ogg'),
            (b'\xef\xbb\xbf<?xml version="1.0"?><!-- start --><p:MPD/>', 'dash'),
            (b'<SmoothStreamingMedia/>', 'smooth_streaming'), (b'<smil/>', 'smil'),
            ('<?xml version="1.0"?><html/>'.encode('utf-16'), 'html'),
            (b'<html>' + b'G' * 800 + b'</html>', 'html'),
            (b'<Something/>', 'xml_or_markup'), (b'#EXTM3U\n#EXT-X-TARGETDURATION:4', 'hls'),
            (b'#EXTM3U\n#EXTINF:-1,TV', 'm3u'), (b'[playlist]\nFile1=secret', 'pls'),
            (b'{"key":"secret"}', 'json_like'), (b'403 Forbidden\r\n', 'access_message'),
            (URL.encode(), 'url_text'), (b'\x85\x00\x01\xff', 'unknown_binary'),
            (b'plain text', 'text'), (b'', 'empty'), (None, 'not_read'),
        ]
        for payload, expected in fixtures:
            with self.subTest(expected=expected):
                self.assertEqual(body_kind(payload), expected)
        self.assertEqual(body_kind(b' ' * PREFIX_BYTES + b'<MPD/>'), body_kind(b' ' * PREFIX_BYTES))
        detail = describe_response(response(b'', 'video/private-secret; cookie=private-secret'), MP4)
        self.assertEqual(detail, {'body_kind': 'iso_bmff', 'content_type': 'other', 'http_status': 200,
                                  'prefix_bytes': PREFIX_BYTES})

    def test_open_container_served_as_html_stays_pending_without_second_fetch(self):
        source = response(MP4, 'text/html; private-token=secret', **{'Set-Cookie': 'secret'})
        with patch.object(sv, 'urlopen', return_value=source) as first, patch.object(direct, 'urlopen') as second:
            result = sv.check_stream(ITEM, {})
        first.assert_called_once()
        second.assert_not_called()
        self.assertTrue(result['inconclusive'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['response_diagnostic']['body_kind'], 'iso_bmff')
        self.assertEqual(result['response_diagnostic']['content_type'], 'text/html')
        self.assertNotIn('secret', json.dumps(result))

    def test_child_payload_is_not_replaced_by_the_master_on_a_retry(self):
        master = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000\nchild\n'
        with patch.object(sv, 'urlopen', side_effect=[response(master, 'text/plain'),
                          response(MP4, 'video/mp4')]) as fetch, patch.object(direct, 'urlopen') as raw:
            result = sv.check_stream(ITEM, {})
        self.assertEqual([c.args[0].full_url for c in fetch.call_args_list], [URL, 'https://example.com/child'])
        raw.assert_not_called()
        self.assertTrue(result['inconclusive'])
        self.assertEqual(result['response_diagnostic']['body_kind'], 'iso_bmff')

    def test_unknown_raw_body_and_finite_media_keep_evidence_from_actual_reply(self):
        for payload, mime, extra, expected in [
            (b'\x85\x00' * 3000, 'application/octet-stream', {}, 'unknown_binary'),
            (b'FLV' + bytes(85), 'video/x-flv', {'Content-Length': '88'}, 'flv'),
        ]:
            initial = response(b'x' * 900, 'text/plain')
            with self.subTest(kind=expected), patch.object(sv, 'urlopen', return_value=initial), \
                    patch.object(direct, 'urlopen', return_value=response(payload, mime, **extra)):
                result = sv.check_stream(ITEM, {})
            self.assertTrue(result['inconclusive'])
            self.assertEqual(result['response_diagnostic']['body_kind'], expected)
            self.assertEqual(result['response_diagnostic']['content_type'], mime)

    def test_access_status_does_not_read_an_error_page_or_change_network_failures(self):
        for code in (401, 403, 429, 404, 503):
            body = io.BytesIO(b'private error')
            failure = HTTPError(URL, code, 'example', {'Content-Type': 'text/html; token=secret'}, body)
            with self.subTest(code=code), patch.object(sv, 'urlopen', side_effect=failure):
                result = sv.check_stream(ITEM, {})
            self.assertEqual(body.tell(), 0)
            if code in (401, 403, 429):
                self.assertTrue(result['inconclusive'])
                self.assertEqual(result['response_diagnostic'], {'body_kind': 'not_read',
                                 'content_type': 'text/html', 'http_status': code})
            else:
                self.assertFalse(result['inconclusive'])
                self.assertNotIn('response_diagnostic', result)

    def test_fragmented_ts_with_wrong_mime_still_uses_the_existing_transport_probe(self):
        # A read can end before even one packet is available.
        data = b'X' * 30 + (b'\x47' + bytes(187)) * 40
        stream = response(data, 'text/html')
        read = stream.read1
        stream.read1 = lambda cap: read(min(cap, 100))
        with tempfile.TemporaryDirectory() as tmp, patch.object(direct, 'urlopen', return_value=stream), \
                patch.object(direct.time, 'monotonic', side_effect=[0] * 8 + [19, 19]):
            out = Path(tmp) / 'sample'
            result = direct.capture(ITEM, out, {}, sv.validate_url)
            self.assertEqual(result['transport'], 'mpegts')
            self.assertEqual(out.read_bytes(), data[:700])

    def test_only_a_fresh_attempt_can_change_the_frozen_diagnostics(self):
        key = sv.stream_key(ITEM)
        other = {**ITEM, 'headers': {'Referer': URL}}
        c = Campaign({'id': 'fixture', 'environment': 'ru-vps', 'max_requests_per_run': 1000},
                     {}, {'A': [other]}, {}, {'pc_success_request_keys': [key]}, sv.stream_key,
                     '2026-11-25T10:00:00Z', verification_items=[ITEM])
        reason = 'format requires a separate adapter or access review'

        def attempt(at, reason, version, detail=None):
            r = {'checked_utc': at, 'ok': False, 'inconclusive': True, 'reason': reason, 'media_probe_version': version}
            if detail:
                r['response_diagnostic'] = detail
            return r
        old = attempt(c.now, reason, '2026-11-25.1')
        c.record({key: old})
        c.now = '2026-11-25T11:59:59Z'
        self.assertNotIn(key, c.batch({key: ITEM}))
        c.now = '2026-11-25T12:00:00Z'
        self.assertIn(key, c.batch({key: ITEM}))
        c.stats[key]['inconclusive_reason'] = 'continuous sample decode incomplete; format review required'
        self.assertNotIn(key, c.batch({key: ITEM}))
        c.stats[key]['inconclusive_reason'] = reason
        detail = describe_response(response(b'', 'video/mp4'), MP4)
        new = attempt(c.now, 'container requires review', MEDIA_PROBE_VERSION, detail)
        c.record({key: new, sv.stream_key(other): new})
        c.record({key: old})
        counts = c.report()['frozen_response_diagnostics']
        self.assertEqual(counts, {'requests': 1, 'below_required_samples': 1, 'by_body_kind': {'iso_bmff': 1},
                                 'by_content_type': {'video/mp4': 1}})
        self.assertEqual((c.category(key), c.stats[key]['samples'], c.stats[key]['inconclusive_attempts']),
                         ('pending', 0, 2))
        self.assertIn(key, c.pc)
        c.now = '2026-11-25T14:00:00Z'
        self.assertNotIn(key, c.batch({key: ITEM}))
        c.record({key: attempt(c.now, 'encrypted media requires an authorized player', MEDIA_PROBE_VERSION)})
        self.assertNotIn('response_diagnostic', c.stats[key])
        c.record({key: {**new, 'checked_utc': '2026-12-02T14:00:00Z'}})
        c.record({key: {'ok': True, 'checked_utc': '2026-12-09T14:00:00Z'}})
        self.assertEqual(c.report()['frozen_response_diagnostics']['requests'], 0)
        self.assertNotIn('response_diagnostic', c.stats[key])
        self.assertEqual(c.stats[key]['samples'], 1)
