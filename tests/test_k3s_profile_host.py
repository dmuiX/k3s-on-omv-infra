#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock


def load(name):
    s = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / ('tools/' + name + '.py'))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


host = load('k3s_profile_host')
job = load('k3s_profile_job')


class HostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.overlay = self.path / 'overlay.yaml'
        for name, value in [('OVERLAY', self.overlay), ('EXPECTED_UID', os.getuid())]:
            p = patch.object(host, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_atomic_overlay_and_private_permissions(self):
        host.publish_overlay(b'enable-pprof: true\n')
        self.assertEqual(self.overlay.read_bytes(), b'enable-pprof: true\n')
        self.assertEqual(self.overlay.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.iterdir()), [self.overlay])

    def test_no_overlay_on_write_sync_failure(self):
        with patch.object(host.os, 'fsync', side_effect=OSError('synthetic sync failure')):
            with self.assertRaises(OSError):
                host.publish_overlay(b'enable-pprof: true\n')
        self.assertFalse(self.overlay.exists())
        self.assertEqual(list(self.path.iterdir()), [])

    def test_existing_overlay_or_symlink_is_never_replaced(self):
        target = self.path / 'unrelated'
        target.write_bytes(b'protected fixture')
        self.overlay.symlink_to(target)
        with self.assertRaises(FileExistsError):
            host.publish_overlay(b'enable-pprof: true\n')
        self.assertEqual(target.read_bytes(), b'protected fixture')
        self.assertTrue(self.overlay.is_symlink())

    def test_directory_boundaries(self):
        host.safe_dir(self.path)
        link = self.path / 'link'
        link.symlink_to(self.path)
        with self.assertRaises(RuntimeError):
            host.safe_dir(link)
        self.path.chmod(0o777)
        with self.assertRaises(RuntimeError):
            host.safe_dir(self.path)

    def test_wrong_target_never_activates(self):
        report = {'target_matches': False}
        with patch.object(host, 'inspect', return_value=report), patch.object(host.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                host.activate('127.0.0.1', ['127.0.0.2', '127.0.0.3'], b'')
            run.assert_not_called()
        self.assertFalse(self.overlay.exists())

    def test_wrong_peers_never_inspect_or_activate(self):
        with patch.object(host, 'inspect') as inspect:
            with self.assertRaises(RuntimeError):
                host.activate('127.0.0.1', ['127.0.0.1', '127.0.0.2'], b'')
            inspect.assert_not_called()

    def test_peer_failure_never_registers_timer_or_writes_config(self):
        report = dict(target_matches=True, default_config=True, active=True, kill_mode='process', overlay_absent=True)
        with patch.object(host, 'inspect', return_value=report), patch.object(host.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                host.activate('127.0.0.1', ['127.0.0.2', '127.0.0.3'], b'def peer_serving(peer): return False\n')
            run.assert_not_called()
        self.assertFalse(self.overlay.exists())

    def test_missing_overlay_finish_does_not_restart(self):
        with patch.object(host.subprocess, 'run') as run:
            result = host.finish()
            self.assertFalse(result['extra_restart_requested'])
            run.assert_not_called()

    def test_finish_delays_host_cleanup_until_container_exit_is_published(self):
        self.overlay.write_bytes(b'enable-pprof: true\n')
        with patch.object(host, 'install_snapshot', return_value=self.path / 'snapshot.py'), \
                patch.object(host.subprocess, 'run') as run:
            host.finish(['127.0.0.2', '127.0.0.3'], b'fixture source')
            calls = [c.args[0] for c in run.call_args_list]
            self.assertEqual(calls[0][0], 'systemd-run')
            self.assertIn('--on-active=15s', calls[0])
            self.assertEqual(calls[0][-3:], ['--peers', '127.0.0.2', '127.0.0.3'])
            self.assertEqual(calls[1], ['systemctl', 'stop', host.UNIT + '.timer'])
            self.assertNotIn(['systemctl', 'restart', '--no-block', 'k3s'], calls)

    def test_timer_registered_before_publish_and_restart(self):
        report = dict(target_matches=True, default_config=True, active=True, kill_mode='process', overlay_absent=True)
        events = []
        src = b'CONTENT=b"enable-pprof: true\\n"\ndef peer_serving(peer): return True\n'
        def run(cmd, **kwargs):
            events.append('timer' if cmd[0]=='systemd-run' else 'restart')
            return Mock(returncode=0)
        def publish(content):
            events.append('publish')
        with patch.object(host, 'inspect', return_value=report), patch.object(host, 'safe_dir'), \
                patch.object(host, 'install_snapshot', return_value=self.path / 'snapshot.py'), \
                patch.object(host, 'publish_overlay', side_effect=publish), \
                patch.object(host.subprocess, 'run', side_effect=run):
            result = host.activate('127.0.0.1', ['127.0.0.2', '127.0.0.3'], src)
        self.assertEqual(events, ['timer', 'publish', 'restart'])
        self.assertTrue(result['profiling_restart_queued'])


class JobTests(unittest.TestCase):
    def render(self, **kwargs):
        args = dict(name='test-diagnostic', node='test-node', image='docker.io/example/image@sha256:'+'a'*64,
                    action='inspect', expected_ip='127.0.0.1', peers=['127.0.0.2', '127.0.0.3'],
                    host_source=b'print("fixture")', cleanup_source=b'print("fixture")')
        args.update(kwargs)
        return job.build_job(**args)

    def test_bounded_tokenless_host_entrypoint(self):
        obj = self.render()
        spec = obj['spec']['template']['spec']
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertNotIn('volumes', spec)
        self.assertEqual(spec['nodeName'], 'test-node')
        self.assertEqual(spec['restartPolicy'], 'Never')
        self.assertEqual(obj['spec']['backoffLimit'], 0)
        self.assertEqual(obj['spec']['activeDeadlineSeconds'], 180)
        self.assertIn('--root=/proc/1/root', spec['containers'][0]['command'])
        self.assertEqual(spec['containers'][0]['command'][-3:], ['inspect', '--expected-ip', '127.0.0.1'])

    def test_unpinned_image_and_shell_input_refused(self):
        for kwargs in [dict(image='example:latest'), dict(node='test;evil'), dict(action='arbitrary'),
                       dict(peers=['127.0.0.1', '127.0.0.2'])]:
            with self.assertRaises(ValueError):
                self.render(**kwargs)


if __name__ == '__main__':
    unittest.main()
