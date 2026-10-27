"""Offline proxy migration: complete pre-migration objects, not selected fields."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest

import yaml

INFRA = Path(__file__).resolve().parents[1]
LIVE = Path(os.environ.get('PLATFORM_LIVE_REPO', INFRA.parent / 'k3s-on-omv-live'))
CHART = INFRA / 'charts/oidc-proxy'
BASELINE = json.loads((INFRA / 'tests/fixtures/oidc-proxy/baseline.json').read_text())


def load(path):
    return yaml.safe_load(path.read_text())


def identity(doc):
    return (doc['apiVersion'], doc['kind'], doc['metadata'].get('namespace', 'longhorn'),
            doc['metadata']['name'])


def render(values):
    return subprocess.run(['helm', 'template', 'longhorn-oidc-proxy', str(CHART), '-f', '-'],
                          input=yaml.safe_dump(values), text=True, capture_output=True, timeout=30)


class OIDCProxy(unittest.TestCase):
    def values(self):
        return load(LIVE / 'infra/longhorn/proxy-values.yml')

    def documents(self):
        result = render(self.values())
        self.assertEqual(result.returncode, 0, 'Proxy chart must render')
        return [d for d in yaml.safe_load_all(result.stdout) if d]

    def test_complete_original_resources_equal(self):
        docs = self.documents()
        self.assertCountEqual([d['kind'] for d in docs], ['Deployment', 'Service', 'ConfigMap'])
        for original in BASELINE['resources']:
            actual, = [d for d in docs if identity(d) == identity(original)]
            self.assertEqual(actual, original)
        config, = [d for d in docs if d['kind'] == 'ConfigMap']
        # Hash the WHOLE original document, not just the two URL values.
        actual = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        self.assertEqual(actual, BASELINE['configMapDigest'])

    def test_urls_required_independently(self):
        for field in ('issuerURL', 'redirectURL'):
            for value in ('', None):
                with self.subTest(field=field, value=value):
                    values = copy.deepcopy(self.values())
                    values[field] = value
                    self.assertNotEqual(render(values).returncode, 0)

    def test_one_owner_across_application_sources(self):
        app = load(INFRA / '06-platform/longhorn-config/app.yml')
        sources = app['spec']['sources']
        live, = [s for s in sources if s.get('ref') == 'live']
        self.assertNotIn('path', live)
        proxy, = [s for s in sources if s.get('path') == 'charts/oidc-proxy']
        self.assertEqual(proxy['repoURL'], 'https://github.com/dmuiX/k3s-on-omv-infra.git')
        self.assertEqual(proxy['helm']['valueFiles'], ['$live/infra/longhorn/proxy-values.yml'])
        self.assertFalse((LIVE / 'infra/longhorn/kustomization.yaml').exists())
        self.assertFalse((LIVE / 'infra/longhorn/configmap.yaml').exists())
        docs = []
        for source in sources:
            if not source.get('path'):
                continue
            path = INFRA / source['path']
            if 'helm' in source:
                command = ['helm', 'template', source['helm']['releaseName'], str(path)]
                for value in source['helm']['valueFiles']:
                    command += ['-f', str(LIVE / value.removeprefix('$live/'))]
                result = subprocess.run(command, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, 'Application chart must render')
                docs.extend(d for d in yaml.safe_load_all(result.stdout) if d)
            else:
                for file in path.glob('*.yaml'):
                    docs.extend(d for d in yaml.safe_load_all(file.read_text()) if d)
        identities = [identity(d) for d in docs]
        self.assertEqual(len(identities), len(set(identities)), 'No duplicate resource owners')
        for doc in self.documents():
            self.assertEqual(identities.count(identity(doc)), 1)

    def test_credentials_remain_outside_chart(self):
        path = INFRA / '06-platform/longhorn-config/resources/oidc-proxy.yaml'
        docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
        self.assertEqual([d['kind'] for d in docs],
                         ['ServiceAccount', 'VaultAuth', 'VaultStaticSecret'])
        # Normalize only helper syntax and transport against the original baseline.
        text = path.read_text()
        auth_header = 'apiVersion: secrets.hashicorp.com/v1beta1\nkind: VaultAuth\n'
        connection = ('apiVersion: secrets.hashicorp.com/v1beta1\n'
                      'kind: VaultConnection\nmetadata:\n'
                      '  name: openbao-longhorn\n  namespace: longhorn\n'
                      'spec:\n  address: "http://openbao-active.openbao.svc.cluster.local:8200"\n---\n')
        self.assertEqual(text.count(auth_header), 1)
        text = text.replace(auth_header, connection + auth_header, 1)
        qualified = 'vaultConnectionRef: vault-secrets-operator/default'
        self.assertEqual(text.count(qualified), 1)
        text = text.replace(qualified, 'vaultConnectionRef: openbao-longhorn', 1)
        reference = ('      transformationRefs:\n'
                     '        - name: required-openbao-value\n'
                     '          namespace: vault-secrets-operator\n')
        self.assertEqual(text.count(reference), 1)
        text = text.replace(reference, '', 1)
        for key in ('client-id', 'client-secret'):
            shared = '{{- template "requiredValue" (dict "key" "' + key + '" "value" (get .Secrets "' + key + '")) -}}'
            original = ('{{- $value := get .Secrets "' + key + '" -}}\n'
                        '            {{- if empty $value -}}\n'
                        '            {{- fail "missing required OpenBao key: ' + key + '" -}}\n'
                        '            {{- end -}}\n'
                        '            {{- $value -}}')
            self.assertEqual(text.count(shared), 1)
            text = text.replace(shared, original, 1)
        shared = '{{- template "requiredValue" (dict "key" "cookie-secret" "value" $value) -}}'
        self.assertEqual(text.count(shared), 1)
        text = text.replace(shared, '{{- fail "missing required OpenBao key: cookie-secret" -}}', 1)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), BASELINE['credentialsTextDigest'])


if __name__ == '__main__':
    unittest.main()
