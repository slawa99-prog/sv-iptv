import base64
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import placeholder_filter as pf
import sv_iptv as sv


class VisualPolicyTests(unittest.TestCase):
    def test_retained_playlist_removes_only_confirmed_slate_urls(self):
        text = '#EXTM3U\n#EXTINF:-1 tvg-id="A.ru",A\nhttps://example.com/a\n#EXTINF:-1 tvg-id="B.ru",B\nhttps://example.com/b\n'
        entries = sv.parse_m3u(text)
        results = {sv.stream_key(entries[0]): {'ok': False, 'frame_inspection': {'state':'blocked_frame'}},
                   sv.stream_key(entries[1]): {'ok': False, 'reason':'network timeout'}}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'cloud.m3u';path.write_text(text)
            main=Path(d)/'tv.m3u';main.write_text('reviewed local playlist')
            self.assertEqual(sv.prune_confirmed_placeholders(path,results),1)
            self.assertNotIn('https://example.com/a',path.read_text())
            self.assertIn('https://example.com/b',path.read_text())
            self.assertEqual(main.read_text(),'reviewed local playlist')

    def test_discovered_channel_with_slate_is_excluded_from_cloud_and_backups(self):
        existing={'id':'A.ru','name':'A','group':'ТВ'}
        new={'id':'B.ru','name':'B','group':'ТВ'}
        text='#EXTM3U\n#EXTINF:-1 tvg-id="A.ru",A\nhttps://example.com/a\n#EXTINF:-1 tvg-id="B.ru",B\nhttps://example.com/b\n'
        def checker(item,cfg):
            good=item['name']=='A'
            return {'ok':good,'reason':'decoded' if good else 'video placeholder',
                    'visual_criteria':sv.visual_signature(),
                    'frame_inspection':{'state':'no_known_placeholder' if good else 'blocked_frame'}}
        with tempfile.TemporaryDirectory() as d,patch.object(sv,'ROOT',Path(d)):
            root=Path(d);(root/'channels.json').write_text(json.dumps([existing]));(root/'input.m3u').write_text(text)
            (root/'tv.m3u').write_text('reviewed local playlist')
            with patch.object(sv,'discover_catalog',return_value=([existing,new],{'state':'fresh','added':1})),\
                 contextlib.redirect_stdout(io.StringIO()):
                report=sv.build({'sources':['input.m3u'],'publication_mode':'reviewed-local',
                    'minimum_channels':1,'require_fresh_remote_source':False},tester=checker,now='2026-11-11T08:00:00+00:00')
            self.assertEqual(report['catalog_channels'],2)
            self.assertEqual(report['unique_channels_passed'],1)
            self.assertEqual(report['known_placeholder_streams'],1)
            self.assertNotIn('https://example.com/b',(root/'cloud.m3u').read_text())
            self.assertNotIn('https://example.com/b',(root/'backups.json').read_text())
            self.assertEqual((root/'tv.m3u').read_text(),'reviewed local playlist')

    def test_old_technical_successes_do_not_establish_new_visual_stability(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        old = {'checks': [{'at': (start + timedelta(hours=6*i)).isoformat(), 'ok': True} for i in range(8)]}
        result = {'ok': True, 'visual_criteria': sv.visual_signature(),
                  'frame_inspection': {'state': 'no_known_placeholder'}}
        updated = sv.observation(old, result, (start + timedelta(hours=48)).isoformat(), {})
        self.assertEqual(updated['samples'], 9)
        self.assertEqual(updated['visual_samples'], 1)
        self.assertFalse(updated['stable'])
        self.assertEqual(len(old['checks']), 8)

    def test_same_interval_retry_does_not_erase_slate_failure(self):
        bad = {'ok': False, 'visual_criteria': sv.visual_signature(), 'frame_inspection': {'state': 'blocked_frame'}}
        good = {**bad, 'ok': True, 'frame_inspection': {'state': 'no_known_placeholder'}}
        h = sv.observation({}, bad, '2026-11-11T00:00:00+00:00', {})
        h = sv.observation(h, good, '2026-11-11T00:20:00+00:00', {})
        self.assertEqual(h['samples'], 1)
        self.assertEqual(h['success_rate'], 0)
        self.assertTrue(h['last_result']['ok'])

    def test_frame_extraction_failure_is_closed(self):
        with tempfile.TemporaryDirectory() as d, patch.object(sv, 'run_command', return_value=(1, '', 'failed')):
            with self.assertRaises(sv.VisualInspectionError) as err:
                sv.inspect_sample(Path(d)/'sample.m3u8', {}, 12, [])
            self.assertEqual(err.exception.inspection['state'], 'frame_decode_failed')

    def test_missing_templates_are_not_a_successful_inspection(self):
        with tempfile.TemporaryDirectory() as d, patch.object(sv, 'run_command', return_value=(0, '', '')),\
             patch.object(pf, 'inspect', return_value={'state': 'no_templates'}):
            with self.assertRaises(sv.VisualInspectionError):
                sv.inspect_sample(Path(d)/'sample.m3u8', {}, 12, [])

    def test_later_slate_rejects_stream_after_clean_initial_window(self):
        initial = {'target': 1, 'segments': [{'url': 'https://example.com/a.ts', 'seconds': 12, 'sequence': 0}]}
        fresh = {'target': 1, 'segments': [{'url': 'https://example.com/b.ts', 'seconds': 2, 'sequence': 1}]}
        good = {'played_seconds': 12, 'pixels': 160*90, 'download_seconds': 1,
                'frame_inspection': {'state': 'no_known_placeholder', 'frames': []}}
        bad = sv.VisualInspectionError({'state': 'blocked_frame', 'reason': 'CINERAMA', 'id': 'cinerama'})
        with patch.object(sv, 'choose_media', return_value=('https://example.com/a.m3u8', 'first', None)),\
             patch.object(sv, 'media_segments', side_effect=[initial, fresh]),\
             patch.object(sv, 'manifest', return_value='later'),\
             patch.object(sv, 'decode_sample', side_effect=[good, bad]),patch.object(sv.time, 'sleep'):
            result = sv.check_stream({'url': 'https://example.com/a.m3u8', 'headers': {}}, {})
        self.assertFalse(result['ok'])
        self.assertEqual(result['frame_inspection']['state'], 'blocked_frame')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RealSlateIntegrationTests(unittest.TestCase):
    def test_playable_video_and_audio_with_each_new_slate_is_rejected(self):
        data = json.loads((Path(__file__).parent/'slate-fixtures.json').read_text())
        for ref in data:
            with self.subTest(slate=ref['id']), tempfile.TemporaryDirectory() as d:
                folder=Path(d); pgm=folder/'slate.jpg'; ts=folder/'slate.ts'
                pgm.write_bytes(base64.b64decode(ref['jpeg_base64']))
                subprocess.run(['ffmpeg','-v','error','-loop','1','-i',str(pgm),'-f','lavfi','-i',
                    'sine=frequency=440:sample_rate=48000','-t','3','-vf','scale=480:270',
                    '-c:v','libx264','-preset','ultrafast','-crf','10','-pix_fmt','yuv420p','-threads','1',
                    '-c:a','aac','-f','mpegts',str(ts)],check=True,capture_output=True,timeout=15)
                with patch.object(sv, 'fetch_bytes', return_value=ts.read_bytes()):
                    with self.assertRaises(sv.VisualInspectionError) as err:
                        sv.decode_sample([{'url':'https://example.com/slate.ts','seconds':3}],{}, {})
                self.assertEqual(err.exception.inspection['state'],'blocked_frame')


if __name__ == '__main__': unittest.main()
