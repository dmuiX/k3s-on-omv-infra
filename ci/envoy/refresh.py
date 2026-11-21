"""Resolve a stable Istio patch release from official sources, without executing its code."""
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path


def read(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def raw(repo, ref, path):
    return read(f'https://raw.githubusercontent.com/{repo}/{ref}/{path}').decode()


def digest(ref):
    v = subprocess.check_output(['docker','buildx','imagetools','inspect',ref,'--format','{{.Manifest.Digest}}'],text=True).strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',v):
        raise ValueError('invalid registry digest')
    return ref.rsplit(':',1)[0]+'@'+v


def main():
    path=Path('ci/envoy/lock.json'); lock=json.loads(path.read_text()); old=lock['istio']; prefix=old.rsplit('.',1)[0]+'.'
    releases=json.loads(read('https://api.github.com/repos/istio/istio/releases?per_page=100'))
    versions=[r['tag_name'] for r in releases if not r['draft'] and not r['prerelease'] and re.fullmatch(re.escape(prefix)+r'\d+',r['tag_name'])]
    version=max([old,*versions],key=lambda x:int(x.rsplit('.',1)[1])); changed=version!=old
    if changed:
        deps=json.loads(raw('istio/istio',version,'istio.deps')); proxy=next(d['lastStableSHA'] for d in deps if d['name']=='PROXY_REPO_SHA');assert re.fullmatch(r'[0-9a-f]{40}',proxy)
        workspace=raw('istio/proxy',proxy,'WORKSPACE'); lock['envoy']=re.search(r'^ENVOY_SHA = "([0-9a-f]{40})"$',workspace,re.M)[1]; lock['envoy_tar_sha256']=re.search(r'^ENVOY_SHA256 = "([0-9a-f]{64})"$',workspace,re.M)[1]
        env=raw('istio/proxy',proxy,'common/scripts/setup_env.sh');tool=re.search(r'^  IMAGE_VERSION=(release-[0-9.]+-[0-9a-f]{40})$',env,re.M)[1]
        lock.update(istio=version,proxy=proxy,proxy_tar_sha256=hashlib.sha256(read(f'https://codeload.github.com/istio/proxy/tar.gz/{proxy}')).hexdigest(),toolchain=digest('registry.istio.io/testing/build-tools-proxy:'+tool),stock=digest('docker.io/istio/proxyv2:'+version+'-distroless'));path.write_text(json.dumps(lock,indent=2)+'\n')
    if 'GITHUB_OUTPUT' in os.environ:
        with open(os.environ['GITHUB_OUTPUT'],'a') as f: print('changed='+str(changed).lower(),file=f)
    print(f'Selected Istio {version} (configured minor series only)')


if __name__=='__main__':main()
