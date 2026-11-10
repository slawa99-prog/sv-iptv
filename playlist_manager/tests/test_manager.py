import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("manager", Path(__file__).resolve().parents[1] / "manager.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def playlist(count=4, prefix="old"):
    return ("#EXTM3U\n" + "".join(
        f'#EXTINF:-1 tvg-id="id{i}" group-title="Группа",Канал {i}\nhttps://media.example/{prefix}/{i}.m3u8\n'
        for i in range(count))).encode()


class ParserTests(unittest.TestCase):
    def test_relocates_relative_streams_logos_epg_and_keeps_options(self):
        raw = ('\ufeff#EXTM3U x-tvg-url="epg.xml.gz"\r\n#PLAYLIST:Old\r\n'
            '#EXTINF:-1 tvg-id="one" tvg-logo="../logo.png" group-title="Кино",Первый\r\n'
            '#EXTVLCOPT:http-user-agent=Player One\r\n'
            '#EXTVLCOPT:http-referrer=https://site.example/watch\r\n'
            '#EXTHTTP:{"X-Test":"unchanged"}\r\n'
            '#KODIPROP:inputstream.adaptive.manifest_type=hls\r\n'
            'streams/a.m3u8|User-Agent=Example\r\n').encode()
        out, count = m.parse_playlist(raw, "https://author.example/lists/tv.m3u", "SV ТВ 2")
        self.assertEqual(count, 1)
        self.assertIn('x-tvg-url="https://author.example/lists/epg.xml.gz"', out)
        self.assertIn('tvg-logo="https://author.example/logo.png"', out)
        self.assertIn('https://author.example/lists/streams/a.m3u8|User-Agent=Example', out)
        self.assertIn('#EXTHTTP:{"X-Test":"unchanged"}', out)
        self.assertIn('#EXTVLCOPT:http-user-agent=Player One', out)
        self.assertIn('tvg-id="one"', out)
        self.assertEqual(out.count('#PLAYLIST:'), 1)

    def test_rejects_html_empty_hls_and_damaged_entries(self):
        for raw in [b'<html>Error</html>', b'#EXTM3U\n', b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=3\na.m3u8',
                    b'#EXTM3U\n#EXTINF:-1,A\n', b'#EXTM3U\nhttps://site.example/a',
                    b'#EXTM3U\n#EXTINF:-1,A\n#EXTINF:-1,B\nhttps://site.example/b',
                    b'#EXTM3U\n#EXTINF:-1,A\nfile:///etc/passwd',
                    b'#EXTM3U\n#EXTINF:-1,A\npipe://sh']:
            with self.subTest(raw=raw), self.assertRaises(m.PlaylistError):
                m.parse_playlist(raw, 'https://author.example/tv.m3u')

    def test_cp1251(self):
        data = '#EXTM3U\n#EXTINF:-1,Первый\nhttps://media.example/1\n'.encode('cp1251')
        self.assertIn('Первый', m.parse_playlist(data)[0])

    def test_source_validation(self):
        for url in ['file:///a', 'http://localhost/a', 'http://127.0.0.1/a', 'http://10.0.0.1/a',
                    'http://169.254.169.254/latest', 'https://user:pass@site.example/x',
                    'https://site.example/a\nHeader:x', 'http://[::1]/x', 'http://site.local/a']:
            with self.subTest(url=url), self.assertRaises(m.PlaylistError):
                m.check_url(url)

    def test_redirect_rejects_private_destination(self):
        with self.assertRaises(m.PlaylistError):
            m.SafeRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/key')


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = {'version': 1, 'public_base_url': 'https://example.com/managed',
               'minimum_retained_fraction': 0.5,
               'slots': {key: {'title': 'SV ' + key, 'source': None, 'auto_refresh': True} for key in m.SLOT_IDS}}
        cfg['slots']['tv1']['source'] = 'repo:tv.m3u'
        m.write_json(self.root/'managed/config.json', cfg)
        (self.root/'tv.m3u').write_bytes(playlist())
        self.responses = {}

        def fetcher(source, root):
            if source.startswith('repo:'):
                return (root/source[5:]).read_bytes(), None
            response = self.responses[source]
            if isinstance(response, Exception):
                raise response
            return response, 'https://author.example/redirected/list.m3u'

        self.fetcher = fetcher
        self.manager = m.Manager(self.root, fetcher)
        self.assertEqual(self.manager.refresh(), [])

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_switch_changes_only_selected_slot_and_leaves_original_playlist(self):
        old_root = (self.root/'tv.m3u').read_bytes()
        self.responses['https://author.example/new'] = playlist(2, 'new')
        self.manager.replace('tv1', 'https://author.example/new')
        self.assertIn('/new/', self.manager.output('tv1').read_text())
        self.assertEqual((self.root/'tv.m3u').read_bytes(), old_root)
        self.assertFalse(self.manager.output('tv2').exists())
        self.assertEqual(self.manager.config['slots']['tv1']['source'], 'https://author.example/new')
        self.assertEqual(self.manager.state['slots']['tv1']['entries'], 2)
        self.assertIn('https://example.com/managed/tv1.m3u', (self.root/'managed/README.md').read_text())

    def test_invalid_manual_replacement_leaves_all_files_unchanged(self):
        before = self.files()
        for bad in [b'<html>blocked</html>', m.PlaylistError('Network failed')]:
            self.responses['https://author.example/new'] = bad
            with self.assertRaises(m.PlaylistError):
                self.manager.replace('tv1', 'https://author.example/new')
            self.assertEqual(self.files(), before)

    def test_refresh_failure_preserves_playlist_and_records_error(self):
        self.responses['https://author.example/new'] = playlist(4, 'new')
        self.manager.replace('tv1', 'https://author.example/new')
        before = self.manager.output('tv1').read_bytes()
        self.responses['https://author.example/new'] = m.PlaylistError('Network failed')
        self.assertEqual(self.manager.refresh(), ['tv1'])
        self.assertEqual(self.manager.output('tv1').read_bytes(), before)
        self.assertEqual(self.manager.state['slots']['tv1']['result'], 'error')

    def test_drop_guard_does_not_publish_partial_upstream_list(self):
        before = self.manager.output('tv1').read_bytes()
        (self.root/'tv.m3u').write_bytes(playlist(1))
        self.assertEqual(self.manager.refresh(), ['tv1'])
        self.assertEqual(self.manager.output('tv1').read_bytes(), before)

    def test_rollback_restores_source_and_freezes_automatic_refresh(self):
        original = self.manager.output('tv1').read_bytes()
        self.responses['https://author.example/new'] = playlist(2, 'new')
        self.manager.replace('tv1', 'https://author.example/new')
        # Reload from disk, as the next GitHub run would do.
        self.manager = m.Manager(self.root, self.fetcher)
        self.manager.rollback('tv1')
        self.assertEqual(self.manager.output('tv1').read_bytes(), original)
        self.assertEqual(self.manager.config['slots']['tv1']['source'], 'repo:tv.m3u')
        self.assertFalse(self.manager.config['slots']['tv1']['auto_refresh'])
        (self.root/'tv.m3u').write_bytes(playlist(4, 'later'))
        self.manager.refresh()
        self.assertEqual(self.manager.output('tv1').read_bytes(), original)
        self.manager.resume('tv1')
        self.assertTrue(self.manager.config['slots']['tv1']['auto_refresh'])
        self.assertIn('/later/', self.manager.output('tv1').read_text())

    def test_same_content_different_author_can_be_rolled_back(self):
        self.responses['https://author.example/same'] = playlist()
        self.manager.replace('tv1', 'https://author.example/same')
        self.manager.rollback('tv1')
        self.assertEqual(self.manager.config['slots']['tv1']['source'], 'repo:tv.m3u')

    def test_relative_paths_use_final_url_after_redirect(self):
        self.responses['https://short.example/x'] = b'#EXTM3U\n#EXTINF:-1,A\nchannel/index.m3u8\n'
        self.manager.replace('tv2', 'https://short.example/x')
        self.assertIn('https://author.example/redirected/channel/index.m3u8', self.manager.output('tv2').read_text())

    def test_one_source_failure_does_not_block_other_slots(self):
        self.manager.config['slots']['tv2']['source'] = 'https://author.example/2'
        self.manager.config['slots']['tv3']['source'] = 'https://author.example/3'
        self.responses['https://author.example/2'] = m.PlaylistError('Down')
        self.responses['https://author.example/3'] = playlist(2, 'third')
        self.assertEqual(self.manager.refresh(), ['tv2'])
        self.assertFalse(self.manager.output('tv2').exists())
        self.assertTrue(self.manager.output('tv3').exists())

    def test_path_traversal_and_managed_source_loop_are_rejected(self):
        for slot in ['../tv', '/etc/passwd', 'TV1']:
            with self.assertRaises(m.PlaylistError):
                self.manager.replace(slot, 'https://author.example/new')
        with self.assertRaises(m.PlaylistError):
            self.manager.replace('tv1', 'https://raw.githubusercontent.com/slawa99-prog/sv-iptv/main/managed/tv2.m3u')

    def test_user_input_is_data_not_shell(self):
        self.responses['https://author.example/list?q=$(touch-marker)'] = playlist()
        with patch.dict('os.environ', {'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'INPUT_OPERATION': 'Заменить источник', 'INPUT_SLOT': 'tv1 — SV ТВ 1',
            'INPUT_SOURCE': 'https://author.example/list?q=$(touch-marker)', 'INPUT_PRESET': ''}), \
            patch.object(m, 'fetch', self.fetcher):
            # The production CLI receives arguments via env. There is no shell evaluation.
            source = __import__('os').environ['INPUT_SOURCE']
            self.manager.replace('tv1', source)
            self.assertEqual(self.manager.config['slots']['tv1']['source'], source)


if __name__ == '__main__':
    unittest.main()
