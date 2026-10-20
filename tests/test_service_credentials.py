"""Exercise the deployed Job script, including real OpenBao ACL/CAS and OIDC."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
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
MANIFEST = INFRA / '06-platform/openbao-config/resources/service-credentials.yaml'
DOCUMENTS = list(yaml.safe_load_all(MANIFEST.read_text()))
SCRIPT = next(d['data']['provision.py'] for d in DOCUMENTS if d['kind'] == 'ConfigMap')
BINARY = os.environ.get('OPENBAO_TEST_BINARY')


def module():
    result = types.ModuleType('provision')
    exec(compile(SCRIPT, str(MANIFEST), 'exec'), result.__dict__)
    return result


class ServiceCredentialContracts(unittest.TestCase):
    def test_job_is_before_secret_consumers_and_bounded(self):
        job = next(d for d in DOCUMENTS if d['kind'] == 'Job')
        self.assertEqual(job['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '1')
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 600)
        pod = job['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['restartPolicy'], 'Never')
        for container in pod['containers'] + pod['initContainers']:
            self.assertIn('@sha256:', container['image'])
            self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        policy = next(d for d in DOCUMENTS if d['kind'] == 'Policy')['spec']['forProvider']['policy']
        self.assertNotIn('"update"', policy)
        self.assertNotIn('path "*"', policy)
        self.assertNotIn('root', SCRIPT)

    def test_sql_blocks_are_json_wrapped_for_the_native_plugin_parser(self):
        for path in [APPS / 'radar/resources/postgres-access.yaml',
                     INFRA / '08-observability/kube-prometheus-stack/resources/grafana.yaml']:
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
        m = module()
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
        policy = next(d for d in DOCUMENTS if d['kind'] == 'Policy')['spec']['forProvider']['policy']
        cls.api('sys/policies/acl/service-credentials', {'policy': policy})
        cls.job_token = cls.api('auth/token/create', {
            'policies': ['service-credentials'], 'no_default_policy': True})['auth']['client_token']

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
            m.kv_read(self.job_token, 'radar/auth')
        self.api('kv/data/radar/argocd', {'data': {'managed-by': m.OWNER, 'token': 'fixture'}})
        req = urllib.request.Request(self.address + 'kv/data/radar/argocd', method='DELETE',
                                     headers={'X-Vault-Token': self.root_token})
        urllib.request.urlopen(req, timeout=5).close()
        with self.assertRaisesRegex(m.Failure, 'history'):
            m.kv_read(self.job_token, 'radar/argocd')
        with self.assertRaises(m.APIError) as denied:
            self.api('kv/data/platform/r2', token=self.job_token)
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
        files = [INFRA / '06-platform/openbao-config/resources/radar-identity.yaml',
                 LIVE / 'infra/openbao-config/longhorn-oidc.yaml']
        for path in files:
            for resource in yaml.safe_load_all(path.read_text()):
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


if __name__ == '__main__':
    unittest.main()
