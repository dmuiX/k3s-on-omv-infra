"""Shared required-value contracts and offline rendering with the upstream VSO engine."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
APPS = Path(os.environ.get('PLATFORM_APPS_REPO', ROOT.parent / 'k3s-on-omv-apps'))
REFERENCE = [{'name': 'required-openbao-value', 'namespace': 'vault-secrets-operator'}]
HELPER = yaml.safe_load((ROOT / '02-controllers/vault-secrets-operator/resources/required-values.yaml').read_text())
VSO_SOURCE = os.environ.get('VSO_TEMPLATE_SOURCE')
VSO_CHART_SOURCE = os.environ.get('VSO_CHART_SOURCE')


def consumers(documents):
    return [d for d in documents if isinstance(d, dict) and d.get('kind') in (
        'VaultStaticSecret', 'VaultDynamicSecret', 'VaultPKISecret', 'HCPVaultSecretsApp')]


def desired_documents():
    for root in (ROOT, APPS, ROOT.parent / 'k3s-on-omv-live'):
        for path in sorted(root.rglob('*')):
            if path.suffix not in ('.yaml', '.yml') or any(part in path.relative_to(root).parts
                    for part in ('fixtures', 'charts', 'chart', '.git')):
                continue
            yield from (d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict))


def chart_documents():
    from test_postgres_consumer import render, values
    for consumer in ('radar', 'grafana'):
        yield from yaml.safe_load_all(render(values(consumer)).stdout)
    path = APPS / 'tests/test_audiobookshelf_chart.py'
    spec = importlib.util.spec_from_file_location('apps_audiobookshelf_contracts', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield from module.resources(module.oidc_fixture())


def required_keys(text):
    return set(re.findall(r'dict "key" "([^"]+)" "value"', text))


HARNESS = r'''
package main
import (
    "encoding/json"
    "os"
    "sort"
    vso "github.com/hashicorp/vault-secrets-operator/template"
)
func main() {
    var input struct {
        Sources []struct { Name, Text string }
        Templates map[string]struct { Text string }
        Values map[string]any
    }
    if err := json.NewDecoder(os.Stdin).Decode(&input); err != nil { panic(err) }
    t := vso.NewSecretTemplate("")
    for _, source := range input.Sources {
        if err := t.Parse(source.Name, source.Text); err != nil { panic(err) }
    }
    keys := make([]string, 0, len(input.Templates))
    for key := range input.Templates { keys = append(keys, key) }
    sort.Strings(keys)
    for _, key := range keys {
        if err := t.Parse(key, input.Templates[key].Text); err != nil { panic(err) }
    }
    result := struct { Data map[string]string; Error string }{Data: map[string]string{}}
    for _, key := range keys {
        value, err := t.ExecuteTemplate(key, map[string]any{"Secrets": input.Values})
        if err != nil { result.Data = nil; result.Error = err.Error(); break }
        result.Data[key] = string(value)
    }
    if err := json.NewEncoder(os.Stdout).Encode(result); err != nil { panic(err) }
}
'''


class RequiredValueContracts(unittest.TestCase):
    def test_single_source_only_helper_owned_by_early_operator(self):
        helpers = [d for d in desired_documents() if d.get('kind') == 'SecretTransformation']
        self.assertEqual(helpers, [HELPER])
        self.assertEqual(HELPER['metadata']['namespace'], 'vault-secrets-operator')
        self.assertNotIn('templates', HELPER['spec'])
        self.assertEqual(len(HELPER['spec']['sourceTemplates']), 1)
        app = yaml.safe_load((ROOT / '02-controllers/vault-secrets-operator/app.yml').read_text())
        self.assertIn('02-controllers/vault-secrets-operator/resources',
                      [s.get('path') for s in app['spec']['sources']])
        self.assertEqual(app['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '2')

    @unittest.skipUnless(VSO_CHART_SOURCE and shutil.which('helm'),
                         'Set VSO_CHART_SOURCE to the cached upstream 1.6.0 chart')
    def test_operator_chart_grants_global_transformation_lookup(self):
        app = yaml.safe_load((ROOT / '02-controllers/vault-secrets-operator/app.yml').read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'values.json'
            path.write_text(json.dumps(app['spec']['sources'][0]['helm']['valuesObject']))
            result = subprocess.run(['helm', 'template', 'vault-secrets-operator', VSO_CHART_SOURCE,
                                     '--namespace', 'vault-secrets-operator', '-f', str(path)],
                                    capture_output=True, text=True, check=True, timeout=30)
        documents = list(yaml.safe_load_all(result.stdout))
        roles = [d for d in documents if d and d.get('kind') == 'ClusterRole' and any(
            'secrettransformations' in rule.get('resources', []) and
            {'get', 'list', 'watch'} <= set(rule['verbs']) for rule in d['rules'])]
        deployment, = [d for d in documents if d and d.get('kind') == 'Deployment']
        binding, = [d for d in documents if d and d.get('kind') == 'ClusterRoleBinding'
                    and d['roleRef']['name'] in {role['metadata']['name'] for role in roles}
                    and any(s.get('name') == deployment['spec']['template']['spec']['serviceAccountName']
                            for s in d.get('subjects', []))]
        self.assertIn({'kind': 'ServiceAccount',
                       'name': deployment['spec']['template']['spec']['serviceAccountName'],
                       'namespace': 'vault-secrets-operator'}, binding['subjects'])

    @unittest.skipUnless(VSO_CHART_SOURCE, 'Set VSO_CHART_SOURCE to the cached upstream 1.6.0 chart')
    def test_helper_reference_fields_match_native_vso_schema(self):
        chart = Path(VSO_CHART_SOURCE)
        self.assertEqual(str(yaml.safe_load((chart / 'Chart.yaml').read_text())['version']), '1.6.0')
        crd = yaml.safe_load((chart / 'crds/secrets.hashicorp.com_vaultstaticsecrets.yaml').read_text())
        version, = [v for v in crd['spec']['versions'] if v['name'] == 'v1beta1']
        fields = version['schema']['openAPIV3Schema']['properties']['spec']['properties'][
            'destination']['properties']['transformation']['properties']
        self.assertEqual(fields['excludeRaw']['type'], 'boolean')
        references = fields['transformationRefs']
        self.assertEqual(references['type'], 'array')
        self.assertEqual(references['items']['properties']['name']['type'], 'string')
        self.assertEqual(references['items']['properties']['namespace']['type'], 'string')
        for secret in consumers(desired_documents()) + consumers(chart_documents()):
            transform = secret['spec']['destination']['transformation']
            self.assertIsInstance(transform['excludeRaw'], bool)
            self.assertEqual(transform['transformationRefs'], REFERENCE)

    def test_every_mapped_input_uses_shared_helper(self):
        secrets = consumers(desired_documents()) + consumers(chart_documents())
        self.assertGreater(len(secrets), 15)
        for secret in secrets:
            with self.subTest(secret=secret['metadata']['name']):
                transform = secret['spec']['destination']['transformation']
                self.assertEqual(transform['transformationRefs'], REFERENCE)
                texts = '\n'.join(t['text'] for t in transform['templates'].values())
                mapped = set(re.findall(r'get .Secrets "([^"]+)"', texts))
                mapped.update(re.findall(r'\.Secrets\.([\w-]+)', texts))
                self.assertTrue(mapped)
                self.assertEqual(required_keys(texts), mapped)
                self.assertNotIn('fail ', texts)
                self.assertNotIn('define "requiredValue"', texts)


@unittest.skipUnless(VSO_SOURCE and shutil.which('go'),
                     'Set VSO_TEMPLATE_SOURCE to cached VSO v1.6.0 sources')
class RequiredValueRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='vso-template-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        for name in ('go.mod', 'go.sum', 'template/template.go', 'template/funcs.go'):
            destination = directory / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(VSO_SOURCE) / name, destination)
        (directory / 'main.go').write_text(HARNESS)
        cls.binary = directory / 'render'
        subprocess.run(['go', 'build', '-mod=readonly', '-o', str(cls.binary), '.'],
            cwd=directory, env={**os.environ, 'GOPROXY': 'off', 'GOSUMDB': 'off',
                               'GOTOOLCHAIN': 'local'}, check=True, capture_output=True, timeout=120)

    def render(self, templates, values):
        result = subprocess.run([str(self.binary)], input=json.dumps({
            'Sources': HELPER['spec']['sourceTemplates'], 'Templates': templates, 'Values': values}),
            text=True, capture_output=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def test_all_outputs_reject_each_missing_or_sprig_empty_input(self):
        for secret in consumers(desired_documents()) + consumers(chart_documents()):
            for output, template in secret['spec']['destination']['transformation']['templates'].items():
                keys = required_keys(template['text'])
                values = {key: ' fixture+/:' + key + '\n' for key in keys}
                if 'endpoint' in keys:
                    values['endpoint'] = 'https://fixture.invalid/path'
                with self.subTest(secret=secret['metadata']['name'], output=output):
                    result = self.render({output: template}, values)
                    self.assertEqual(result['Error'], '')
                    actual = result['Data'][output]
                    if output == 'alertmanager.yaml':
                        config = yaml.safe_load(actual)
                        self.assertEqual(config['global'], {
                            'smtp_smarthost': values['smarthost'], 'smtp_from': values['from'],
                            'smtp_auth_username': values['username'],
                            'smtp_auth_password': values['password'], 'smtp_require_tls': True})
                        self.assertEqual(config['receivers'][0]['email_configs'][0]['to'], values['to'])
                    elif output == 'dsn':
                        from urllib.parse import quote_plus
                        from test_postgres_consumer import values as postgres_values
                        p = {**yaml.safe_load((ROOT / 'charts/postgres-consumer/values.yaml').read_text())['postgresConsumer'],
                             **postgres_values('radar')['postgresConsumer']}
                        self.assertEqual(actual, 'postgresql://' + quote_plus(values['username']) + ':' +
                            quote_plus(values['password']) + '@' + p['host'] + ':' + str(p['port']) +
                            '/' + p['databaseName'] + '?sslmode=' + p['sslMode'])
                    elif keys:
                        self.assertEqual(len(keys), 1)
                        key, = keys
                        expected = values[key]
                        if output == 'cookie-secret':
                            expected = expected.replace('+', '-').replace('/', '_')
                        if output == 'etcd-s3-endpoint':
                            expected = expected.removeprefix('https://')
                        self.assertEqual(actual, expected)
                    else:
                        self.assertEqual(actual, template['text'])
                for key in keys:
                    for empty in ('MISSING', '', None, False, 0, [], {}):
                        with self.subTest(secret=secret['metadata']['name'], output=output, key=key, empty=empty):
                            invalid = dict(values)
                            if empty == 'MISSING':
                                invalid.pop(key)
                            else:
                                invalid[key] = empty
                            result = self.render({output: template}, invalid)
                            self.assertIsNone(result['Data'])
                            self.assertIn('missing or empty OpenBao key: ' + key, result['Error'])

    def test_historical_postgres_outputs_keep_identical_successful_bytes(self):
        actual = {d['metadata']['name']: d for d in consumers(desired_documents()) + consumers(chart_documents())}
        values = {key: ' fixture+/:"' + key + '\n' for key in
                  ('username', 'password', 'client_id', 'client_secret', 'auth-secret', 'token')}
        for path in (ROOT / 'tests/fixtures/postgres-consumer').glob('*.yaml'):
            for old in consumers(yaml.safe_load_all(path.read_text())):
                name = old['metadata']['name']
                if name not in actual:
                    continue
                with self.subTest(secret=name):
                    before = self.render(old['spec']['destination']['transformation']['templates'], values)
                    self.assertEqual(before['Error'], '')
                    self.assertEqual(self.render(actual[name]['spec']['destination']['transformation']['templates'], values), before)

    def test_success_preserves_whitespace_and_aliases(self):
        secrets = {d['metadata']['name']: d for d in consumers(desired_documents())}
        values = {'password': ' fixture-password\n', 'access-key-id': ' fixture-access ',
                  'secret-access-key': 'fixture-secret\n', 'region': 'auto', 'endpoint': 'https://fixture.invalid'}
        for name, expected in (
            ('k8up-repository', {'password': values['password']}),
            ('postgresql-validation-r2', {**{k: values[k] for k in ('access-key-id', 'secret-access-key')},
             'region': values['region'], 'S3_ENDPOINT': values['endpoint']})):
            with self.subTest(secret=name):
                self.assertEqual(self.render(secrets[name]['spec']['destination']['transformation']['templates'], values),
                                 {'Data': expected, 'Error': ''})


if __name__ == '__main__':
    unittest.main()
