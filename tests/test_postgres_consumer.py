"""Offline PostgreSQL consumer chart contracts (Python + PyYAML + Helm only)."""
import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
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


def render(config, namespace="radar", check=True, *, env=None):
    with tempfile.TemporaryDirectory(prefix="postgres-consumer-") as directory:
        path = Path(directory) / "values.yaml"
        path.write_text(yaml.safe_dump(config))
        result = subprocess.run(
            ["helm", "template", "consumer", str(CHART), "--namespace", namespace, "-f", str(path)],
            text=True, capture_output=True, env=env, timeout=30,
        )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


def shared_required_baseline(docs):
    """Normalize only the intentional template changes from the historical baseline."""
    docs = copy.deepcopy(docs)
    for doc in docs:
        if doc['kind'] == 'SecretBackendStaticRole':
            provider = doc['spec']['forProvider']
            login = provider['username']
            for i, statement in enumerate(provider['rotationStatements']):
                for command in ('ALTER ROLE', 'CREATE ROLE'):
                    statement = statement.replace(f'{command} {login} ', f'{command} \\"{login}\\" ')
                provider['rotationStatements'][i] = statement.replace(
                    '(rolsuper OR rolreplication OR rolbypassrls)',
                    '(NOT rolcanlogin OR rolsuper OR rolreplication OR rolbypassrls OR rolcreatedb OR rolcreaterole)',
                ).replace('WITH LOGIN NOCREATEDB NOCREATEROLE INHERIT CONNECTION LIMIT',
                          'WITH CONNECTION LIMIT')
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


def rotation_sql(*renders, env=None, hook=()):
    with tempfile.TemporaryDirectory(prefix="rotation-grants-") as directory:
        paths = []
        for i, render_text in enumerate(renders):
            path = Path(directory) / f"consumer-{i}.yaml"
            path.write_text(render_text)
            paths.append(path)
        return subprocess.run(
            [sys.executable, str(ROOT / "07-k3s-postgres/tools/render_rotation_grants.py"),
             "--namespace", "cloudnative-pg", "--cluster", "k3s-postgres", *hook, *paths],
            text=True, capture_output=True, env=env, timeout=30,
        )


HOOK_PRIMARY = "k3s-postgres-2"
HOOK_CLUSTER_UID = "fce60004-619e-42ac-9888-117201afcbcb"
HOOK_POD_UID = "85d30f18-8bb9-4e7f-8629-575a8b75760b"
HOOK_SYSTEM_ID = "1234567890"
HOOK = ("--argo-hook", HOOK_PRIMARY, HOOK_CLUSTER_UID, HOOK_POD_UID, HOOK_SYSTEM_ID)


