"""Render a reversible digest-only override. Never change the control-plane version."""
import json
import re
import sys
from pathlib import Path

IMAGE = 'ghcr.io/dmuix/istio-proxyv2'
MARK = '              # Temporary: envoyproxy/envoy#48409; remove when the official Istio image passes the same regression and gateway checks.\n'
ANCHOR = '            - name: istio-proxy\n'


def render(text, mode, digest):
    if mode not in ('stock', 'patched') or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('invalid candidate')
    if text.count(ANCHOR) != 1:
        raise ValueError('ambiguous container')
    before, tail = text.split(ANCHOR)
    if tail.startswith(MARK):
        pattern = re.escape(MARK + '              image: ' + IMAGE + '@') + r'sha256:[0-9a-f]{64}\n'
        tail, n = re.subn('^' + pattern, '', tail, count=1)
        if n != 1:
            raise ValueError('unrecognized managed override')
    if not tail.startswith('              resources:\n') or re.search(r'^              image:', tail, re.M):
        raise ValueError('refusing to replace unmanaged image')
    override = MARK + '              image: ' + IMAGE + '@' + digest + '\n' if mode == 'patched' else ''
    return before + ANCHOR + override + tail


if __name__ == '__main__':
    mode, digest = sys.argv[1:]
    version = json.loads(Path('ci/envoy/lock.json').read_text())['istio']
    pins = re.findall(r'^    targetRevision: (\S+)$', Path('01-bootstrap/istio/app.yml').read_text(), re.M)
    if pins != [version] * 4:
        sys.exit('candidate does not match deployed chart pins')
    path = Path('01-bootstrap/istio-gateway/resources/gateway-config.yaml')
    path.write_text(render(path.read_text(), mode, digest))
