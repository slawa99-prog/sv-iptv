"""Offline fixtures for the held-format adapter. Never import them into VPS history."""
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import direct_probe
from expansion_check import Campaign, FROZEN_RETRY_POLICY, MEDIA_PROBE_VERSION
from format_review import MAX_PAGE_BYTES
import sv_iptv as sv


PAGE = {'url': 'https://example.com/watch', 'headers': {'User-Agent': 'player', 'Referer': 'https://example.com/'}}
AUDIO = {'url': 'https://example.com/radio', 'headers': {}}


def response(data, url, mime, finite=False):
    stream = io.BytesIO(data)
    stream.url = url
    stream.status = 200
    stream.headers = {'Content-Type': mime}
    if finite:
        stream.headers['Content-Length'] = str(len(data))
    return stream


class HeldFormatTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'real local decoder required')
    def test_audio_is_decoded_and_has_no_video_track(self):
        for encoder, fmt, kind in [('libmp3lame', 'mp3', 'mp3'), ('aac', 'adts', 'aac_adts')]:
            with self.subTest(fmt=fmt):
                data = subprocess.check_output(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=777:sample_rate=44100', '-t', '13', '-c:a', encoder,
                    '-b:a', '128k', '-f', fmt, 'pipe:1'], timeout=10)
                # Start and finish between frames; do not accept MIME as evidence.
                for sample, confirmed in [(data[163:-100], True), (data[:12000], False),
                                          (data[:32000] + b'not audio' * 100 + data[32000:], False)]:
                    with patch.object(sv, 'urlopen', return_value=response(sample, AUDIO['url'], 'video/mp4')), \
                         patch.object(direct_probe, 'urlopen', return_value=response(sample, AUDIO['url'], 'video/mp4', True)):
                        result = sv.check_stream(AUDIO, {'_review_held_format': True})
                    self.assertFalse(result['ok'])
                    self.assertTrue(result['inconclusive'])
                    detail = result['response_diagnostic']
                    self.assertEqual(detail['body_kind'], kind)
                    self.assertEqual(detail['format_review'], 'decoded_audio_only' if confirmed else 'audio_window_unconfirmed')
                    if confirmed:
                        self.assertGreaterEqual(detail['decoded_seconds'], 12)
                        self.assertEqual(detail['video_tracks'], 0)

    def test_html_keys_are_separate_and_raw_urls_remain_local(self):
        markup = b"""<html><head><base href="/station/"></head><body>
            <script>let clip = 'ignored.m3u8'</script>
            <template><video src="ignored.ts"></video></template>
            <source src="outside.m3u8"><video><source src="live.m3u8?signature=private">
            <picture><source src="poster.jpg"></picture>
            <source src="https://cdn.example.org/tv.mpd"><source src="file:///tmp/nope">
            <source src="http://127.0.0.1/"><source src="javascript:void(0)"></video></body></html>"""
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sv, 'urlopen', return_value=response(markup, PAGE['url'], 'text/html')) as fetch, \
             patch.object(direct_probe, 'urlopen') as raw:
            result = sv.check_stream(PAGE, {'_review_held_format': True, '_format_source_dir': tmp})
            expected = [{'url': 'https://example.com/station/live.m3u8?signature=private',
                         'headers': {'User-Agent': 'player'}},
                        {'url': 'https://cdn.example.org/tv.mpd', 'headers': {}}]
            keys = {sv.stream_key(i) for i in expected}
            self.assertEqual(set(result['response_diagnostic']['source_keys']), keys)
            saved = Path(tmp) / (sv.stream_key(PAGE) + '.json')
            self.assertEqual(json.loads(saved.read_text())['sources'], {sv.stream_key(i): i for i in expected})
            self.assertEqual(saved.stat().st_mode & 0o077, 0)
            self.assertNotIn('signature=private', json.dumps(result))
            self.assertFalse(result['ok'])
            self.assertTrue(result['inconclusive'])
            fetch.assert_called_once()
            raw.assert_not_called()

    def test_oversized_and_script_only_pages_remain_open(self):
        for body, state in [(b'<html>' + bytes(MAX_PAGE_BYTES + 1), 'html_too_large'),
                (b'<html><iframe src="player"></iframe><script>play("http://example.org/s")</script></html>',
                 'html_no_declared_media')]:
            with patch.object(sv, 'urlopen', return_value=response(body, PAGE['url'], 'text/html')):
                result = sv.check_stream(PAGE, {'_review_held_format': True})
            self.assertTrue(result['inconclusive'])
            self.assertEqual(result['response_diagnostic']['format_review'], state)

    def test_declared_frozen_source_is_scheduled_as_its_own_request(self):
        video = {'url': 'https://example.com/tv.m3u8', 'headers': {}}
        alternate = {**video, 'headers': {'User-Agent': 'different player'}}
        page, candidate, header_variant = map(sv.stream_key, (PAGE, video, alternate))
        cfg = {'id': 'fixture', 'environment': 'ru-vps', 'frozen_retry_policy': FROZEN_RETRY_POLICY,
               'max_requests_per_run': 1}
        c = Campaign(cfg, {}, {}, {}, {}, sv.stream_key, '2026-11-27T08:00:00Z',
                     verification_items=[PAGE, video, alternate])
        for k in (candidate, header_variant):
            c.stats[k].update(samples=2, successes=1, last_checked='2026-11-26T10:00:00Z')
        c.stats[page].update(last_inconclusive='2026-11-27T07:00:00Z',
            sweep_attempts=[{'at': '2026-11-27T03:00:00Z', 'outcome': 'inconclusive'},
                            {'at': '2026-11-27T07:00:00Z', 'outcome': 'inconclusive',
                             'format_review_version': MEDIA_PROBE_VERSION}],
            response_diagnostic={'body_kind': 'html', 'format_review': 'html_declared_media',
                                 'source_keys': [candidate, 'not-in-the-frozen-queue']})
        selected = c.batch(c.items)
        self.assertEqual(set(selected), {candidate})
        c.record({candidate: {'ok': True, 'checked_utc': c.now}})
        self.assertEqual(c.stats[candidate]['samples'], 3)
        self.assertEqual((c.stats[page]['samples'], c.stats[header_variant]['samples']), (0, 2))
        report = c.sweep_report()['format_review']
        self.assertEqual((report['declared_source_keys'], report['source_keys_in_frozen_queue'],
                          report['frozen_source_keys_checked_since_page']), (2, 1, 1))

    def test_targeted_requeue_persists_two_audio_visits_without_tv_samples(self):
        other = {'url': 'https://example.com/access', 'headers': {}}
        pending = {'url': 'https://example.com/pending', 'headers': {}}
        items = [AUDIO, PAGE, other, pending]
        a, p, o, missing = map(sv.stream_key, items)
        cfg = {'id': 'fixture', 'environment': 'ru-vps', 'max_requests_per_run': 2,
               'frozen_retry_policy': FROZEN_RETRY_POLICY}
        def at(h):
            return f'2026-11-27T{h}:00Z'
        def load(now, saved):
            return Campaign(cfg, saved, {}, {}, {'pc_success_request_keys': [a]},
                            sv.stream_key, at(now), verification_items=items)
        c = load('06:00', {})
        for key, kind in [(a, 'mp3'), (p, 'html'), (o, 'not_read'), (missing, 'html')]:
            c.stats[key].update(sweep_attempts=[{'at': at('00:00'), 'outcome': 'inconclusive'},
                                                {'at': at('03:00'), 'outcome': 'inconclusive'}],
                                last_inconclusive=at('03:00'), response_diagnostic={'body_kind': kind})
        c.stats[a].update(samples=1, successes=1, sample_at=at('00:00'), first_checked=at('00:00'),
                          last_checked=at('00:00'), ever_passed=True)
        c.stats[missing].pop('last_inconclusive')
        c.stats[missing]['sweep_attempts'][-1].update(at=at('05:59'), outcome='failure')
        c.stats[missing].update(samples=1, last_checked=at('05:59'))
        self.assertEqual(set(c.batch(c.items)), {a, p})
        proof = {'ok': False, 'inconclusive': True, 'checked_utc': c.now, 'reason': 'fixture',
                 'format_review_version': MEDIA_PROBE_VERSION, 'response_diagnostic': {'body_kind': 'mp3',
                 'format_review': 'decoded_audio_only', 'audio_codec': 'mp3', 'video_tracks': 0, 'decoded_seconds': 12.4}}
        page_result = {**proof, 'response_diagnostic': {'body_kind': 'html', 'format_review': 'html_declared_media',
                                                       'source_keys': [a]}}
        # An old checkpoint must not consume the new adapter's first chance.
        c.record({a: {**proof, 'format_review_version': 'old'}})
        self.assertEqual(len(c.stats[a]['sweep_attempts']), 2)
        c.record({a: proof, p: page_result})
        c.record({a: proof})
        c = load('07:59', c.state)
        self.assertNotIn(a, c.batch(c.items))
        c = load('08:00', c.state)
        self.assertIn(a, c.batch(c.items))
        c.record({a: {**proof, 'checked_utc': c.now}})
        c = load('18:00', c.state)
        self.assertEqual(set(c.batch(c.items)), {missing})
        self.assertEqual(c.sweep_stage(a), 'audio_only')
        self.assertEqual((c.stats[a]['samples'], c.stats[a]['successes'], c.category(a)), (1, 1, 'pending'))
        self.assertIn(a, c.pc)
        self.assertEqual(c.stats[p]['samples'], 0)
        r = c.sweep_report()['format_review']
        self.assertEqual((r['requests_probed'], r['audio_only_confirmed'], r['source_keys_in_frozen_queue']), (2, 1, 1))
        self.assertEqual(r['html_pages'], {'html_declared_media': 1})

if __name__ == '__main__':
    unittest.main()