class RotationGrantTests(unittest.TestCase):
    def test_argo_hook_is_scoped_and_ordered_before_rotation(self):
        manifests = render(values("radar")).stdout
        result = rotation_sql(manifests, hook=HOOK)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout)}
        self.assertEqual(set(rendered), {"ConfigMap", "ServiceAccount", "Role", "RoleBinding", "Job"})
        config = rendered["ConfigMap"]
        self.assertTrue(config["immutable"])
        self.assertIn(f"IS DISTINCT FROM '{HOOK_SYSTEM_ID}'", config["data"]["grants.sql"])
        self.assertIn("SET LOCAL lock_timeout = '5s'", config["data"]["grants.sql"])
        rules = rendered["Role"]["rules"]
        by_resource = {rule["resources"][0]: rule for rule in rules}
        self.assertEqual(set(by_resource), {"clusters", "databaseroles", "pods", "pods/exec"})
        self.assertEqual(by_resource["databaseroles"]["verbs"], ["list"])
        self.assertEqual(by_resource["clusters"]["resourceNames"], ["k3s-postgres"])
        self.assertEqual(by_resource["pods"]["resourceNames"], [HOOK_PRIMARY])
        self.assertEqual(by_resource["pods/exec"]["resourceNames"], [HOOK_PRIMARY])
        self.assertEqual(set(by_resource["pods/exec"]["verbs"]), {"create", "get"})
        wave = "argocd.argoproj.io/sync-wave"
        job = rendered["Job"]
        self.assertEqual(job["metadata"]["annotations"]["argocd.argoproj.io/hook"], "Sync")
        job_wave = int(job["metadata"]["annotations"][wave])
        for kind in set(rendered) - {"Job"}:
            self.assertLess(int(rendered[kind]["metadata"]["annotations"][wave]), job_wave)
        static_role = next(d for d in yaml.safe_load_all(manifests) if d["kind"] == "SecretBackendStaticRole")
        self.assertLess(job_wave, int(static_role["metadata"]["annotations"][wave]))
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], rendered["ServiceAccount"]["metadata"]["name"])
        self.assertEqual(pod["volumes"][0]["configMap"]["name"], config["metadata"]["name"])

    def test_argo_hook_refuses_changed_primary_or_cnpg_ownership(self):
        rendered = rotation_sql(render(values("radar")).stdout, hook=HOOK)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        manifests = {doc["kind"]: doc for doc in yaml.safe_load_all(rendered.stdout)}
        job = manifests["Job"]
        expected_sql = manifests["ConfigMap"]["data"]["grants.sql"]
        container = job["spec"]["template"]["spec"]["containers"][0]
        env = {entry["name"]: entry["value"] for entry in container["env"]}
        with tempfile.TemporaryDirectory(prefix="rotation-hook-") as directory:
            kubectl = Path(directory) / "kubectl"
            kubectl.write_text('''#!/bin/sh
case "$*" in
  *status.currentPrimary*) printf '%s' "$MOCK_CLUSTER" ;;
  *databaseroles*) [ "$MOCK_OWNERS" != fail ] || exit 42; printf '%s' "$MOCK_OWNERS" ;;
  *ownerReferences*) printf '%s' "$MOCK_POD" ;;
  *' exec '*) cat; exit "$MOCK_PSQL_EXIT" ;;
  *) exit 42 ;;
esac
''')
            kubectl.chmod(0o700)
            sql = Path(directory) / "grants.sql"
            script = container["command"][-1].replace("/bin/kubectl", shlex.quote(str(kubectl))).replace(
                "MIGRATION_PATH=/migration/grants.sql", f"MIGRATION_PATH={shlex.quote(str(sql))}")
            observations = f"{HOOK_CLUSTER_UID}/{HOOK_PRIMARY}/{HOOK_PRIMARY}/{HOOK_SYSTEM_ID}/"
            defaults = {"PATH": "/usr/bin:/bin:/sbin", "MOCK_CLUSTER": observations, "MOCK_OWNERS": "other/",
                        "MOCK_POD": f"{HOOK_POD_UID}/{HOOK_CLUSTER_UID}", "MOCK_PSQL_EXIT": "0"}
            for observed, contents, code, diagnostic, calls_psql in (
                ({}, expected_sql, 0, "", True),
                ({"MOCK_CLUSTER": f"{HOOK_CLUSTER_UID}/{HOOK_PRIMARY}/new-primary/{HOOK_SYSTEM_ID}/"},
                 expected_sql, 1, "identity or primary changed", False),
                ({"MOCK_CLUSTER": observations + "openbao_manager"}, expected_sql, 1, "still owns", False),
                ({"MOCK_OWNERS": "other/k3s-postgres/"}, expected_sql, 1, "DatabaseRole owns", False),
                ({"MOCK_OWNERS": "fail"}, expected_sql, 42, "", False),
                ({"MOCK_POD": f"other-pod/{HOOK_CLUSTER_UID}"}, expected_sql, 1, "identity or ownership", False),
                ({}, "wrong migration\n", 1, "does not match", False),
                ({"MOCK_PSQL_EXIT": "3"}, expected_sql, 3, "", True),
            ):
                with self.subTest(observed=observed, diagnostic=diagnostic):
                    sql.write_text(contents)
                    result = subprocess.run(["/bin/sh", "-ec", script], text=True, capture_output=True,
                                            env=env | defaults | observed, timeout=10)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertEqual(result.stdout, expected_sql if calls_psql else "")

    def test_argo_hook_requires_valid_identity_and_opted_in_login(self):
        config = values("radar")
        config["postgresConsumer"]["roleSyncWave"] = "-11"
        for text, target, diagnostic in (
            ("", HOOK, "no opted-in logins"),
            ("", (*HOOK[:3], "not-a-uid", HOOK_SYSTEM_ID), "Kubernetes UUID"),
            ("", (*HOOK[:-1], "1234;"), "unsigned 64-bit decimal"),
            (render(config).stdout, HOOK, "sync wave -10 or later"),
        ):
            with self.subTest(diagnostic=diagnostic):
                result = rotation_sql(text, hook=target)
                self.assertEqual(result.returncode, 1)
                self.assertIn(diagnostic, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_rendered_consumers_opt_in_without_a_second_list(self):
        ordinary = copy.deepcopy(baseline("radar")[0])
        ordinary["metadata"]["name"] = "ordinary"
        ordinary["spec"].update(name="ordinary", owner="ordinary")
        result = rotation_sql(render(values("radar")).stdout,
                              render(values("grafana"), "kube-prometheus-stack").stdout,
                              yaml.safe_dump(ordinary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("('grafana', 'grafana')", result.stdout)
        self.assertIn("('radar', 'radar')", result.stdout)
        self.assertNotIn("ordinary", result.stdout)
        self.assertIn("WITH ADMIN TRUE, INHERIT FALSE, SET FALSE", result.stdout)

    def test_renamed_app_and_login_are_taken_from_the_same_values(self):
        config = values("radar")
        config["postgresConsumer"].update(name="new-app", username="new_login", databaseName="new_db")
        result = rotation_sql(render(config).stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("('new_login', 'new_db')", result.stdout)
        self.assertNotIn("('radar', 'radar')", result.stdout)

    def test_invalid_or_conflicting_request_emits_no_migration(self):
        docs = list(yaml.safe_load_all(render(values("radar")).stdout))
        for username, diagnostic in (
            ("postgres", "refusing reserved login"),
            ("pg_read_all_data", "refusing reserved login"),
            ("different_login", "must match its own Database and owner"),
            ("bad'login", "must be simple PostgreSQL identifiers"),
        ):
            with self.subTest(username=username):
                candidate = copy.deepcopy(docs)
                role, = (d for d in candidate if d["kind"] == "SecretBackendStaticRole")
                database, = (d for d in candidate if d["kind"] == "Database")
                role["spec"]["forProvider"]["username"] = username
                if username != "different_login":
                    database["spec"]["owner"] = username
                result = rotation_sql(yaml.safe_dump_all(candidate))
                self.assertEqual(result.returncode, 1)
                self.assertIn(diagnostic, result.stderr)
                self.assertEqual(result.stdout, "")
        result = rotation_sql(yaml.safe_dump_all(docs), yaml.safe_dump_all(docs))
        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate Database declaration", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_duplicate_identity_rejected_before_scope_filtering(self):
        manifests = render(values("radar")).stdout
        for kind, field, diagnostic in (
            ("Database", "clusterName", "duplicate Database declaration"),
            ("SecretBackendStaticRole", "clusterName", "duplicate static role declaration"),
            ("SecretBackendStaticRole", "providerConfigRef", "duplicate static role declaration"),
        ):
            with self.subTest(kind=kind, field=field):
                config = values("radar")
                config["postgresConsumer"][field] = "other"
                duplicate, = (d for d in yaml.safe_load_all(render(config).stdout) if d["kind"] == kind)
                for sources in ((manifests, yaml.safe_dump(duplicate)), (yaml.safe_dump(duplicate), manifests)):
                    result = rotation_sql(*sources)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_wrong_cluster_or_openbao_provider_does_not_get_grants(self):
        for field in ("clusterName", "providerConfigRef"):
            with self.subTest(field=field):
                config = values("radar")
                config["postgresConsumer"][field] = "unrelated"
                result = rotation_sql(render(config).stdout)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "-- No opted-in PostgreSQL logins.\n")

    def test_nonrotated_database_cannot_share_an_opted_in_login(self):
        docs = list(yaml.safe_load_all(render(values("radar")).stdout))
        ordinary = copy.deepcopy(docs[0])
        ordinary["metadata"]["name"] = "ordinary"
        ordinary["spec"]["name"] = "ordinary"
        result = rotation_sql(yaml.safe_dump_all(docs), yaml.safe_dump(ordinary))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("more than one declaration", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_malformed_resource_emits_no_migration(self):
        manifests = render(values("radar")).stdout
        for broken, diagnostic in (
            (manifests.replace("providerConfigRef:", "providerConfigRef: null\n  ignored:"),
             "expected Kubernetes manifest mappings"),
            (manifests.replace('    dbName: "k3s-postgres"\n', ''), "spec.forProvider.dbName"),
            (manifests.replace('    name: "k3s-postgres"', '    name: 42', 1), "spec.cluster.name"),
            (manifests.replace('    username: "radar"', '    username: "radar"\n    username: "other"'),
             "duplicate YAML key"),
            (manifests + "\n---\ninvalid: [", "Cannot render rotation grants"),
            (manifests + "\n---\nkind: Database\n", "at apiVersion"),
            (manifests + "\n---\napiVersion: postgresql.cnpg.io/v1\nkind: 7\n", "at kind"),
        ):
            with self.subTest(diagnostic=diagnostic):
                self.assertNotEqual(broken, manifests)
                result = rotation_sql(broken)
                self.assertEqual(result.returncode, 1)
                self.assertIn(diagnostic, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_foreign_namespace_static_role_cannot_pair_with_ordinary_database(self):
        config = values("radar")
        config["postgresConsumer"]["databaseNamespace"] = "foreign"
        ordinary = baseline("radar")[0]
        result = rotation_sql(render(config).stdout, yaml.safe_dump(ordinary))
        self.assertEqual(result.returncode, 1)
        self.assertIn("in the same render", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_list_wrapper_cannot_hide_an_overlapping_database(self):
        manifests = render(values("radar")).stdout
        ordinary = copy.deepcopy(baseline("radar")[0])
        ordinary["metadata"]["name"] = "ordinary"
        ordinary["spec"]["name"] = "ordinary"
        for kind in ("List", "ResourceCollection"):
            with self.subTest(kind=kind):
                wrapped = yaml.safe_dump({"apiVersion": "v1", "kind": kind, "items": [ordinary]})
                for sources in ((manifests, wrapped), (wrapped, manifests)):
                    result = rotation_sql(*sources)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("Kubernetes List input is not supported", result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_keyword_login_is_quoted_in_rotation_and_migration(self):
        config = values("radar")
        config["postgresConsumer"]["username"] = "select"
        manifests = render(config).stdout
        docs = list(yaml.safe_load_all(manifests))
        role, = (d for d in docs if d["kind"] == "SecretBackendStaticRole")
        sql, = json.loads(role["spec"]["forProvider"]["rotationStatements"][0])
        self.assertIn('ALTER ROLE "select" WITH CONNECTION LIMIT', sql)
        self.assertIn('CREATE ROLE "select" WITH LOGIN', sql)
        result = rotation_sql(manifests)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("('select', 'radar')", result.stdout)


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
            sql, = json.loads(statement)
            alter = sql.split("ALTER ROLE", 1)[1].split(";", 1)[0]
            self.assertNotIn("CREATEDB", alter)
            self.assertNotIn("CREATEROLE", alter)
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
        # Custody and reader ACL details are covered by test_grafana_admin;
        # retain exact inventory and the existing database auth contract here.
        custody = {("VaultStaticSecret", "grafana-admin"),
                   ("Policy", "grafana-vso-read"), ("AuthBackendRole", "grafana-vso")}
        self.assertEqual({(d["kind"], d["metadata"]["name"]) for d in remaining
                          if d["kind"] in {kind for kind, _ in custody}}, custody)
        self.assertFalse(any(d["kind"] == "ServiceAccount" for d in remaining))
        actual = [d for d in remaining if (d["kind"], d["metadata"]["name"]) not in custody]
        original = documents(FIXTURES / "grafana.yaml")
        expected = [d for d in original if d not in baseline("grafana")
                    and d["kind"] not in ("Namespace", "ServiceAccount")]
        expected = shared_connection(expected)
        for doc in expected:
            if doc["kind"] == "VaultAuth":
                doc["spec"]["kubernetes"]["serviceAccount"] = "kube-prometheus-stack-grafana"
        self.assertEqual(keyed(actual), keyed(expected))

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
