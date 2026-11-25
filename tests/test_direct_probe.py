"""Offline transport fixtures; these tests are not VPS observations."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import direct_probe as dp
import sv_iptv as sv


class DirectProbeTests(unittest.TestCase):
    def setUp(self):
        self.item = {'url': 'https://example.com/live', 'headers': {'Referer': 'https://example.com/player'}}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'sample'
        self.ts = (b'\x47' + b'\0' * 187) * 400

    def response(self, data, url='https://example.com/live'):
        response = io.BytesIO(data)
        response.url = url
        return response

    def test_transport_sniff_does_not_accept_html_mpd_or_m3u(self):
        self.assertEqual(dp.transport_kind(self.ts), 'mpegts')
        self.assertEqual(dp.transport_kind(b'FLV\x01\x05'), 'flv')
        for value in (b'<MPD>manifest</MPD>', b'<html>failure</html>', b'#EXTM3U\nhttps://example.com'):
            self.assertIsNone(dp.transport_kind(value))

    def test_capture_is_time_bounded_and_keeps_request_headers(self):
        with patch.object(dp, 'urlopen', return_value=self.response(self.ts)) as open_mock, \
             patch.object(dp.time, 'monotonic', side_effect=[0,0,19,19]):
            result = dp.capture(self.item, self.path, {}, sv.validate_url)
        self.assertEqual(result['transport'], 'mpegts')
        self.assertEqual(result['capture_seconds'], 19)
        self.assertEqual(open_mock.call_args.args[0].get_header('Referer'), self.item['headers']['Referer'])

    def test_binary_response_is_detected_before_waiting_for_the_hls_size_limit(self):
        with patch.object(sv, 'urlopen', return_value=self.response(self.ts)), \
             patch.object(sv.time, 'monotonic', return_value=0), self.assertRaises(dp.UnsupportedMedia):
            sv.manifest(self.item['url'], {}, {})

    def test_finite_files_unknown_formats_caps_and_private_redirects_do_not_pass(self):
        for data, cfg, url in [(self.ts, {}, self.item['url']), (b'<MPD/>', {}, self.item['url']),
            (self.ts, {'sample_max_bytes': 10}, self.item['url']), (self.ts, {}, 'http://127.0.0.1/private')]:
            with self.subTest(url=url, cfg=cfg, size=len(data)), \
                 patch.object(dp, 'urlopen', return_value=self.response(data, url)), \
                 patch.object(dp.time, 'monotonic', return_value=0), self.assertRaises(ValueError):
                dp.capture(self.item, self.path, cfg, sv.validate_url)

    def test_decoder_has_no_network_protocols_and_requires_audio_video_frames(self):
        calls = []
        def run(command, timeout):
            calls.append(command)
            if command[0] == 'ffprobe':
                return 0, json.dumps({
                    'streams': [{'index': 0, 'codec_type': 'video', 'width': 640, 'height': 480},
                                {'index': 1, 'codec_type': 'audio'}], 'format': {'start_time': '0'},
                    'packets': [{'stream_index': track, 'pts_time': i / 10, 'duration_time': .1,
                                 'flags': 'K' if not i else ''} for track in (0, 1) for i in range(130)]}), ''
            return 0, 'out_time_us=12000000\nframe=300\n', ''
        def capture(item, path, cfg, validate):
            path.write_bytes(self.ts)
            return {'transport': 'mpegts', 'bytes': len(self.ts), 'capture_seconds': 12}
        with patch.object(dp, 'capture', side_effect=capture), patch.object(sv, 'run_command', side_effect=run), \
             patch.object(sv, 'inspect_sample', return_value={'state': 'no_known_placeholder'}), \
             patch.object(sv, 'visual_signature', return_value='fixture'):
            result = dp.check_direct(self.item, {})
        self.assertTrue(result['ok'] and result['identity_review_required'] and result['live_broadcast_review_required'])
        self.assertFalse(result['advanced'])
        for command in calls:
            self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'file')
            self.assertFalse(any('https://' in s for s in command))

    def test_unsupported_format_stays_inconclusive_and_encryption_is_not_bypassed(self):
        for exc in (dp.DirectMedia('non-playlist media response'), dp.UnsupportedMedia('encrypted media requires an authorized player')):
            with self.subTest(reason=str(exc)), patch.object(sv, 'manifest', side_effect=exc), \
                 patch.object(sv, 'visual_signature', return_value='fixture'), \
                 patch.object(sv, 'check_direct', side_effect=dp.UnsupportedMedia('format review')) as direct:
                result = sv.check_stream(self.item, {})
                self.assertTrue(result['inconclusive'])
                self.assertFalse(result['ok'])
                self.assertEqual(direct.call_count, 1 if isinstance(exc, dp.DirectMedia) else 0)
