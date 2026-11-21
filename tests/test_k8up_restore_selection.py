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
TAGS = ['prometheus-tsdb', 'prometheus-replica-0']
ID = 'ab' * 32


def timestamp(delta=0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta)).isoformat()


def snapshot(path=CANARY, **changes):
    return dict({'paths': [path], 'tags': TAGS if path == STREAM else [], 'id': ID,
                 'hostname': 'kube-prometheus-stack' if path == STREAM else 'backup-validation',
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

    def exercise(self, records, source='canary', success=False):
        (self.root / 'snapshots').write_text(json.dumps(records))
        log = self.invoke(source, success)
        self.assertEqual((self.scratch / 'restored').exists(), success)
        self.assertEqual((self.scratch / 'snapshot-seconds').exists(), success and source == 'prometheus-stream')
        return log

    def test_restore_exact_sources_and_id(self):
        for source, path in [('canary', CANARY), ('prometheus-stream', STREAM)]:
            with self.subTest(source=source):
                offset_time = dt.datetime.fromisoformat(timestamp(-5)).astimezone(dt.timezone(dt.timedelta(hours=3)))
                chosen = snapshot(path, time=offset_time.isoformat())
                log = self.exercise([chosen, snapshot(path, id='c'*64, time=timestamp(-4000))], source, True)
                data = self.scratch / 'restored' / path[1:]
                self.assertEqual((data / 'marker.txt' if source == 'canary' else data).read_bytes(), b'payload')
                if source == 'prometheus-stream':
                    self.assertEqual(int((self.scratch / 'snapshot-seconds').read_text()), int(offset_time.timestamp()))
                calls = (self.root / 'calls').read_text().splitlines()
                self.assertIn(f'--host {chosen["hostname"]} --path {path}', calls[-2])
                self.assertTrue(calls[-1].endswith(' ' + ID))
                shutil.rmtree(self.scratch)
                self.assertEqual(log.decode(), f'snapshot_id={ID} time={chosen["time"]}\n')
                self.scratch.mkdir()

    def test_expected_snapshot_never_falls_back_or_restores_a_different_backup(self):
        wanted = snapshot(STREAM, time=timestamp(-100))
        newer = snapshot(STREAM, id='de' * 32)
        stale = dict(wanted, time=timestamp(-4000))
        wrong_path = dict(wanted, paths=[CANARY])
        wrong_tags = dict(wanted, tags=[])
        for expected, snapshots in ((ID, []), (ID, [newer]), (ID, [wanted, newer]),
                                    (ID, [stale]), (ID, [wrong_path]), (ID, [wrong_tags]),
                                    ('', [wanted]), (ID[:8], [wanted]), ('--latest', [wanted])):
            with self.subTest(expected=expected):
                self.env['EXPECTED_RESTIC_SNAPSHOT_ID'] = expected
                self.assertEqual(self.exercise(snapshots, 'prometheus-stream'), b'')
                shutil.rmtree(self.scratch)
                self.scratch.mkdir()
        self.assertNotIn(' restore ', (self.root / 'calls').read_text())
        self.env['EXPECTED_RESTIC_SNAPSHOT_ID'] = ID
        log = self.exercise([wanted], 'prometheus-stream', success=True)
        self.assertIn(f'snapshot_id={ID}'.encode(), log)
        calls = (self.root / 'calls').read_text().splitlines()
        self.assertTrue(calls[-2].endswith(f' snapshots --json {ID}'))
        self.assertTrue(calls[-1].endswith(f' {ID}'))

    def test_no_eligible_snapshot(self):
        good = snapshot(STREAM, time=timestamp(-400))
        for records in ([], {}, [snapshot(STREAM, time=timestamp(-4000))],
                        [snapshot(STREAM, time=timestamp(90))], [snapshot(STREAM, time='bad')],
                        [good, dict(good, id='c'*64)], [good, snapshot(STREAM, paths=[STREAM, '/data'])],
                        [good, snapshot(STREAM, tags=TAGS[:1])], [snapshot(STREAM, hostname='other')],
                        [snapshot(STREAM, id='latest')], [snapshot(STREAM, id='extra\n' + ID)],
                        [snapshot(STREAM, time='extra\n' + timestamp(-5))],
                        [snapshot(STREAM, paths=[STREAM + '.tar'])]):
            with self.subTest(records=records):
                self.assertEqual(self.exercise(records, 'prometheus-stream'), b'')
                shutil.rmtree(self.scratch)
                self.scratch.mkdir()
        self.assertNotIn(' restore ', (self.root / 'calls').read_text())

    def test_newest_canary_must_be_untagged(self):
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

    def test_reject_canary_prefix(self):
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

    def test_actual_verified_canary_directory(self):
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
        log = self.invoke('canary', True, script)
        self.assertEqual(log.decode(), f'snapshot_id={record["id"]} time={record["time"]}\n')
        restored = self.scratch / 'restored' / source.relative_to('/')
        self.assertEqual((restored / 'marker.txt').read_bytes(), marker)
        self.assertFalse((self.scratch / 'snapshot-seconds').exists())

    def test_actual_verified_stream(self):
        self.init_real_repo()
        self.restic('backup', '--stdin', '--stdin-filename', STREAM, '--host', 'kube-prometheus-stack',
                    '--tag', ','.join(TAGS), data=b'synthetic bytes')
        self.restic('backup', '--stdin', '--stdin-filename', '/other', data=b'unrelated')
        snapshots = json.loads(self.restic('snapshots', '--json', '--path', STREAM))
        self.restic('backup', '--stdin', '--stdin-filename', STREAM, '--host', 'kube-prometheus-stack',
                    '--tag', ','.join(TAGS), data=b'newer synthetic bytes')
        older, newer = sorted(json.loads(self.restic('snapshots', '--json', '--path', STREAM)),
                              key=lambda snap: snap['time'])
        self.assertEqual(older['id'], snapshots[0]['id'])
        self.assertGreater(dt.datetime.fromisoformat(newer['time']), dt.datetime.fromisoformat(older['time']))
        # Make repository writes (including a transient lock) impossible for non-root callers.
        dirs = [self.repo, *(p for p in self.repo.rglob('*') if p.is_dir())]
        for p in dirs:
            p.chmod(0o555)
            self.addCleanup(p.chmod, 0o755)
        (self.scratch / 'lost+found').mkdir()
        del self.env['RESTIC_PASSWORD_FILE']
        self.env['RESTIC_PASSWORD'] = 'disposable-test-only'
        record, = snapshots
        self.env['EXPECTED_RESTIC_SNAPSHOT_ID'] = record['id']
        log = self.invoke('prometheus-stream', True)
        self.assertEqual((self.scratch / 'restored' / STREAM[1:]).read_bytes(), b'synthetic bytes')
        self.assertEqual(log.decode(), f'snapshot_id={record["id"]} time={record["time"]}\n')
        seconds = int(dt.datetime.fromisoformat(record['time'].replace('Z', '+00:00')).timestamp())
        self.assertEqual((self.scratch / 'snapshot-seconds').read_text(), f'{seconds}\n')


if __name__ == '__main__':
    unittest.main()
