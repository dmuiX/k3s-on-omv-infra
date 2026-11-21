"""Offline restore contract, including a disposable local repo using pinned restic."""
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'adapters/k8up-restore/restore.sh'
CANARY = '/data/k8up-validation-canary'
STREAM = '/kube-prometheus-stack-snapshottar'
HOST = 'kube-prometheus-stack'
TAGS = ['prometheus-tsdb', 'prometheus-replica-0']
RUN_TAG = 'backup-run-b97ee5b0-f083-4d08-a789-4964a4ddef54'
EXPECTED = '20261015T120000Z-0123456789abcdef'
ID = 'ab' * 32


def timestamp(delta=0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta)).isoformat()


def snapshot(path=CANARY, **changes):
    return dict({'paths': [path], 'tags': TAGS + [RUN_TAG] if path == STREAM else [], 'id': ID,
                 'hostname': HOST if path == STREAM else 'backup-validation',
                 'time': timestamp(-5)}, **changes)


class Restore(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()
        self.repo = self.root / 'repository'
        self.password = self.root / 'password'
        self.password.write_text('disposable-test-only')
        self.env = dict(PATH=f'{self.root}:{os.environ["PATH"]}', SCRATCH_DIR=str(self.scratch),
                        RESTIC_PASSWORD_FILE=str(self.password), RESTIC_REPOSITORY=str(self.repo))
        stub = self.root / 'restic'
        stub.write_text('''#!/bin/sh
set -eu
printf '%s\\n' "$*" >>../calls
[ "$1" = --repo ] && [ "$2" = "local:$RESTIC_REPOSITORY" ] && [ "$3 $4" = '--no-lock --no-cache' ] || exit 42
shift 4
case $1 in
 snapshots) cat ../snapshots; [ ! -e ../list-error ] ;;
 restore) [ "$2 $3" = '--verify --include' ] && [ "$5" = --target ] || exit 43
    dest=$6$4
    if [ "$4" = /data/k8up-validation-canary ]; then mkdir -p "$dest"; dest=$dest/marker.txt; else mkdir -p "$6"; fi
    printf 'payload' >"$dest"
    [ ! -e ../empty ] || : >"$dest"
    echo 'sensitive filename' >&2
    [ ! -e ../verify-error ] ;;
 *) exit 44 ;;
esac
''')
        stub.chmod(0o700)

    def invoke(self, source, success, script=SCRIPT):
        proc = subprocess.run(['sh', script, source, '3600'], env=self.env, capture_output=True)
        self.assertEqual(proc.returncode == 0, success, proc.stderr)
        self.assertNotIn(b'PASS', proc.stdout + proc.stderr)
        self.assertEqual(b'result=FAIL' in proc.stderr, not success)
        if proc.stdout:
            self.assertRegex(proc.stdout, rb'\Asnapshot_id=[0-9a-f]{64} time=[0-9T:Z.+-]{20,35}\n\Z')
        self.assertNotIn(b'sensitive filename', proc.stderr)
        return proc.stdout

    def exercise(self, records, source='backup-validation', success=False):
        (self.root / 'snapshots').write_text(json.dumps(records))
        log = self.invoke(source, success)
        self.assertEqual((self.scratch / 'restored').exists(), success)
        self.assertEqual((self.scratch / 'snapshot-seconds').exists(), success and source == 'prometheus-stream')
        return log

    def test_restore_newest_fixture_by_full_id_regardless_of_prometheus_pins(self):
        self.pin('--latest', '')
        chosen = snapshot()
        log = self.exercise([chosen, snapshot(id='c'*64, time=timestamp(-4000))], success=True)
        self.assertEqual((self.scratch / 'restored' / CANARY[1:] / 'marker.txt').read_bytes(), b'payload')
        listing, restore = (self.root / 'calls').read_text().splitlines()
        self.assertTrue(listing.endswith(f' snapshots --json --host backup-validation --path {CANARY} --latest 2'))
        self.assertTrue(restore.endswith(' ' + ID))
        self.assertEqual(log.decode(), f'snapshot_id={ID} time={chosen["time"]}\n')

    def pin(self, tag: str = RUN_TAG, expected: str = EXPECTED) -> None:
        self.env.update(K8UP_PROMETHEUS_RUN_TAG=tag, PROMETHEUS_EXPECTED_SNAPSHOT=expected)

    def test_pinned_selects_only_exact_tag_and_full_id(self) -> None:
        self.pin()
        offset_time = dt.datetime.fromisoformat(timestamp(-5)).astimezone(dt.timezone(dt.timedelta(hours=3)))
        record = snapshot(STREAM, time=offset_time.isoformat(), tags=[TAGS[0], RUN_TAG, TAGS[1]])
        self.assertEqual(self.exercise([record], 'prometheus-stream', True),
                         f'snapshot_id={ID} time={record["time"]}\n'.encode())
        listing, restore = (self.root / 'calls').read_text().splitlines()
        self.assertTrue(listing.endswith(f' snapshots --json --host {HOST} --path {STREAM} --tag {RUN_TAG}'))
        self.assertNotIn('--latest', listing)
        self.assertTrue(restore.endswith(' ' + ID))
        self.assertEqual(int((self.scratch / 'snapshot-seconds').read_text()), int(offset_time.timestamp()))

    def test_missing_or_invalid_pins_stop_before_scratch_and_repository_access(self) -> None:
        for tag, expected in ((None, None), ('', ''), (None, EXPECTED), (RUN_TAG, None),
                              (RUN_TAG, ''), ('', EXPECTED), (RUN_TAG, EXPECTED + '\n'),
                              (RUN_TAG, '20261015T120000Z-abc'), (RUN_TAG + '\n', EXPECTED),
                              (RUN_TAG + ',' + TAGS[0], EXPECTED), ('--latest', EXPECTED),
                              ('backup-run-not-a-uuid', EXPECTED)):
            with self.subTest(tag=tag, expected=expected):
                for name, value in (('K8UP_PROMETHEUS_RUN_TAG', tag), ('PROMETHEUS_EXPECTED_SNAPSHOT', expected)):
                    if value is None:
                        self.env.pop(name, None)
                    else:
                        self.env[name] = value
                self.assertEqual(self.exercise([snapshot(STREAM, tags=TAGS)], 'prometheus-stream'), b'')
                self.assertFalse((self.root / 'calls').exists())
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_inexact_or_ambiguous_pinned_listing_never_restores(self) -> None:
        self.pin()
        good = snapshot(STREAM)
        for records in ([], {}, [good, dict(good, id='c'*64)], [good, dict(good, id='c'*64, time=timestamp(-60))],
                        [dict(good, id=ID[:8])], [dict(good, id='latest')], [dict(good, id='extra\n' + ID)],
                        [dict(good, time=timestamp(-4000))], [dict(good, time=timestamp(90))],
                        [dict(good, time='bad')], [dict(good, time='extra\n' + timestamp(-5))],
                        [dict(good, paths=[STREAM, '/other'])], [dict(good, paths=[STREAM + '.tar'])],
                        [dict(good, hostname='other')], [dict(good, tags=TAGS)],
                        [dict(good, tags=TAGS[:1] + [RUN_TAG])], [dict(good, tags=TAGS + [RUN_TAG, 'extra'])],
                        [dict(good, tags=TAGS + [RUN_TAG, RUN_TAG])]):
            with self.subTest(records=records):
                self.assertEqual(self.exercise(records, 'prometheus-stream'), b'')
                shutil.rmtree(self.scratch)
                self.scratch.mkdir()
        self.assertNotIn(' restore ', (self.root / 'calls').read_text())

    def test_newest_fixture_must_be_untagged(self):
        older = snapshot(id='c'*64, time=timestamp(-400))
        for tags in (['manual'], ['unexpected'], TAGS, False, None, ''):
            with self.subTest(tags=tags):
                self.assertEqual(self.exercise([older, snapshot(tags=tags)]), b'')
                shutil.rmtree(self.scratch)
                self.scratch.mkdir()
        self.assertNotIn(' restore ', (self.root / 'calls').read_text())

    def test_reject_invalid_calendar_date(self):
        # A normalizing date parser could roll this older record into March
        # and silently accept the newer one instead of failing the listing.
        self.assertEqual(self.exercise([snapshot(time='2025-02-29T04:17:00Z'), snapshot()]), b'')
        self.assertNotIn(' restore ', (self.root / 'calls').read_text())

    def test_reject_fixture_path_prefix(self):
        self.exercise([snapshot(CANARY + '-other')])
        self.assertNotIn(' restore ', (self.root / 'calls').read_text())

    def test_failures_never_publish(self):
        for flag in ['list-error', 'verify-error', 'empty']:
            with self.subTest(flag=flag):
                marker = self.root / flag
                marker.touch()
                log = self.exercise([snapshot()])
                self.assertEqual(b'snapshot_id=' in log, flag != 'list-error')
                marker.unlink()
                shutil.rmtree(self.scratch)
                self.scratch.mkdir()
        (self.scratch / '.stale').touch()
        self.exercise([snapshot()])

    def restic(self, *args, data=None):
        return subprocess.run(['restic', '--no-cache', *args], input=data, env=self.env,
                              check=True, capture_output=True).stdout

    def init_real_repo(self):
        self.env['PATH'] = os.environ['PATH']  # No stub. Never use caller repo or credentials.
        self.assertEqual(self.restic('version').split()[:2], [b'restic', b'0.19.0'])
        self.restic('init')

    def test_actual_verified_fixture_directory(self):
        self.init_real_repo()
        # Restic records the absolute source path even for a relative backup.
        # Relocate only the fixed source in a test copy; never create host /data
        # or give the deployed script a configurable source root.
        source = self.root.resolve() / CANARY[1:]
        source.mkdir(parents=True)
        marker = b'k8up-validation-canary:v1\n'
        (source / 'marker.txt').write_bytes(marker)
        script = self.root / 'restore.sh'
        assignment = 'path=' + CANARY
        original = SCRIPT.read_text()
        self.assertEqual(original.count(assignment), 1)
        script.write_text(original.replace(assignment, 'path=' + shlex.quote(str(source))))
        self.restic('backup', '--host', 'backup-validation', str(source))
        record, = json.loads(self.restic('snapshots', '--json'))
        self.assertEqual(record['paths'], [str(source)])
        self.assertNotIn('tags', record)
        shutil.rmtree(source)
        log = self.invoke('backup-validation', True, script)
        self.assertEqual(log.decode(), f'snapshot_id={record["id"]} time={record["time"]}\n')
        restored = self.scratch / 'restored' / source.relative_to('/')
        self.assertEqual((restored / 'marker.txt').read_bytes(), marker)
        self.assertFalse((self.scratch / 'snapshot-seconds').exists())

    def backup_stream(self, payload: bytes, tags: list[str], *, host: str = HOST, path: str = STREAM) -> None:
        self.restic('backup', '--stdin', '--stdin-filename', path, '--host', host,
                    '--tag', ','.join(tags), data=payload)

    def test_actual_exact_tag_restore_ignores_newer_legacy_and_other_sources(self) -> None:
        self.init_real_repo()
        self.pin()
        tags = TAGS + [RUN_TAG]
        self.backup_stream(b'exact artifact', tags)
        expected, = json.loads(self.restic('snapshots', '--json'))
        self.backup_stream(b'newer legacy artifact', TAGS)
        self.backup_stream(b'wrong path', tags, path='/other')
        self.backup_stream(b'wrong host', tags, host='other')
        log = self.invoke('prometheus-stream', True)
        self.assertEqual(log.decode(), f'snapshot_id={expected["id"]} time={expected["time"]}\n')
        self.assertEqual((self.scratch / 'restored' / STREAM[1:]).read_bytes(), b'exact artifact')

    def test_actual_missing_or_extra_tag_never_falls_back(self) -> None:
        self.init_real_repo()
        self.pin()
        self.backup_stream(b'legacy artifact', TAGS)
        self.backup_stream(b'lookalike tag', TAGS + [RUN_TAG + '0'])
        self.assertEqual(self.invoke('prometheus-stream', False), b'')
        self.assertFalse((self.scratch / '.stage/tree').exists())
        shutil.rmtree(self.scratch)
        self.scratch.mkdir()
        self.backup_stream(b'additional tag', TAGS + [RUN_TAG, 'extra'])
        self.assertEqual(self.invoke('prometheus-stream', False), b'')
        self.assertFalse((self.scratch / '.stage/tree').exists())

    def test_actual_duplicate_run_tag_never_chooses_newest(self) -> None:
        self.init_real_repo()
        self.pin()
        self.backup_stream(b'first artifact', TAGS + [RUN_TAG])
        self.backup_stream(b'second artifact', TAGS + [RUN_TAG])
        snapshots = json.loads(self.restic('snapshots', '--json', '--tag', RUN_TAG))
        self.assertEqual(len({record['time'] for record in snapshots}), 2)
        self.assertEqual(self.invoke('prometheus-stream', False), b'')
        self.assertFalse((self.scratch / '.stage/tree').exists())

    def test_actual_unpinned_stream_never_restores_eligible_legacy_backup(self) -> None:
        self.init_real_repo()
        self.backup_stream(b'eligible legacy artifact', TAGS)
        self.assertEqual(self.invoke('prometheus-stream', False), b'')  # Both env vars unset.
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.pin('', '')  # Missing Pod annotations produce empty Downward API values.
        self.assertEqual(self.invoke('prometheus-stream', False), b'')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_actual_verified_pinned_stream_on_read_only_repository(self) -> None:
        self.init_real_repo()
        self.pin()
        self.backup_stream(b'synthetic bytes', TAGS + [RUN_TAG])
        snapshots = json.loads(self.restic('snapshots', '--json', '--path', STREAM))
        # Make repository writes (including a transient lock) impossible for non-root callers.
        dirs = [self.repo, *(p for p in self.repo.rglob('*') if p.is_dir())]
        for p in dirs:
            p.chmod(0o555)
            self.addCleanup(p.chmod, 0o755)
        (self.scratch / 'lost+found').mkdir()
        del self.env['RESTIC_PASSWORD_FILE']
        self.env['RESTIC_PASSWORD'] = 'disposable-test-only'
        log = self.invoke('prometheus-stream', True)
        self.assertEqual((self.scratch / 'restored' / STREAM[1:]).read_bytes(), b'synthetic bytes')
        record, = snapshots
        self.assertEqual(log.decode(), f'snapshot_id={record["id"]} time={record["time"]}\n')
        seconds = int(dt.datetime.fromisoformat(record['time'].replace('Z', '+00:00')).timestamp())
        self.assertEqual((self.scratch / 'snapshot-seconds').read_text(), f'{seconds}\n')


if __name__ == '__main__':
    unittest.main()
