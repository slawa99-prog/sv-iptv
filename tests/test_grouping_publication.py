import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sv_iptv as sv


class GroupingPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(sv, 'ROOT', self.root)
        p.start()
        self.addCleanup(p.stop)
        self.channels = [
            {'id': 'TravelChannel.ru', 'name': 'Travel Channel', 'group': 'Региональные и общие'},
            {'id': 'Vmirezhivotnykh.ru', 'name': 'В мире животных', 'group': 'Региональные и общие'}]
        self.rules = {'groups': {'TravelChannel.ru': 'Путешествия',
                                'Vmirezhivotnykh.ru': 'Природа и животные'},
                      'group_order': ['Природа и животные', 'Путешествия']}
        for name, data in [('channels.json', self.channels), ('channel-groups.json', self.rules)]:
            (self.root / name).write_text(json.dumps(data), encoding='utf-8')
        (self.root / 'input.m3u').write_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="TravelChannel.ru" group-title="Региональные",Travel Channel\n'
            'https://example.com/travel.m3u8\n'
            '#EXTINF:-1 tvg-id="Vmirezhivotnykh.ru",В мире животных\nhttps://example.com/nature.m3u8\n')
        self.cfg = {'sources': ['input.m3u'], 'minimum_channels': 1, 'workers': 1,
                    'require_fresh_remote_source': False}

    def build(self, ok=True):
        def tester(item, cfg):
            return {'ok': ok, 'reason': 'ok' if ok else 'unavailable', 'media_url': item['url']}
        with contextlib.redirect_stdout(io.StringIO()):
            return sv.build(self.cfg, tester=tester, now='2026-11-10T12:00:00+00:00')

    def test_source_categories_cannot_override_reviewed_ids_and_order(self):
        report = self.build()
        self.assertEqual({c['id']: c['group'] for c in report['selected']}, self.rules['groups'])
        parsed = sv.parse_m3u((self.root / 'tv.m3u').read_text())
        self.assertEqual([x['attrs']['group-title'] for x in parsed], self.rules['group_order'])

    def test_unknown_discovered_channel_is_not_mislabeled_regional(self):
        cfg = {'catalog_discovery': {'enabled': True,
            'metadata_url': 'https://example.com/channels.json',
            'playlist_url': 'https://example.com/rus.m3u'}}
        metadata = [{'id': 'Unknown.ru', 'name': 'Unknown', 'categories': ['general']}]
        playlist = '#EXTM3U\n#EXTINF:-1 tvg-id="Unknown.ru",Unknown\nhttps://example.com/new.m3u8\n'
        with patch.object(sv, 'fetch_bytes', side_effect=[json.dumps(metadata).encode(), playlist.encode()]):
            catalog, _ = sv.discover_catalog([], cfg, '2026-11-10T12:00:00+00:00')
        self.assertEqual(catalog[0]['group'], 'Общие')

    def test_cloud_success_and_failure_preserve_reviewed_home_playlist(self):
        self.cfg['publication_mode'] = 'reviewed-local'
        original = b'#EXTM3U\n# Reviewed home snapshot\n'
        (self.root / 'tv.m3u').write_bytes(original)
        (self.root / 'home-review.json').write_text(json.dumps({
            'unique_channels': 376, 'strictly_stable_channels': 0,
            'checked_utc': '2026-11-10T09:54:49+00:00'}))
        report = self.build()
        self.assertEqual(report['published_playlist'], 'cloud.m3u')
        self.assertEqual(report['unique_channels_passed'], 2)
        self.assertEqual(report['reviewed_local_playlist']['unique_channels'], 376)
        self.assertEqual((self.root / 'tv.m3u').read_bytes(), original)
        cloud = (self.root / 'cloud.m3u').read_bytes()
        report = self.build(ok=False)
        self.assertEqual(report['publication'], 'blocked')
        self.assertEqual((self.root / 'cloud.m3u').read_bytes(), cloud)
        self.assertEqual((self.root / 'tv.m3u').read_bytes(), original)

    def test_local_check_keeps_group_rules_but_uses_its_own_output_and_history(self):
        self.cfg['publication_mode'] = 'reviewed-local'
        (self.root / 'history.json').write_text('{"cloud_only":true}')
        output, cfg = sv.prepare_local_workspace(self.cfg)
        self.assertEqual(cfg['publication_mode'], 'automatic')
        self.assertEqual(cfg['check_environment'], 'local-network')
        self.assertEqual(json.loads((output / 'channel-groups.json').read_text()), self.rules)
        self.assertFalse((output / 'history.json').exists())


if __name__ == '__main__':
    unittest.main()
