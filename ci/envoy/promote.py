"""Render a reversible digest-only override. Never change the control-plane version."""
import json
import re
import sys
from pathlib import Path

IMAGE = 'ghcr.io/dmuix/istio-proxyv2'
MARK = '              # Temporary: envoyproxy/envoy#48409; remove when the official Istio image passes the same regression and gateway checks.\n'
STOCK = 'docker.io/istio/proxyv2'
STOCK_MARK = '              # Official Istio gateway; pinned to the reviewed multiarch manifest digest.\n'
ANCHOR = '            - name: istio-proxy\n'


TAG = r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}'


def render(text, mode, digest, tag):
    if not re.fullmatch(TAG, tag):
        raise ValueError('invalid version comment')
    if mode not in ('stock', 'patched') or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('invalid candidate')
    if text.count(ANCHOR) != 1:
        raise ValueError('ambiguous container')
    before, tail = text.split(ANCHOR)
    for marker, repo in ((MARK, IMAGE), (STOCK_MARK, STOCK)):
        if tail.startswith(marker):
            pattern = re.escape(marker + '              image: ' + repo + '@') + rf'sha256:[0-9a-f]{{64}}(?: # {TAG})?\n'
            tail, n = re.subn('^' + pattern, '', tail, count=1)
            if n != 1:
                raise ValueError('unrecognized managed override')
            break
    if not tail.startswith('              resources:\n') or re.search(r'^              image:', tail, re.M):
        raise ValueError('refusing to replace unmanaged image')
    marker, repo = (MARK, IMAGE) if mode == 'patched' else (STOCK_MARK, STOCK)
    override = marker + '              image: ' + repo + '@' + digest + ' # ' + tag + '\n'
    return before + ANCHOR + override + tail


if __name__ == '__main__':
    mode, digest, tag = sys.argv[1:]
    lock = json.loads(Path('ci/envoy/lock.json').read_text())
    version = lock['istio']
    if mode == 'stock' and (lock['stock'] != STOCK + '@' + digest or lock['stock_tag'] != tag):
        sys.exit('stock digest does not match official image lock')
    pins = re.findall(r'^    targetRevision: (\S+)$', Path('01-bootstrap/istio/app.yml').read_text(), re.M)
    if pins != [version] * 4:
        sys.exit('candidate does not match deployed chart pins')
    path = Path('01-bootstrap/istio-gateway/resources/gateway-config.yaml')
    path.write_text(render(path.read_text(), mode, digest, tag))
