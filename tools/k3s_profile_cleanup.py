#!/usr/bin/env python3
"""Remove only ticket #35's owned temporary profiling overlay, then restart K3s.

Run as a root-owned immutable systemd oneshot, never from a writable checkout.
Peer health uses the existing etcd client certificate without printing its data.
"""
import argparse
import fcntl
import ipaddress
import os
from pathlib import Path
import stat
import subprocess

OVERLAY = Path('/etc/rancher/k3s/config.yaml.d/99-ram-profiling-ticket35.yaml')
LOCK = Path('/run/lock/k3s-profile-ticket35.lock')
CONTENT = b'enable-pprof: true\n'
EXPECTED_UID = 0


def peer_serving(host):
    address = ipaddress.ip_address(host)
    authority = f'[{address}]' if address.version == 6 else str(address)
    cmd = ['curl', '-fsS', '--max-time', '4', '--http2',
           '--cacert', '/var/lib/rancher/k3s/server/tls/etcd/server-ca.crt',
           '--cert', '/var/lib/rancher/k3s/server/tls/etcd/client.crt',
           '--key', '/var/lib/rancher/k3s/server/tls/etcd/client.key',
           '-H', 'Content-Type: application/grpc', '-H', 'TE: trailers',
           '--data-binary', '@-', f'https://{authority}:2379/grpc.health.v1.Health/Check']
    try:
        result = subprocess.run(cmd, input=b'\0\0\0\0\0', capture_output=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == b'\0\0\0\0\2\x08\x01'


def cleanup(peers):
    lockfd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        lockstat = os.fstat(lockfd)
        if (not stat.S_ISREG(lockstat.st_mode) or lockstat.st_uid != EXPECTED_UID or
                lockstat.st_mode & 0o077):
            raise RuntimeError('Unexpected cleanup lease owner/type/mode')
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fd = os.open(OVERLAY, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            print('Overlay already absent; no restart requested.')
            return False
        try:
            s = os.fstat(fd)
            if (not stat.S_ISREG(s.st_mode) or s.st_uid != EXPECTED_UID or
                    s.st_mode & 0o077 or s.st_size != len(CONTENT)):
                raise RuntimeError('Unexpected profiling overlay owner/type/mode/size')
            if os.read(fd, len(CONTENT) + 1) != CONTENT:
                raise RuntimeError('Profiling overlay changed; refusing to remove it')
            current = OVERLAY.lstat()
            if (current.st_dev, current.st_ino) != (s.st_dev, s.st_ino):
                raise RuntimeError('Profiling overlay identity changed')
            # Always remove our overlay so a future normal start is safe. If
            # quorum is unavailable, defer the restart rather than risk quorum.
            healthy = all(peer_serving(peer) for peer in peers)
            OVERLAY.unlink()
            dfd = os.open(OVERLAY.parent, os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            if not healthy:
                raise RuntimeError('Overlay removed; restart deferred because peer health is unverified')
            subprocess.run(['systemctl', 'restart', '--no-block', 'k3s'], check=True, timeout=8)
            print('Owned profiling overlay removed; normal K3s restart queued.')
            return True
        finally:
            os.close(fd)
    finally:
        os.close(lockfd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--peers', nargs=2, required=True)
    args = parser.parse_args()
    peers = [str(ipaddress.ip_address(peer)) for peer in args.peers]
    if len(set(peers)) != 2:
        parser.error('Two different peer addresses are required')
    if os.geteuid() != 0:
        parser.error('Root is required')
    cleanup(peers)


if __name__ == '__main__':
    main()
