#!/usr/bin/env python3
"""Bounded host adapter for the explicitly approved ticket #35 diagnostics.

Entered through a digest-pinned, tokenless, short-lived Kubernetes Job. Never
prints arbitrary argv, environments, credential objects or host configuration.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

EXPECTED_UID = 0
OVERLAY = Path('/etc/rancher/k3s/config.yaml.d/99-ram-profiling-ticket35.yaml')
UNIT = 'k3s-profile-ticket35-cleanup'


def output(cmd):
    return subprocess.check_output(cmd, text=True, timeout=6).strip()


def safe_dir(path, create=False):
    if create and not path.exists():
        path.mkdir(mode=0o700)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != EXPECTED_UID or s.st_mode & 0o022:
        raise RuntimeError('Unsafe diagnostic directory owner/type/mode')


def target_matches(expected_ip):
    import ipaddress
    ipaddress.ip_address(expected_ip)
    addresses = json.loads(output(['ip', '-j', 'address']))
    return any(a.get('local') == expected_ip for i in addresses for a in i.get('addr_info', []))


def inspect(expected_ip):
    matches = target_matches(expected_ip)
    pid = int(output(['systemctl', 'show', 'k3s', '-p', 'MainPID', '--value']))
    if not pid:
        raise RuntimeError('No active host K3s process; unsupported service model')
    proc = Path(f'/proc/{pid}')
    # Only the whitelisted config-path setting is consumed; never dump env/argv.
    config = next((item.split(b'=', 1)[1] for item in (proc / 'environ').read_bytes().split(b'\0')
                   if item.startswith(b'K3S_CONFIG_FILE=')), None)
    launch = output(['systemctl', 'show', 'k3s', '-p', 'ExecStart', '--value'])
    option = re.search(r'--config(?:=|\s+)([^\s;]+)', launch)
    default = config in (None, b'/etc/rancher/k3s/config.yaml') and (
        option is None or option[1].strip('"\'') == '/etc/rancher/k3s/config.yaml')
    default = default and '--enable-pprof' not in launch
    fields = (proc / 'stat').read_text().rsplit(') ', 1)[1].split()
    report = {'pid': pid, 'pid_start_ticks': int(fields[19]),
              'target_matches': matches, 'default_config': default,
              'active': output(['systemctl', 'is-active', 'k3s']) == 'active',
              'kill_mode': output(['systemctl', 'show', 'k3s', '-p', 'KillMode', '--value']),
              'overlay_absent': not OVERLAY.exists() and not OVERLAY.is_symlink(),
              'rss_bytes': int((proc / 'statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')}
    cg = Path('/sys/fs/cgroup/system.slice/k3s.service')
    ms = dict((k, int(v)) for k, v in (l.split() for l in (cg / 'memory.stat').read_text().splitlines()))
    report['working_set_bytes'] = int((cg / 'memory.current').read_text()) - ms['inactive_file']
    for suffix in ['timer', 'service']:
        result = subprocess.run(['systemctl', 'show', UNIT + '.' + suffix, '-p', 'ActiveState', '--value'],
                                capture_output=True, text=True, timeout=6)
        report['cleanup_' + suffix + '_state'] = result.stdout.strip() or 'not-loaded'
    for directory in [Path('/etc'), Path('/etc/rancher'), OVERLAY.parent.parent]:
        safe_dir(directory)
    if OVERLAY.parent.exists() or OVERLAY.parent.is_symlink():
        safe_dir(OVERLAY.parent)
    return report


def install_snapshot(source):
    sha = hashlib.sha256(source).hexdigest()
    parent = Path('/run/k3s-profile-ticket35')
    safe_dir(parent, create=True)
    release = parent / sha
    safe_dir(release, create=True)
    dest = release / 'cleanup.py'
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o500)
    except FileExistsError:
        s = dest.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_uid != EXPECTED_UID or s.st_mode & 0o222:
            raise RuntimeError('Unexpected cleanup snapshot owner/type/mode')
    else:
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(source)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            raise
    if hashlib.sha256(dest.read_bytes()).hexdigest() != sha:
        raise RuntimeError('Cleanup snapshot digest mismatch')
    return dest


def publish_overlay(content):
    # Never expose a partial *.yaml file to K3s; link publishes atomically and
    # refuses replacement of any pre-existing object, including a symlink.
    stage = None
    try:
        with tempfile.NamedTemporaryFile(dir=OVERLAY.parent, prefix='.ticket35-', suffix='.tmp', delete=False) as f:
            stage = Path(f.name)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.link(stage, OVERLAY, follow_symlinks=False)
        dfd = os.open(OVERLAY.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if stage is not None:
            stage.unlink()


def activate(expected_ip, peers, source):
    import ipaddress
    peers = [str(ipaddress.ip_address(p)) for p in peers]
    if len(peers) != 2 or len(set(peers + [expected_ip])) != 3:
        raise RuntimeError('Two different non-target peers required')
    report = inspect(expected_ip)
    if not (report['target_matches'] and report['default_config'] and report['active']
            and report['kill_mode'] == 'process' and report['overlay_absent']):
        raise RuntimeError('Host preflight refused activation')
    namespace = {'__name__': 'diagnostic_cleanup'}
    exec(compile(source, '<validated-cleanup-snapshot>', 'exec'), namespace)
    if not all(namespace['peer_serving'](peer) for peer in peers):
        raise RuntimeError('Peer health gate failed; no configuration change')
    safe_dir(OVERLAY.parent, create=True)
    dest = install_snapshot(source)
    cmd = ['systemd-run', '--unit=' + UNIT, '--on-active=10min', '--timer-property=AccuracySec=1s',
           '--property=Type=oneshot', '--property=User=root', '--property=UMask=0077',
           '--property=TimeoutStartSec=30s', '--property=NoNewPrivileges=yes',
           '--property=ProtectSystem=strict', '--property=ProtectHome=yes',
           '--property=ReadWritePaths=/etc/rancher/k3s/config.yaml.d /run/lock',
           '/usr/bin/python3', str(dest), '--peers', *peers]
    subprocess.run(cmd, check=True, capture_output=True, timeout=8)
    publish_overlay(namespace['CONTENT'])
    try:
        subprocess.run(['systemctl', 'restart', '--no-block', 'k3s'], check=True, timeout=6)
    except BaseException:
        subprocess.run(['systemctl', 'start', '--no-block', UNIT + '.service'], timeout=6)
        raise
    return {'before': report, 'cleanup_sha256': hashlib.sha256(source).hexdigest(),
            'profiling_restart_queued': True, 'rollback_timer': UNIT + '.timer'}


def finish():
    if OVERLAY.exists() or OVERLAY.is_symlink():
        subprocess.run(['systemctl', 'start', '--no-block', UNIT + '.service'], check=True, timeout=6)
        # The service job now owns cleanup independently; do not keep a stale
        # timer waiting after the explicitly requested normal restart.
        subprocess.run(['systemctl', 'stop', UNIT + '.timer'], capture_output=True, timeout=6)
        return {'normal_restart_cleanup_queued': True}
    return {'overlay_already_absent': True, 'extra_restart_requested': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['inspect', 'activate', 'finish'])
    p.add_argument('--expected-ip', required=True)
    p.add_argument('--peers', nargs=2)
    p.add_argument('--cleanup-source-base64')
    args = p.parse_args()
    if os.geteuid() != 0:
        p.error('Host root is required')
    if args.action == 'inspect':
        result = inspect(args.expected_ip)
    elif args.action == 'activate':
        if not args.peers or not args.cleanup_source_base64:
            p.error('Activation requires peers and a validated cleanup snapshot')
        result = activate(args.expected_ip, args.peers, base64.b64decode(args.cleanup_source_base64, validate=True))
    else:
        # Rollback must also work if a profiling start failed (MainPID == 0).
        if not target_matches(args.expected_ip):
            raise RuntimeError('Wrong host identity')
        result = finish()
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
