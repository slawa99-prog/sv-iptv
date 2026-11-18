"""Exercise updater preflight without root, a real VPS, or network requests."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


UPDATE = Path(__file__).resolve().parents[1] / 'server' / 'update.sh'


class ServerUpdatePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / 'service-calls'
        self.env = {**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
                    'UPDATE_CALL_LOG': str(self.log)}
        self.mock('id', 'echo 0')
        self.mock('nproc', 'echo 2')
        self.mock('awk', 'echo 8388608')
        self.mock('systemctl', 'printf "%s\\n" "$*" >> "$UPDATE_CALL_LOG"; exit 77')

    def mock(self, name, body):
        path = self.root / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def run_update(self, *args):
        return subprocess.run(['bash', str(UPDATE), *args], env=self.env,
                              text=True, capture_output=True, timeout=10)

    def test_invalid_worker_or_revision_does_not_contact_systemd(self):
        for args in (('a'*40, '0'), ('a'*40, '9'), ('a'*40, '6', 'extra'), ('main', '6')):
            with self.subTest(args=args):
                result = self.run_update(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())

    def test_six_workers_refused_before_service_stop_until_upgrade_is_visible(self):
        for cpus, memory in ((2, 8388608), (4, 4194304)):
            with self.subTest(cpus=cpus, memory=memory):
                self.mock('nproc', f'echo {cpus}')
                self.mock('awk', f'echo {memory}')
                result = self.run_update('a'*40, '6')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Current check was not stopped', result.stderr)
                self.assertFalse(self.log.exists())

    def test_upgrade_visible_and_existing_worker_mode_pass_preflight(self):
        self.mock('nproc', 'echo 4')
        self.mock('ffmpeg', 'exit 0')
        for args in (('a'*40, '6'), ('a'*40, '8'), ('a'*40,)):
            with self.subTest(args=args):
                self.log.unlink(missing_ok=True)
                result = self.run_update(*args)
                # Deliberately stop at the first read-only service existence check.
                self.assertEqual(result.returncode, 77, result.stderr)
                self.assertEqual(self.log.read_text(), 'cat sv-iptv-ru.service\n')
