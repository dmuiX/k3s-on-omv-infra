#!/usr/bin/env python3
"""Build one bounded, immutable, tokenless host diagnostic Job (no daemon)."""
import base64
import hashlib
import ipaddress
import re


def build_job(name, node, image, action, expected_ip, peers, host_source, cleanup_source):
    if action not in {'inspect', 'activate', 'finish'}:
        raise ValueError('Unsupported diagnostic action')
    for value in [name, node]:
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value):
            raise ValueError('Unsafe Job/node name')
    if not re.fullmatch(r'[a-z0-9./_-]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('A digest-pinned existing trusted image is required')
    expected_ip = str(ipaddress.ip_address(expected_ip))
    peers = [str(ipaddress.ip_address(peer)) for peer in peers]
    if len(peers) != 2 or len(set(peers + [expected_ip])) != 3:
        raise ValueError('Exactly two different non-target peers required')
    args = [action, '--expected-ip', expected_ip]
    if action == 'activate':
        args += ['--peers', *peers, '--cleanup-source-base64', base64.b64encode(cleanup_source).decode()]
    command = ['nsenter', '--target', '1', '--mount', '--uts', '--ipc', '--net', '--pid',
               '--root=/proc/1/root', '--wd=/', '--', '/usr/bin/python3', '-B', '-c', host_source.decode(), *args]
    labels = {'app.kubernetes.io/name': 'k3s-ram-diagnostics', 'ram-diagnostics/action': action}
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'name': name, 'namespace': 'kube-system', 'labels': labels,
                         'annotations': {'ram-diagnostics/host-source-sha256': hashlib.sha256(host_source).hexdigest(),
                                         'ram-diagnostics/cleanup-source-sha256': hashlib.sha256(cleanup_source).hexdigest()}},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 45, 'ttlSecondsAfterFinished': 300,
                     'parallelism': 1, 'completions': 1,
                     'template': {'metadata': {'labels': labels},
                                  'spec': {'nodeName': node, 'hostPID': True, 'hostNetwork': True,
                                           'dnsPolicy': 'ClusterFirstWithHostNet',
                                           'automountServiceAccountToken': False, 'restartPolicy': 'Never',
                                           'tolerations': [{'operator': 'Exists'}],
                                           'containers': [{'name': 'diagnostic', 'image': image,
                                                           'imagePullPolicy': 'IfNotPresent', 'command': command,
                                                           'securityContext': {'privileged': True, 'runAsUser': 0},
                                                           'resources': {'requests': {'cpu': '10m', 'memory': '24Mi'},
                                                                         'limits': {'cpu': '500m', 'memory': '128Mi'}}}]}}}}
