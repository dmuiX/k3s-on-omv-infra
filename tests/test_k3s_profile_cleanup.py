#!/usr/bin/env python3
"""No host service or production file is touched by these regression tests."""
import importlib.util
import fcntl
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).resolve().parents[1] / 'tools/k3s_profile_cleanup.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.file = self.directory / 'profile.yaml'
        for key, value in [('OVERLAY', self.file), ('LOCK', self.directory / 'lock'), ('EXPECTED_UID', os.getuid())]:
            p = patch.object(m, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.file.write_bytes(m.CONTENT)
        self.file.chmod(0o600)

    def test_removes_only_overlay_and_queues_one_normal_restart(self):
        with patch.object(m, 'peer_serving', return_value=True), patch.object(m.subprocess, 'run') as run:
            self.assertTrue(m.cleanup(['127.0.0.2', '127.0.0.3']))
            self.assertFalse(self.file.exists())
            run.assert_called_once_with(['systemctl', 'restart', '--no-block', 'k3s'], check=True, timeout=8)
            self.assertFalse(m.cleanup(['127.0.0.2', '127.0.0.3']))
            self.assertEqual(run.call_count, 1)

    def test_no_quorum_removes_our_overlay_but_does_not_restart(self):
        with patch.object(m, 'peer_serving', return_value=False), patch.object(m.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'restart deferred'):
                m.cleanup(['127.0.0.2', '127.0.0.3'])
            self.assertFalse(self.file.exists())
            run.assert_not_called()

    def test_does_not_remove_changed_overlay(self):
        self.file.write_bytes(b'enable-pprof: false\n')
        with patch.object(m.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                m.cleanup(['127.0.0.2', '127.0.0.3'])
            self.assertTrue(self.file.exists())
            run.assert_not_called()

    def test_refuses_symlink_and_preserves_target(self):
        target = self.directory / 'unrelated'
        target.write_bytes(m.CONTENT)
        self.file.unlink()
        self.file.symlink_to(target)
        with self.assertRaises(OSError):
            m.cleanup(['127.0.0.2', '127.0.0.3'])
        self.assertEqual(target.read_bytes(), m.CONTENT)

    def test_refuses_world_writable_overlay(self):
        self.file.chmod(0o666)
        with self.assertRaises(RuntimeError):
            m.cleanup(['127.0.0.2', '127.0.0.3'])
        self.assertTrue(self.file.exists())

    def test_health_transport_timeout_fails_closed(self):
        with patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('curl', 6)):
            self.assertFalse(m.peer_serving('127.0.0.2'))

    def test_busy_cleanup_lease_does_not_touch_overlay(self):
        with m.LOCK.open('w') as lock:
            m.LOCK.chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(BlockingIOError):
                m.cleanup(['127.0.0.2', '127.0.0.3'])
        self.assertTrue(self.file.exists())

    def test_exact_health_reply_required(self):
        with patch.object(m.subprocess, 'run', return_value=Mock(returncode=0, stdout=b'\0\0\0\0\2\x08\x01')):
            self.assertTrue(m.peer_serving('127.0.0.2'))
        with patch.object(m.subprocess, 'run', return_value=Mock(returncode=0, stdout=b'other response')):
            self.assertFalse(m.peer_serving('127.0.0.2'))
        with self.assertRaises(ValueError):
            m.peer_serving('127.0.0.2; arbitrary-command')


if __name__ == '__main__':
    unittest.main()
