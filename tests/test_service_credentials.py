"""Exercise the deployed Job script, including real OpenBao ACL/CAS and OIDC."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

import yaml

INFRA = Path(__file__).resolve().parents[1]
LIVE = INFRA.parent / 'k3s-on-omv-live'
APPS = INFRA.parent / 'k3s-on-omv-apps'
MANIFESTS = {
    'longhorn': INFRA / '06-platform/longhorn-config/resources/provision-credentials.yaml',
    'radar': APPS / 'radar/resources/provision-credentials.yaml',
    'k3s-postgres': INFRA / '07-k3s-postgres/resources/provision-credentials.yaml',
}
DOCUMENTS = {name: list(yaml.safe_load_all(path.read_text())) for name, path in MANIFESTS.items()}
MANIFEST = INFRA / 'adapters/credential-custody/resources.yaml'
SCRIPT = next(d['data']['credential_helpers.py'] for d in yaml.safe_load_all(MANIFEST.read_text())
              if d['kind'] == 'ConfigMap')
BINARY = os.environ.get('OPENBAO_TEST_BINARY')


def module():
    result = types.ModuleType('provision')
    exec(compile(SCRIPT, str(MANIFEST), 'exec'), result.__dict__)
    return result


class ServiceCredentialContracts(unittest.TestCase):
    def test_audiobookshelf_custody_can_reach_openbao_api_and_ambient(self):
        documents = yaml.safe_load_all((INFRA / '04-openbao/resources/security.yaml').read_text())
        policy = next(d for d in documents if d and d['kind'] == 'NetworkPolicy'
                      and d['metadata']['name'] == 'allow-openbao-raft-and-clients')
        for port in (8200, 15008):
            rules = [r for r in policy['spec']['ingress']
                     if any(p['port'] == port for p in r.get('ports', []))]
            self.assertTrue(any('audiobookshelf' in match.get('values', [])
                for rule in rules for peer in rule.get('from', [])
                for match in peer.get('namespaceSelector', {}).get('matchExpressions', [])
                if match.get('key') == 'kubernetes.io/metadata.name'
                and match.get('operator') == 'In'), port)

    def test_secret_engines_and_auth_objects_survive_native_owner_migration_without_delete_permission(self):
        checked = 0
        for directory in ('06-platform', '07-k3s-postgres', '08-backup-validation', '09-kube-prometheus-stack'):
            for path in (INFRA / directory).rglob('*.yaml'):
                for resource in yaml.safe_load_all(path.read_text()):
                    if not isinstance(resource, dict) or resource.get('kind') not in ('Policy', 'AuthBackendRole', 'Mount', 'Backend'):
                        continue
                    self.assertEqual(resource['spec'].get('deletionPolicy'), 'Orphan',
                        f"{path}: {resource['metadata']['name']} must preserve its external secret/auth object")
                    if resource['kind'] in ('Mount', 'Backend'):
                        self.assertEqual(resource['metadata']['annotations'].get('crossplane.io/external-name'),
                                         resource['spec']['forProvider']['path'],
                                         'Adopt the existing engine instead of trying to create it again')
                    checked += 1
        self.assertGreater(checked, 20, 'The lifecycle contract must cover the actual consumers')

    def test_jobs_are_consumer_owned_before_secret_consumers_and_bounded(self):
        for name, documents in DOCUMENTS.items():
            with self.subTest(consumer=name):
                script = next(d['data']['provision.py'] for d in documents if d['kind'] == 'ConfigMap')
                compile(script, '<' + name + '-provision>', 'exec')
                job = next(d for d in documents if d['kind'] == 'Job')
                wave = '-1' if name in ('k3s-postgres', 'radar') else '1'
                self.assertEqual(job['metadata']['annotations']['argocd.argoproj.io/sync-wave'], wave)
                self.assertEqual(job['spec']['activeDeadlineSeconds'], 600)
                pod = job['spec']['template']['spec']
                self.assertFalse(pod['automountServiceAccountToken'])
                self.assertEqual(pod['restartPolicy'], 'Never')
                self.assertEqual(job['metadata']['namespace'], 'cloudnative-pg' if name == 'k3s-postgres' else name)
                self.assertEqual(job['spec']['template']['metadata']['labels']['istio.io/dataplane-mode'], 'ambient')
                for container in pod['containers'] + pod.get('initContainers', []):
                    self.assertIn('@sha256:', container['image'])
                    self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
                policy = next(d for d in documents if d['kind'] == 'Policy')['spec']['forProvider']['policy']
                self.assertNotIn('"update"', policy)
                self.assertNotIn('path "*"', policy)
                if name != 'radar':
                    self.assertFalse(any(d['kind'] in ('Role', 'RoleBinding') for d in documents))
                    self.assertNotIn('initContainers', pod)
                else:
                    binding = next(d for d in documents if d['kind'] == 'RoleBinding')
                    self.assertEqual(binding['metadata']['namespace'], 'argocd')
                    self.assertEqual(binding['subjects'][0]['namespace'], 'radar')
        for consumer in ('longhorn/', 'radar/', 'postgresql/'):
            self.assertNotIn(consumer, SCRIPT, 'Shared custody helper must not own service decisions')
        self.assertNotIn('argocd', SCRIPT)
        self.assertFalse((INFRA / '06-platform/openbao-config/resources/service-credentials.yaml').exists())

    def test_radar_native_core_discovery_and_portforward_permissions_stay_namespaced(self):
        role = next(d for d in DOCUMENTS['radar'] if d['kind'] == 'Role')
        self.assertEqual(role['metadata']['namespace'], 'argocd')
        rules = role['rules']
        for resource, verbs in [('services', {'get', 'list'}), ('pods', {'get', 'list'}),
                                ('pods/portforward', {'get', 'create'})]:
            self.assertTrue(any(resource in rule['resources'] and verbs.issubset(rule['verbs'])
                                and rule['apiGroups'] == [''] for rule in rules), resource)
        self.assertFalse(any(d['kind'] in ('ClusterRole', 'ClusterRoleBinding') for d in DOCUMENTS['radar']))
        self.assertFalse(any('create' in r['verbs'] and 'pods' in r['resources'] for r in rules),
                         'Native port forwarding does not require permission to create arbitrary Pods')

    def test_radar_binary_copy_does_not_charge_tmpfs_to_the_128mi_init_container(self):
        job = next(d for d in DOCUMENTS['radar'] if d['kind'] == 'Job')
        volumes = {v['name']: v for v in job['spec']['template']['spec']['volumes']}
        self.assertNotEqual(volumes['tools']['emptyDir'].get('medium'), 'Memory',
                            'The pinned Argo CLI is 250176085 bytes; copying it to tmpfs OOMs the 128Mi init container')
        self.assertEqual(volumes['work']['emptyDir']['medium'], 'Memory',
                         'Temporary credentials must remain in memory')

    @unittest.skipUnless(shutil.which('helm'), 'Helm Go/Sprig renderer is required')
    def test_native_cookie_template_preserves_key_bytes_in_oauth2_proxy_url_encoding(self):
        resources = yaml.safe_load_all((INFRA / '06-platform/longhorn-config/resources/oidc-proxy.yaml').read_text())
        secret = next(r for r in resources if r['kind'] == 'VaultStaticSecret')
        template = secret['spec']['destination']['transformation']['templates']['cookie-secret']['text']
        key = bytes([251, 255]) * 16
        original = base64.b64encode(key).decode()
        self.assertRegex(original, r'[+/]', 'Fixture must exercise the actual alphabet incompatibility')
        with tempfile.TemporaryDirectory(prefix='cookie-template-') as directory:
            chart = Path(directory); (chart / 'templates').mkdir()
            (chart / 'Chart.yaml').write_text('apiVersion: v2\nname: fixture\nversion: 0.1.0\n')
            (chart / 'values.yaml').write_text(yaml.safe_dump({'fixture': {'cookie-secret': original}}))
            (chart / 'templates/cookie.yaml').write_text(
                'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: fixture\ndata:\n  cookie: '
                '{{ include "cookie" (dict "Secrets" .Values.fixture) | quote }}\n'
                '{{ define "cookie" }}' + template + '{{ end }}\n')
            rendered = subprocess.run(['helm', 'template', 'fixture', str(chart)],
                text=True, capture_output=True, timeout=20)
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            encoded = yaml.safe_load(rendered.stdout)['data']['cookie']
            self.assertRegex(encoded, r'^[A-Za-z0-9_-]+={0,2}$',
                             'oauth2-proxy v7.15.5 uses RawURLEncoding, not standard Base64')
            self.assertEqual(base64.urlsafe_b64decode(encoded), key,
                             'Encoding correction must not rotate or change the persisted cookie key')

    def test_sql_blocks_are_json_wrapped_for_the_native_plugin_parser(self):
        for path in [APPS / 'radar/resources/postgres-access.yaml',
                     INFRA / '09-kube-prometheus-stack/resources/grafana.yaml']:
            role = next(d for d in yaml.safe_load_all(path.read_text())
                        if d and d['kind'] == 'SecretBackendStaticRole')
            statement, = role['spec']['forProvider']['rotationStatements']
            sql, = json.loads(statement)
            self.assertTrue(sql.startswith('DO $role$ BEGIN'))
            self.assertIn('Refusing privileged', sql)
            alter = sql.split('ALTER ROLE', 1)[1].split(';', 1)[0]
            self.assertNotIn('NOSUPERUSER', alter)
            self.assertNotIn('NOREPLICATION', alter)

    def test_argo_errors_cannot_echo_a_token(self):
        helper = module()
        m = types.ModuleType('radar_provision')
        script = next(d['data']['provision.py'] for d in DOCUMENTS['radar'] if d['kind'] == 'ConfigMap')
        with patch.dict(sys.modules, {'credential_helpers': helper}):
            exec(compile(script, '<radar-provision>', 'exec'), m.__dict__)
        sensitive = secrets.token_urlsafe(32)
        with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 1, stdout=sensitive, stderr=sensitive)):
            with self.assertRaises(m.Failure) as error:
                m.cli('account', 'get')
        self.assertNotIn(sensitive, str(error.exception))


@unittest.skipUnless(BINARY, 'Set OPENBAO_TEST_BINARY for real OpenBao integration tests')
class ServiceCredentialOpenBao(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='gitops-credentials-test-')
        cls.root = Path(cls.temp.name)
        cls.root.chmod(0o700)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        cls.address = f'http://127.0.0.1:{port}/v1/'
        cls.root_token = secrets.token_urlsafe(32)
        cls.log = (cls.root / 'server.log').open('w')
        cls.server = subprocess.Popen([BINARY, 'server', '-dev', '-dev-no-store-token',
            '-dev-listen-address=127.0.0.1:' + str(port), '-dev-root-token-id=' + cls.root_token],
            stdout=cls.log, stderr=subprocess.STDOUT, env={**os.environ, 'HOME': str(cls.root)})
        cls.addClassCleanup(cls.stop_server)
        cls.m = module()
        cls.m.BAO = cls.address
        for _ in range(100):
            try:
                cls.api('sys/health')
                break
            except (cls.m.Failure, urllib.error.URLError):
                time.sleep(.1)
        else:
            raise AssertionError('Disposable OpenBao did not start; private logs withheld')
        cls.api('sys/mounts/kv', {'type': 'kv', 'options': {'version': '2'}})
        cls.job_tokens = {}
        for name, documents in DOCUMENTS.items():
            policy = next(d for d in documents if d['kind'] == 'Policy')['spec']['forProvider']['policy']
            cls.api('sys/policies/acl/' + name, {'policy': policy})
            cls.job_tokens[name] = cls.api('auth/token/create', {
                'policies': [name], 'no_default_policy': True})['auth']['client_token']
        cls.job_token = cls.job_tokens['k3s-postgres']

    @classmethod
    def stop_server(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.log.close()
        cls.temp.cleanup()

    @classmethod
    def api(cls, path, data=None, token=None):
        return cls.m.request(cls.address, path, token or cls.root_token, data)

    def test_create_once_resume_foreign_history_and_acl(self):
        m = self.m
        data = {'username': 'openbao_manager', 'password': secrets.token_urlsafe(32)}
        validate = lambda value: m.fields(value, 'username', 'password')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            first = m.ensure(self.job_token, 'postgresql/openbao-manager', lambda: data, validate)
            second = m.ensure(self.job_token, 'postgresql/openbao-manager',
                              lambda: self.fail('Resume must not generate another password'), validate)
        self.assertEqual(first, second)
        self.assertNotIn(data['password'], output.getvalue())
        self.assertEqual(self.api('kv/metadata/postgresql/openbao-manager')['data']['current_version'], 1)
        with self.assertRaises(m.APIError) as denied:
            self.api('kv/data/postgresql/openbao-manager', {'data': data}, self.job_token)
        self.assertEqual(denied.exception.status, 403, 'Job ACL must forbid changing existing values')
        self.api('kv/data/radar/auth', {'data': {'auth-secret': 'foreign-test-fixture'}})
        with self.assertRaisesRegex(m.Failure, 'foreign'):
            m.kv_read(self.job_tokens['radar'], 'radar/auth')
        self.api('kv/data/radar/argocd', {'data': {'managed-by': m.OWNER, 'token': 'fixture'}})
        req = urllib.request.Request(self.address + 'kv/data/radar/argocd', method='DELETE',
                                     headers={'X-Vault-Token': self.root_token})
        urllib.request.urlopen(req, timeout=5).close()
        with self.assertRaisesRegex(m.Failure, 'history'):
            m.kv_read(self.job_tokens['radar'], 'radar/argocd')
        with self.assertRaises(m.APIError) as denied:
            self.api('kv/data/platform/r2', token=self.job_token)
        self.assertEqual(denied.exception.status, 403)

    def test_consumer_credentials_cannot_cross_service_boundaries(self):
        paths = {'longhorn': 'longhorn/oidc-proxy', 'radar': 'radar/auth',
                 'k3s-postgres': 'postgresql/openbao-manager'}
        for name, token in self.job_tokens.items():
            for owner, path in paths.items():
                if name == owner:
                    continue
                with self.subTest(issuer=name, forbidden_owner=owner):
                    with self.assertRaises(self.m.APIError) as denied:
                        self.api('kv/data/' + path, token=token)
                    self.assertEqual(denied.exception.status, 403)
                    with self.assertRaises(self.m.APIError) as denied:
                        self.api('kv/data/' + path, {'options': {'cas': 0}, 'data': {'fixture': 'test'}}, token)
                    self.assertEqual(denied.exception.status, 403)

    def test_real_oidc_code_exchange_custom_claims_pkce_and_group_assignment(self):
        self.api('sys/auth/userpass', {'type': 'userpass'})
        self.api('auth/userpass/users/operator', {'password': secrets.token_urlsafe(24)})
        entity = self.api('identity/entity', {'name': 'operator'})['data']['id']
        accessor = self.api('sys/auth')['data']['userpass/']['accessor']
        self.api('identity/entity-alias', {'name': 'operator', 'canonical_id': entity,
                                         'mount_accessor': accessor})
        group = self.api('identity/group', {'name': 'radar:admin', 'type': 'internal',
                                           'member_entity_ids': [entity]})['data']['id']
        # An entity-backed test token exercises authorization, not a root-token shortcut.
        self.api('auth/userpass/users/operator', {'password': 'disposable-oidc-test-only'})
        human = self.api('auth/userpass/login/operator', {'password': 'disposable-oidc-test-only'})['auth']['client_token']
        radar_render = subprocess.run([
            'helm', 'template', 'radar-oidc', str(APPS / 'radar/oidc'),
            '--values', str(LIVE / 'apps/radar/values.yml')],
            check=True, capture_output=True, text=True, timeout=30).stdout
        sources = [radar_render, (LIVE / 'infra/longhorn/oidc.yaml').read_text()]
        for source in sources:
            for resource in yaml.safe_load_all(source):
                if resource['kind'] not in ('OidcKey', 'OidcScope'):
                    continue
                spec = resource['spec']['forProvider']
                api_kind = 'key' if resource['kind'] == 'OidcKey' else 'scope'
                payload = {'allowed_client_ids': spec['allowedClientIds']} if api_kind == 'key' else {'template': spec['template']}
                self.api('identity/oidc/' + api_kind + '/' + spec['name'], payload)
        self.api('identity/oidc/assignment/longhorn-admin', {'group_ids': [group]})
        for name in ('radar', 'longhorn'):
            redirect = 'https://example.invalid/' + name
            self.api('identity/oidc/client/' + name, {'key': name, 'client_type': 'confidential',
                     'assignments': ['longhorn-admin'] if name == 'longhorn' else ['allow_all'],
                     'redirect_uris': [redirect]})
            client = self.api('identity/oidc/client/' + name)['data']
            if name == 'radar':
                policy = next(d['spec']['forProvider']['policy'] for d in yaml.safe_load_all(
                    (APPS / 'radar/resources/openbao-access.yaml').read_text()) if d['kind'] == 'Policy')
                self.api('sys/policies/acl/radar-vso-read', {'policy': policy})
                reader = self.api('auth/token/create', {
                    'policies': ['radar-vso-read'], 'no_default_policy': True})['auth']['client_token']
                observed = self.api('identity/oidc/client/radar', token=reader)['data']
                self.assertTrue(observed['client_id'] == client['client_id'])
                self.assertTrue(observed['client_secret'] == client['client_secret'])
                with self.assertRaises(self.m.APIError) as denied:
                    self.api('identity/oidc/client/longhorn', token=reader)
                self.assertEqual(denied.exception.status, 403)
                with self.assertRaises(self.m.APIError) as denied:
                    self.api('identity/oidc/client/radar', {'redirect_uris': [redirect]}, reader)
                self.assertEqual(denied.exception.status, 403)
            self.api('identity/oidc/provider/' + name, {'allowed_client_ids': [client['client_id']],
                     'scopes_supported': [name]})
            verifier = secrets.token_urlsafe(48)
            query = {'client_id': client['client_id'], 'redirect_uri': redirect,
                     'response_type': 'code', 'scope': 'openid ' + name, 'state': secrets.token_urlsafe(24),
                     'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode(),
                     'code_challenge_method': 'S256'}
            auth = self.api('identity/oidc/provider/' + name + '/authorize?' + urllib.parse.urlencode(query), token=human)
            payload = urllib.parse.urlencode({'grant_type': 'authorization_code', 'code': auth['code'],
                'redirect_uri': redirect, 'code_verifier': verifier}).encode()
            basic = base64.b64encode((client['client_id'] + ':' + client['client_secret']).encode()).decode()
            request = urllib.request.Request(self.address + 'identity/oidc/provider/' + name + '/token', data=payload,
                headers={'Authorization': 'Basic ' + basic, 'Content-Type': 'application/x-www-form-urlencoded'})
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
            claims = result['id_token'].split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(claims + '=' * (-len(claims) % 4)))
            self.assertEqual(claims['username'], 'operator')
            self.assertIn('radar:admin', claims['groups'])
            self.assertEqual(claims['aud'], client['client_id'])
            # A provider never accepts a different client merely because the signing key allows '*'.
            self.api('identity/oidc/provider/' + name, {'allowed_client_ids': ['foreign-client']})
            with self.assertRaises(self.m.APIError) as rejected:
                self.api('identity/oidc/provider/' + name + '/authorize?' + urllib.parse.urlencode(query), token=human)
            self.assertEqual(rejected.exception.status, 400)
        self.api('identity/group/id/' + group, {'member_entity_ids': []})
        self.api('identity/oidc/provider/longhorn', {'allowed_client_ids': [client['client_id']]})
        with self.assertRaises(self.m.APIError) as rejected:
            self.api('identity/oidc/provider/longhorn/authorize?' + urllib.parse.urlencode(query), token=human)
        self.assertEqual(rejected.exception.status, 400)

    def test_service_entrypoints_reuse_real_native_credentials(self):
        paths = {'longhorn': 'longhorn/oidc-proxy', 'k3s-postgres': 'postgresql/openbao-manager'}
        for name, path in paths.items():
            script = next(d['data']['provision.py'] for d in DOCUMENTS[name] if d['kind'] == 'ConfigMap')
            entrypoint = types.ModuleType(name + '_provision')
            with patch.dict(sys.modules, {'credential_helpers': self.m}):
                exec(compile(script, '<' + name + '-provision>', 'exec'), entrypoint.__dict__)
            with patch.object(entrypoint, 'login', return_value=self.job_tokens[name]):
                with contextlib.redirect_stdout(io.StringIO()):
                    entrypoint.main()
                    first = self.m.kv_read(self.job_tokens[name], path)
                    entrypoint.main()
                    second = self.m.kv_read(self.job_tokens[name], path)
            self.assertEqual(first, second)
            self.assertEqual(self.api('kv/metadata/' + path)['data']['current_version'], 1)


if __name__ == '__main__':
    unittest.main()
