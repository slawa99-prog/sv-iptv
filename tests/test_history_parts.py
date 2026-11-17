"""Round-trip and corruption tests for lossless report publication."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server import history_parts as hp
from server import ru_check as ru


class HistoryPartsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.history = {'streams': {str(i): {'checks': [{'at': '2026-11-17T00:00:00Z', 'ok': True}],
            'pc_protection': 'не смешивать'} for i in range(100)},
            'expansion_campaign': {'id': 'fixture', 'environment': 'ru-vps',
                'requests': {str(i): {'samples': 2, 'successes': i % 2} for i in range(100)},
                'cohort_keys': list(map(str, range(100))), 'previously_working_keys': ['10'],
                'publication_protected_keys': ['20']}, 'last_selected': {'A.ru': '10'}}

    def save(self, files):
        for name, body in files.items():
            path = self.root / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(body, encoding='utf-8')

    def parts(self):
        return hp.package_history(self.history, inline_bytes=12000, part_bytes=4000)

    def test_small_history_remains_inline_and_preserves_every_field(self):
        files = hp.package_history(self.history)
        self.assertEqual(set(files), {'history.json'})
        self.save(files)
        self.assertEqual(hp.load_snapshot(self.root), self.history)

    def test_large_history_roundtrip_and_prior_storage_field_are_lossless(self):
        self.history['history_storage'] = {'old': 'preserve'}
        files = self.parts()
        self.assertGreater(len(files), 2)
        self.assertLessEqual(max(len(s.encode()) for n, s in files.items() if n != 'history.json'), 4000)
        self.save(files)
        self.assertEqual(hp.load_snapshot(self.root), self.history)
        self.assertEqual(self.history['expansion_campaign']['requests']['10']['samples'], 2)

    def test_missing_corrupt_duplicate_and_mixed_parts_are_rejected(self):
        files = self.parts()
        for mode in ('missing', 'corrupt', 'duplicate', 'mixed'):
            with self.subTest(mode=mode):
                self.save(files)
                index = json.loads(files['history.json'])
                part = index['history_storage']['parts'][0]
                path = self.root / part['path']
                if mode == 'missing': path.unlink()
                elif mode == 'corrupt': path.write_text('{}')
                elif mode == 'duplicate':
                    index['history_storage']['parts'].append(part)
                    (self.root/'history.json').write_text(json.dumps(index))
                else:
                    changed = hp.encode({'other': {'samples': 99}})
                    path.write_text(changed)
                    part.update(sha256=hp.digest(changed), bytes=len(changed.encode()), entries=1)
                    (self.root/'history.json').write_text(json.dumps(index))
                with self.assertRaises((ValueError, FileNotFoundError)):
                    hp.load_snapshot(self.root)

    def test_publisher_reuses_identical_blobs_and_commits_all_parts_atomically(self):
        files = self.parts()
        self.save({'history.json': json.dumps(self.history),
            'status.json': json.dumps({'check_environment': 'ru-vps', 'checked_utc': 'fixture', 'publication': 'blocked'}),
            'node.json': json.dumps({'checked_utc': 'fixture'})})
        reused_name = next(n for n in files if n.startswith('history-parts/'))
        body = files[reused_name].encode()
        reused_sha = hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()
        calls = []
        def api(method, path, payload=None):
            calls.append((method, path, payload))
            if method == 'GET':
                if path.startswith('git/ref/'): return {'object': {'sha': 'b'*40}}
                if path.startswith('git/commits/'): return {'tree': {'sha': 'c'*40}}
                return {'tree': [{'sha': reused_sha, 'type': 'blob'}]}
            return {'sha': 'a'*40}
        before = (self.root/'history.json').read_bytes()
        with patch.object(ru, 'package_history', return_value=files):
            ru.publish_report(self.root, api)
        self.assertEqual((self.root/'history.json').read_bytes(), before)
        tree = next(p for m,n,p in calls if m == 'POST' and n == 'git/trees')['tree']
        self.assertTrue(set(files) <= {e['path'] for e in tree})
        self.assertEqual(next(e['sha'] for e in tree if e['path'] == reused_name), reused_sha)
        self.assertFalse(any(m == 'POST' and n == 'git/blobs' and p['content'] == files[reused_name] for m,n,p in calls))
        self.assertEqual(sum(m == 'PATCH' for m,n,p in calls), 1)
        self.assertFalse(any('heads/main' in n for m,n,p in calls))
