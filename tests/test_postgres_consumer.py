"""Offline PostgreSQL consumer chart contracts (Python + PyYAML + Helm only)."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/postgres-consumer"
FIXTURES = ROOT / "tests/fixtures/postgres-consumer"
LIVE = ROOT.parent / "k3s-on-omv-live"
APPS = ROOT.parent / "k3s-on-omv-apps"


def documents(path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def baseline(consumer):
    docs = documents(FIXTURES / f"{consumer}.yaml") if consumer == "grafana" else (
        documents(FIXTURES / "radar-postgres-access.yaml")
        + documents(FIXTURES / "radar-credentials.yaml")
    )
    return [doc for doc in docs if doc["kind"] in ("Database", "SecretBackendStaticRole")
            or (doc["kind"] == "VaultDynamicSecret" and doc["metadata"]["name"] == f"{consumer}-db")]


def values(consumer):
    database, role, credentials = baseline(consumer)
    provider = role["spec"]["forProvider"]
    spec = credentials["spec"]
    result = {
        "enabled": True,
        "name": database["metadata"]["name"],
        "databaseName": database["spec"]["name"],
        "username": database["spec"]["owner"],
        "roleDisplayName": consumer.capitalize(),
        "databaseNamespace": database["metadata"]["namespace"],
        "clusterName": database["spec"]["cluster"]["name"],
        "providerConfigRef": role["spec"]["providerConfigRef"]["name"],
        "backend": provider["backend"],
        "rotationPeriod": provider["rotationPeriod"],
        "vaultAuthRef": spec["vaultAuthRef"],
        "secretName": spec["destination"]["name"],
        "deploymentName": spec["rolloutRestartTargets"][0]["name"],
        "credentialFormat": "dsn" if consumer == "radar" else "split",
    }
    if consumer == "radar":
        text = spec["destination"]["transformation"]["templates"]["dsn"]["text"]
        result["host"] = text.split("@", 1)[1].split(":", 1)[0]
    return {"postgresConsumer": result}


def render(config, namespace="radar", check=True):
    with tempfile.TemporaryDirectory(prefix="postgres-consumer-") as directory:
        path = Path(directory) / "values.yaml"
        path.write_text(yaml.safe_dump(config))
        result = subprocess.run(
            ["helm", "template", "consumer", str(CHART), "--namespace", namespace, "-f", str(path)],
            text=True, capture_output=True,
        )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


def shared_required_baseline(docs):
    """Normalize only required-value syntax; engine tests cover its semantics."""
    docs = copy.deepcopy(docs)
    for doc in docs:
        if doc['kind'] not in ('VaultStaticSecret', 'VaultDynamicSecret'):
            continue
        transform = doc['spec']['destination'].get('transformation', {})
        changed = False
        for template in transform.get('templates', {}).values():
            text = template['text']
            for key in ('username', 'password', 'client_id', 'client_secret', 'auth-secret', 'token'):
                old = ('{{- $value := get .Secrets "' + key + '" -}}\n'
                       '{{- if empty $value -}}\n'
                       '{{- fail "missing required OpenBao key: ' + key + '" -}}\n'
                       '{{- end -}}\n{{- $value -}}')
                if text == old:
                    text = ('{{ template "requiredValue" (dict "key" "' + key
                            + '" "value" (get .Secrets "' + key + '")) }}')
            guard = ('{{- if or (empty $username) (empty $password) -}}\n'
                     '{{- fail "missing required OpenBao database username or password" -}}\n'
                     '{{- end -}}\n')
            if guard in text:
                text = text.replace(guard, '')
                for key in ('username', 'password'):
                    text = text.replace('{{ $' + key + ' | urlquery }}',
                        '{{ template "requiredValue" (dict "key" "' + key
                        + '" "value" (and $' + key + ' ($' + key + ' | urlquery))) }}')
            if text != template['text']:
                changed = True
                template['text'] = text
        if changed:
            transform['transformationRefs'] = [{'name': 'required-openbao-value',
                                               'namespace': 'vault-secrets-operator'}]
    return docs


def shared_connection(docs):
    """Only connection ownership changes; preserve the historical identity baseline."""
    result = []
    for doc in copy.deepcopy(docs):
        if doc['kind'] == 'VaultConnection':
            continue
        if doc['kind'] == 'VaultAuth':
            doc['spec']['vaultConnectionRef'] = 'vault-secrets-operator/default'
        result.append(doc)
    return result


def keyed(docs):
    return {(doc["apiVersion"], doc["kind"], doc["metadata"].get("namespace", ""),
             doc["metadata"]["name"]): doc for doc in docs}


class PostgresConsumerTests(unittest.TestCase):
    def test_exact_baseline_equality(self):
        for consumer, namespace in (("radar", "radar"), ("grafana", "kube-prometheus-stack")):
            with self.subTest(consumer=consumer):
                actual = list(yaml.safe_load_all(render(values(consumer), namespace).stdout))
                self.assertEqual(keyed(actual), keyed(shared_required_baseline(baseline(consumer))))
                self.assertEqual(len(actual), 3)

    def test_disabled_default_has_no_resources(self):
        self.assertEqual(list(yaml.safe_load_all(render({}).stdout)), [])

    def test_invalid_values_fail_closed(self):
        for field, value in (("username", "radar'; DROP ROLE x;--"),
                             ("roleDisplayName", "Radar'"), ("secretName", ""),
                             ("vaultAuthRef", ""), ("credentialFormat", "raw"),
                             ("host", ""), ("host", "host/path"), ("port", 0),
                             ("rotationPeriod", 0), ("connectionLimit", -1),
                             ("unexpectedRawResources", [])):
            with self.subTest(field=field, value=value):
                config = values("radar")
                config["postgresConsumer"][field] = value
                self.assertNotEqual(render(config, check=False).returncode, 0)

    def test_templates_remain_vso_templates(self):
        for consumer in ("radar", "grafana"):
            rendered = render(values(consumer)).stdout
            docs = list(yaml.safe_load_all(rendered))
            statement = docs[1]["spec"]["forProvider"]["rotationStatements"][0]
            self.assertIn("PASSWORD '{{password}}'", statement)
            # OpenBao expects a JSON array encoded in a string, not raw SQL.
            self.assertEqual(len(json.loads(statement)), 1)
            templates = docs[2]["spec"]["destination"]["transformation"]["templates"]
            self.assertEqual(set(templates), {"dsn"} if consumer == "radar" else {"username", "password"})
            for template in templates.values():
                self.assertIn("get .Secrets", template["text"])
                self.assertIn('template "requiredValue"', template['text'])
            self.assertEqual(docs[2]['spec']['destination']['transformation']['transformationRefs'],
                             [{'name': 'required-openbao-value',
                               'namespace': 'vault-secrets-operator'}])
            self.assertNotIn("kind: Secret\n", rendered)

    @unittest.skipUnless(LIVE.is_dir(), "Live checkout not available")
    def test_live_values_render_exact_baseline(self):
        radar = yaml.safe_load((LIVE / "apps/radar/values.yml").read_text())
        grafana = yaml.safe_load((LIVE / "infra/kube-prometheus-stack/postgres-values.yml").read_text())
        for consumer, config, namespace in (("radar", radar, "radar"),
                ("grafana", grafana, "kube-prometheus-stack")):
            with self.subTest(consumer=consumer):
                actual = list(yaml.safe_load_all(render(config, namespace).stdout))
                self.assertEqual(keyed(actual), keyed(shared_required_baseline(baseline(consumer))))

    def test_grafana_source_and_remaining_auth(self):
        app = yaml.safe_load((ROOT / "09-kube-prometheus-stack/app.yml").read_text())
        source, = [s for s in app["spec"]["sources"] if s.get("path") == "charts/postgres-consumer"]
        self.assertEqual(source["path"], "charts/postgres-consumer")
        self.assertEqual(source["ref"], "postgresConsumer")
        self.assertEqual(source["helm"]["valueFiles"],
                         ["$live/infra/kube-prometheus-stack/postgres-values.yml"])
        remaining = documents(ROOT / "09-kube-prometheus-stack/resources/grafana.yaml")
        original = shared_connection(documents(FIXTURES / "grafana.yaml"))
        self.assertEqual(keyed(remaining), keyed([d for d in original
                         if d not in baseline("grafana") and d["kind"] != "Namespace"]))

    @unittest.skipUnless(APPS.is_dir(), "Apps checkout not available")
    def test_radar_source_and_no_duplicate_database_resources(self):
        app = yaml.safe_load((APPS / "radar/app.yml").read_text())
        sources = [s for s in app["spec"]["sources"] if s.get("path") == "charts/postgres-consumer"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["helm"]["valueFiles"], ["$live/apps/radar/values.yml"])
        resources = APPS / "radar/resources"
        original = documents(FIXTURES / "radar-credentials.yaml")
        self.assertEqual(keyed(documents(resources / "credentials.yaml")), keyed([
            d for d in original if not (d["kind"] == "VaultDynamicSecret"
                                       and d["metadata"]["name"] == "radar-db")]))
        self.assertFalse((resources / "postgres-access.yaml").exists())
        for path in resources.glob("*.yaml"):
            for doc in documents(path):
                self.assertNotIn(doc.get("kind"), ("Database", "SecretBackendStaticRole"))
                self.assertFalse(doc.get("kind") == "VaultDynamicSecret" and doc["metadata"]["name"] == "radar-db")


if __name__ == "__main__":
    unittest.main()
