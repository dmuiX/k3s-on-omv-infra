import copy
import io
import json
import subprocess
import sys
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError
from urllib.request import Request
from uuid import UUID

import yaml

PATH = Path(__file__).parents[1] / "adapters/postgresql-size-retention/retention.py"
VALIDATOR = PATH.parents[2] / "08-backup-validation/resources/postgresql_restore_test.py"
LEASE_PATH = "/apis/coordination.k8s.io/v1/namespaces/backup-validation/leases"
LEASE_NAME = "postgresql-restore-status"
SPEC = spec_from_file_location("retention", PATH)
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RetentionManifestTest(unittest.TestCase):
    def test_postgresql_connection_is_declared_once(self):
        resources = PATH.parents[2] / "07-k3s-postgres/resources"
        identities = {}
        for path in resources.glob("*.yaml"):
            for doc in yaml.safe_load_all(path.read_text()):
                if not doc:
                    continue
                metadata = doc["metadata"]
                identity = (doc["apiVersion"], doc["kind"],
                            metadata.get("namespace", ""), metadata["name"])
                self.assertNotIn(identity, identities,
                                 f"Duplicate resource in {path} and {identities.get(identity)}")
                identities[identity] = path.name
        self.assertEqual(identities[("database.vault.upbound.io/v1alpha1",
                                     "SecretBackendConnection", "", "postgresql")],
                         "openbao-database-connection.yaml")

    def test_restore_uses_disposable_storage_and_production_retains_data(self):
        root = PATH.parents[2]
        script = root / "08-backup-validation/resources/postgresql_restore_test.py"
        spec = spec_from_file_location("postgresql_restore_test", script)
        adapter = module_from_spec(spec)
        spec.loader.exec_module(adapter)
        target = "2026-11-20T03:06:00Z"
        restore = adapter.validation_cluster(target)
        self.assertEqual(restore["metadata"], {"namespace": "backup-validation", "name": "postgresql-restore-test"})
        self.assertEqual(restore["spec"]["storage"], {"size": "5Gi", "storageClass": "longhorn-single-node-delete"})
        self.assertEqual(restore["spec"]["inheritedMetadata"]["annotations"]["k8up.io/backup"], "false")
        self.assertEqual(restore["spec"]["bootstrap"]["recovery"]["recoveryTarget"], {"targetTime": target})
        resources = root / "02-controllers/longhorn/resources"
        classes = {c["metadata"]["name"]: c
                   for path in resources.glob("storageclasses*.yaml")
                   for c in yaml.safe_load_all(path.read_text())}
        production, = (doc for doc in yaml.safe_load_all(
            (root / "07-k3s-postgres/resources/cluster.yaml").read_text())
            if doc["kind"] == "Cluster" and doc["metadata"]["name"] == "k3s-postgres")
        self.assertEqual(classes[production["spec"]["storage"]["storageClass"]]["reclaimPolicy"], "Retain")
        self.assertEqual(classes[restore["spec"]["storage"]["storageClass"]]["reclaimPolicy"], "Delete")

    def test_restore_test_and_retention_agree_on_evidence(self):
        spec = spec_from_file_location("postgresql_restore_test", VALIDATOR)
        adapter = module_from_spec(spec)
        spec.loader.exec_module(adapter)
        legacy = {
            "metadata": {
                "name": LEASE_NAME, "namespace": "backup-validation", "resourceVersion": "1234",
                "annotations": {
                    "k3s-on-omv.io/result": "success", "k3s-on-omv.io/pitr-verified": "true",
                    "k3s-on-omv.io/omv-mirror-verified": "true",
                },
            },
            "spec": {"holderIdentity": "postgresql-pitr-validation", "renewTime": "2000-01-01T00:00:00Z"},
        }
        for current, result, flags, expected in (
            (None, "failed", {}, "false"),
            (legacy, "success", {"restore_target_verified": True, "mirror_verified": True}, "true"),
        ):
            with self.subTest(result=result), patch.object(adapter, "api", side_effect=[current, {}]) as api:
                started = datetime.now(timezone.utc)
                adapter.put_lease(result, **flags)
                body = api.call_args.args[2]
                named_path = f"{LEASE_PATH}/{LEASE_NAME}"
                self.assertEqual(api.call_args_list, [
                    call(named_path, allowed=(404,)),
                    (call(named_path, "PUT", body) if current else call(LEASE_PATH, "POST", body)),
                ])
                self.assertEqual(body["metadata"]["name"], LEASE_NAME)
                self.assertEqual(body["metadata"]["namespace"], "backup-validation")
                self.assertEqual(body["metadata"].get("resourceVersion"), "1234" if current else None)
                self.assertEqual(body["metadata"]["annotations"], {
                    "k3s-on-omv.io/result": result,
                    "k3s-on-omv.io/restore-target-verified": expected,
                    "k3s-on-omv.io/omv-mirror-verified": expected,
                })
                self.assertEqual(body["spec"]["holderIdentity"], "postgresql-restore-test")
                renewed = MODULE._parse_time(body["spec"]["renewTime"])
                self.assertGreaterEqual(renewed, started)
                with (patch.dict(MODULE.os.environ, RESTORE_LEASE_NAMESPACE="backup-validation",
                                 RESTORE_LEASE_NAME=LEASE_NAME,
                                 BASE_BACKUP_BUDGET_BYTES=str(MODULE.BUDGET)),
                      patch.object(MODULE.Path, "read_text", return_value="mock-token"),
                      patch.object(MODULE.ssl, "create_default_context"),
                      patch.object(MODULE.urllib.request, "urlopen") as read_lease):
                    if result == "success":
                        read_lease.return_value = io.StringIO(json.dumps(body))
                        self.assertEqual(MODULE._lease(), (result, renewed))
                        self.assertEqual(read_lease.call_args.args[0].selector, named_path)
                        missing_mirror = copy.deepcopy(body)
                        del missing_mirror["metadata"]["annotations"]["k3s-on-omv.io/omv-mirror-verified"]
                        for invalid in (legacy, missing_mirror):
                            read_lease.return_value = io.StringIO(json.dumps(invalid))
                            with patch.object(MODULE, "_catalog") as catalog:
                                with self.assertRaisesRegex(RuntimeError, "does not verify the recovery target time"):
                                    MODULE.run(preview=True)
                                catalog.assert_not_called()
                    else:
                        read_lease.return_value = io.StringIO(json.dumps(body))
                        with self.assertRaisesRegex(RuntimeError, "does not verify the recovery target time"):
                            MODULE._lease()

    def test_restore_validator_pod_uses_runtime_default_seccomp(self):
        path = PATH.parents[2] / "08-backup-validation/resources/validation.yaml"
        job, = (doc for doc in yaml.safe_load_all(path.read_text())
                if doc["kind"] == "CronJob" and doc["metadata"]["name"] == "postgresql-restore-validation")
        pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod.get("securityContext", {}).get("seccompProfile"), {"type": "RuntimeDefault"})

    def test_restore_validator_role_limits_existing_resources_to_its_own_name(self) -> None:
        documents = yaml.safe_load_all((VALIDATOR.parent / "validation.yaml").read_text())
        role, = (doc for doc in documents if doc["kind"] == "Role"
                 and doc["metadata"]["namespace"] == "backup-validation"
                 and doc["metadata"]["name"] == "restore-validation")
        for resource, api_group, named_verbs in (
            ("clusters", "postgresql.cnpg.io", {"get", "delete"}),
            ("objectstores", "barmancloud.cnpg.io", {"delete"}),
        ):
            with self.subTest(resource=resource):
                rules = [rule for rule in role["rules"] if resource in rule["resources"] or "*" in rule["resources"]]
                self.assertCountEqual([{**rule, "verbs": set(rule["verbs"])} for rule in rules], [
                    {"apiGroups": [api_group], "resources": [resource], "verbs": {"create"}},
                    {"apiGroups": [api_group], "resources": [resource], "verbs": named_verbs,
                     "resourceNames": ["postgresql-restore-test"]},
                ])

    def test_validation_uses_explicit_r2_secret_references(self):
        root = PATH.parents[2] / "08-backup-validation/resources"
        documents = list(yaml.safe_load_all((root / "credentials.yaml").read_text()))
        secret = next(doc for doc in documents
                      if doc["metadata"]["name"] == "postgresql-validation-r2")
        keys = set(secret["spec"]["destination"]["transformation"]["templates"])
        self.assertEqual(keys, {"access-key-id", "secret-access-key", "region", "S3_ENDPOINT"})
        documents = list(yaml.safe_load_all((root / "validation.yaml").read_text()))
        job = next(doc for doc in documents
                   if doc["metadata"]["name"] == "postgresql-restore-validation")
        container = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        self.assertNotIn("envFrom", container)
        refs = {env["name"]: env["valueFrom"]["secretKeyRef"]
                for env in container["env"] if "valueFrom" in env}
        self.assertEqual(refs, {
            name: {"name": "postgresql-validation-r2", "key": key}
            for name, key in {
                "AWS_ACCESS_KEY_ID": "access-key-id",
                "AWS_SECRET_ACCESS_KEY": "secret-access-key",
                "AWS_REGION": "region", "S3_ENDPOINT": "S3_ENDPOINT",
            }.items()
        })

    def test_validation_store_keeps_local_r2_credentials_during_and_after_bridge(self):
        root = PATH.parents[2]
        store = yaml.safe_load((root / "07-k3s-postgres/resources/objectstore.yaml").read_text())
        validator = root / "08-backup-validation/resources/postgresql_restore_test.py"
        spec = spec_from_file_location("postgresql_restore_test", validator)
        adapter = module_from_spec(spec)
        self.enterContext(patch.object(Path, "read_text", side_effect=AssertionError("read a runtime file")))
        self.enterContext(patch("ssl.create_default_context", side_effect=AssertionError("initialized TLS")))
        self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("called the API")))
        self.enterContext(patch.dict(sys.modules, {"psycopg2": None}))
        spec.loader.exec_module(adapter)
        local_url = "https://validation.example.invalid"
        extra_env = {"name": "FIXTURE", "value": "unchanged"}
        for phase in ("legacy", "bridge", "env-only"):
            with self.subTest(phase=phase):
                source = copy.deepcopy(store)
                sidecar = source["spec"]["instanceSidecarConfiguration"]
                sidecar["env"].append(extra_env)
                if phase != "env-only":
                    source["spec"]["configuration"]["endpointURL"] = "https://source.example.invalid"
                if phase == "legacy":
                    sidecar["env"].pop(0)
                baseline = copy.deepcopy(source)
                clone = adapter.validation_store(source, local_url)
                self.assertEqual(source, baseline)
                self.assertEqual(clone["metadata"], {
                    "name": "postgresql-restore-test", "namespace": "backup-validation",
                })
                self.assertEqual(clone["spec"]["instanceSidecarConfiguration"], {
                    **sidecar, "env": [extra_env],
                })
                self.assertEqual(clone["spec"]["configuration"], {
                    **source["spec"]["configuration"], "endpointURL": local_url,
                    "s3Credentials": {
                        "accessKeyId": {"name": "postgresql-validation-r2", "key": "access-key-id"},
                        "secretAccessKey": {"name": "postgresql-validation-r2", "key": "secret-access-key"},
                        "region": {"name": "postgresql-validation-r2", "key": "region"},
                    },
                })

    def test_r2_credentials_are_mapped_without_duplicate_secret_keys(self):
        root = PATH.parents[2]
        credentials = list(yaml.safe_load_all(
            (root / "07-k3s-postgres/resources/credentials.yaml").read_text()))
        secret = next(doc for doc in credentials
                      if doc["metadata"]["name"] == "postgresql-r2-credentials")
        templates = secret["spec"]["destination"]["transformation"]["templates"]
        self.assertEqual(set(templates),
                         {"access-key-id", "secret-access-key", "region", "S3_ENDPOINT"})
        documents = list(yaml.safe_load_all(
            (root / "07-k3s-postgres/resources/retention.yaml").read_text()))
        job = next(doc for doc in documents if doc["kind"] == "CronJob")
        pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        container = pod["containers"][0]
        self.assertEqual(container["args"], ["/adapter/retention.py", "run", "--dry-run"])
        self.assertFalse(any("secretRef" in source for source in container["envFrom"]))
        refs = {env["name"]: env["valueFrom"]["secretKeyRef"]
                for env in container["env"] if "valueFrom" in env}
        self.assertEqual(refs, {
            name: {"name": "postgresql-r2-credentials", "key": key}
            for name, key in {
                "AWS_ACCESS_KEY_ID": "access-key-id",
                "AWS_SECRET_ACCESS_KEY": "secret-access-key",
                "AWS_REGION": "region",
                "S3_ENDPOINT": "S3_ENDPOINT",
            }.items()
        })


class RestoreValidatorTest(unittest.TestCase):
    store_collection = "/apis/barmancloud.cnpg.io/v1/namespaces/backup-validation/objectstores"
    cluster_collection = "/apis/postgresql.cnpg.io/v1/namespaces/backup-validation/clusters"
    store_path = f"{store_collection}/postgresql-restore-test"
    cluster_path = f"{cluster_collection}/postgresql-restore-test"

    def setUp(self) -> None:
        spec = spec_from_file_location("postgresql_restore_test", VALIDATOR)
        self.adapter = module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.enterContext(patch.dict(self.adapter.os.environ, S3_ENDPOINT="https://validation.example.invalid"))
        run = UUID(int=1)
        self.enterContext(patch.object(self.adapter.uuid, "uuid4", return_value=run))
        self.enterContext(patch.object(self.adapter, "secret", return_value={}))
        self.enterContext(patch.object(self.adapter, "api_settings", return_value=({}, None)))
        self.enterContext(patch.object(self.adapter.time, "sleep"))
        connect = self.enterContext(patch.object(self.adapter, "connect"))
        cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (datetime(2026, 11, 20, tzinfo=timezone.utc),)
        cursor.fetchall.return_value = [(f"{run.hex}-before", "before")]
        self.lease = self.enterContext(patch.object(self.adapter, "put_lease"))
        self.http = self.enterContext(patch.object(self.adapter, "urlopen"))
        self.source = yaml.safe_load((PATH.parents[2] / "07-k3s-postgres/resources/objectstore.yaml").read_text())

    @staticmethod
    def response(payload: dict, *, status: int = 200) -> MagicMock:
        reply = MagicMock(status=status)
        reply.__enter__.return_value = reply
        reply.read.return_value = json.dumps(payload).encode()
        return reply

    def created_response(self, uid: str) -> MagicMock:
        return self.response(self.created(uid), status=201)

    def created(self, uid: str) -> dict:
        return {"metadata": {"name": "postgresql-restore-test", "namespace": "backup-validation", "uid": uid}}

    def error(self, path: str, status: int) -> HTTPError:
        error = HTTPError(f"https://kubernetes.default.svc{path}", status, "mock failure", {}, io.BytesIO())
        self.addCleanup(error.close)
        return error

    def assert_deletes(self, *expected: tuple[str, str]) -> None:
        requests = [args[0] for args, _ in self.http.call_args_list if args[0].get_method() == "DELETE"]
        self.assertEqual([(req.selector, json.loads(req.data)) for req in requests], [
            (path, {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}})
            for path, uid in expected
        ])

    def test_missing_source_credentials_replace_previous_evidence_without_marker_cleanup(self) -> None:
        def missing_credentials(namespace: str, name: str) -> dict:
            self.lease.assert_called_once_with("running")
            raise RuntimeError("source credentials unavailable")

        self.adapter.secret.side_effect = missing_credentials
        with self.assertRaisesRegex(RuntimeError, "source credentials unavailable"):
            self.adapter.main()
        self.assertEqual(self.lease.call_args_list, [call("running"), call("failed")])
        self.adapter.connect.assert_not_called()
        self.http.assert_not_called()

    def test_object_store_collision_deletes_neither_resource(self) -> None:
        self.http.side_effect = [self.response(self.source), self.error(self.store_collection, 409)]
        with self.assertRaisesRegex(RuntimeError, "failed with HTTP 409"):
            self.adapter.main()
        self.assert_deletes()
        self.assertEqual(self.http.call_count, 2)  # No Cluster create or ownership lookup.
        self.assertEqual(self.lease.call_args_list, [call("running"), call("failed")])

    def test_cluster_collision_deletes_only_the_confirmed_store_uid(self) -> None:
        self.http.side_effect = [
            self.response(self.source), self.created_response("store-uid"),
            self.error(self.cluster_collection, 409), self.response({}),
        ]
        with self.assertRaisesRegex(RuntimeError, "failed with HTTP 409"):
            self.adapter.main()
        self.assert_deletes((self.store_path, "store-uid"))
        self.assertEqual(self.lease.call_args_list, [call("running"), call("failed")])

    def test_ambiguous_create_never_looks_up_or_deletes_the_name(self) -> None:
        for response, error, message in (
            (TimeoutError("create response lost"), TimeoutError, "response lost"),
            (self.response(self.created("existing-uid")), RuntimeError, "did not return HTTP 201"),
            (self.created_response(""), RuntimeError, "did not confirm"),
        ):
            with self.subTest(message=message):
                self.http.reset_mock()
                self.http.side_effect = [self.response(self.source), response]
                with self.assertRaisesRegex(error, message):
                    self.adapter.main()
                self.assert_deletes()
                self.assertEqual(self.http.call_count, 2)

    def test_uid_conflict_never_retries_delete_against_replacement(self) -> None:
        ready = {**self.created("cluster-uid"), "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        replies = iter([
            self.response(self.source), self.created_response("store-uid"),
            self.created_response("cluster-uid"), self.response(ready),
        ])
        live_cluster = self.created("replacement-uid")

        def request(req: Request, **kwargs: object) -> MagicMock:
            if req.get_method() == "DELETE" and req.selector == self.cluster_path:
                # The Cluster was replaced after readiness, before the server handled DELETE.
                uid = json.loads(req.data or b"{}").get("preconditions", {}).get("uid")
                if uid is not None and uid != live_cluster["metadata"]["uid"]:
                    raise self.error(req.selector, 409)
                return self.response({})
            if req.get_method() == "DELETE" and req.selector == self.store_path:
                return self.response({})
            return next(replies)

        self.http.side_effect = request
        with (patch.object(self.adapter, "mirror_matches_catalog", return_value=True),
              self.assertRaisesRegex(RuntimeError, "validation cleanup failed") as failure):
            self.adapter.main()
        self.assertIn("HTTP 409", str(failure.exception.__cause__))
        self.assert_deletes((self.cluster_path, "cluster-uid"), (self.store_path, "store-uid"))
        self.assertEqual(self.lease.call_args_list, [call("running"), call("failed-cleanup")])

    def test_success_is_published_only_after_uid_guarded_cleanup(self) -> None:
        ready = {**self.created("cluster-uid"), "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        self.http.side_effect = [
            self.response(self.source), self.created_response("store-uid"),
            self.created_response("cluster-uid"), self.response(ready),
            self.response({}), self.error(self.store_path, 404),
        ]

        def record(result: str, **flags: bool) -> None:
            if result == "success":
                self.assert_deletes((self.cluster_path, "cluster-uid"), (self.store_path, "store-uid"))

        self.lease.side_effect = record
        with patch.object(self.adapter, "mirror_matches_catalog", return_value=True):
            self.adapter.main()
        self.assertEqual(self.lease.call_args_list, [
            call("running"), call("success", restore_target_verified=True, mirror_verified=True),
        ])

    def test_catalog_errors_do_not_log_private_command_or_output(self) -> None:
        endpoint, bucket, server = "https://private-catalog.example.invalid", "fixture-bucket", "fixture-server"
        stdout, stderr = "fixture-catalog-stdout", "fixture-catalog-stderr"
        args = ["barman-cloud-backup-list", "--format", "json", "--endpoint-url", endpoint, f"s3://{bucket}", server]
        ready = {**self.created("cluster-uid"), "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        for error in (subprocess.CalledProcessError(1, args, output=stdout, stderr=stderr),
                      subprocess.TimeoutExpired(args, 300, output=stdout, stderr=stderr)):
            with (self.subTest(error=type(error).__name__),
                  patch.dict(self.adapter.os.environ, S3_ENDPOINT=endpoint, S3_BUCKET=bucket, BARMAN_SERVER_NAME=server),
                  patch.object(subprocess, "run", side_effect=error) as barman):
                self.http.reset_mock()
                self.lease.reset_mock()
                self.http.side_effect = [
                    self.response(self.source), self.created_response("store-uid"),
                    self.created_response("cluster-uid"), self.response(ready),
                    self.response({}), self.response({}),
                ]
                try:
                    self.adapter.main()
                except Exception:
                    log = traceback.format_exc()
                else:
                    self.fail("a catalog error did not fail the restore test")
                for private in (endpoint, bucket, server, stdout, stderr):
                    self.assertNotIn(private, log)
                self.assertIn("RuntimeError: barman-cloud-backup-list failed or timed out", log)
                self.assertEqual(barman.call_args.args[0], args)
                self.assertEqual(self.lease.call_args_list, [call("running"), call("failed")])
                self.assert_deletes((self.cluster_path, "cluster-uid"), (self.store_path, "store-uid"))

    def test_replacement_is_not_accepted_as_ready(self) -> None:
        replacement = {**self.created("replacement-uid"), "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        self.http.side_effect = [self.response(replacement)]
        with self.assertRaisesRegex(RuntimeError, "UID changed"):
            self.adapter.wait_ready("cluster-uid")


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        self.backups = [MODULE.Backup(str(i), self.now - timedelta(days=i), 3 * 1024**3) for i in range(3)]

    def plan(self, backups=None, **kwargs):
        return MODULE.plan_dry_run(
            backups or self.backups,
            "success",
            self.now,
            now=self.now,
            acceptance=True,
            catalog_matches_storage=True,
            **kwargs,
        )

    def test_dry_run_then_unchanged_catalog_authorizes_exact_candidate(self):
        status, dry_run = self.plan()
        self.assertIn("dry-run", status)
        self.assertEqual(dry_run, ["barman-cloud-backup-delete", "--dry-run", "--backup-id", "2", "--minimum-redundancy", "2"])
        execute = MODULE.authorize_after_dry_run(self.backups, list(self.backups), dry_run, dry_run_succeeded=True)
        self.assertEqual(execute, ["barman-cloud-backup-delete", "--backup-id", "2", "--minimum-redundancy", "2"])

    def test_catalog_reads_native_iso_time_and_explicit_keep_status(self):
        record = {"backup_id": "20261014T010000", "status": "DONE", "size": 1234,
                  "end_time": "Wed Oct 14 00:00:00 2026", "end_time_iso": self.now.isoformat()}
        catalog = json.dumps({"backups_list": [record]})
        for keep in ("full", "standalone", "nokeep", "unknown"):
            with self.subTest(keep=keep), patch.object(MODULE, "_barman_base_args", return_value=["target", "db"]), \
                    patch.object(MODULE, "_run", side_effect=[catalog, f"Keep: {keep}\n"]) as cli:
                if keep == "unknown":
                    with self.assertRaisesRegex(RuntimeError, "unknown Barman keep status"):
                        MODULE._catalog()
                else:
                    backup, = MODULE._catalog()
                    self.assertEqual(backup.completed_at, self.now)
                    self.assertEqual(backup.archival_protected, keep != "nokeep")
                self.assertEqual(cli.call_args.args[0], ["barman-cloud-backup-keep", "--status", "target", "db", record["backup_id"]])

    def test_missing_restore_evidence_blocks_even_catalog_access(self):
        with (patch.dict(MODULE.os.environ, BASE_BACKUP_BUDGET_BYTES=str(MODULE.BUDGET)),
              patch.object(MODULE, "_lease", side_effect=RuntimeError("missing restore evidence")),
              patch.object(MODULE, "_run") as cli):
            with self.assertRaisesRegex(RuntimeError, "missing restore evidence"):
                MODULE.run(preview=True)
            cli.assert_not_called()

    def test_preview_never_invokes_real_delete(self):
        with (patch.dict(MODULE.os.environ, BASE_BACKUP_BUDGET_BYTES=str(MODULE.BUDGET)),
              patch.object(MODULE, "_lease", return_value=("success", datetime.now(timezone.utc))),
              patch.object(MODULE, "_catalog", return_value=self.backups),
              patch.object(MODULE, "_storage_state", return_value=(True, False)),
              patch.object(MODULE, "_barman_base_args", return_value=["target", "db"]),
              patch.object(MODULE, "_run") as cli):
            MODULE.run(preview=True)
            cli.assert_called_once_with(["barman-cloud-backup-delete", "--dry-run", "--backup-id", "2",
                                         "--minimum-redundancy", "2", "target", "db"])

    def test_changed_catalog_or_failed_dry_run_never_authorizes_delete(self):
        _, dry_run = self.plan()
        self.assertEqual(MODULE.authorize_after_dry_run(self.backups, self.backups[:-1], dry_run, dry_run_succeeded=True), [])
        self.assertEqual(MODULE.authorize_after_dry_run(self.backups, self.backups, dry_run, dry_run_succeeded=False), [])

    def test_stale_lease_running_backup_and_minimum_are_closed(self):
        self.assertEqual(MODULE.plan_dry_run(self.backups, "success", self.now - timedelta(days=9), now=self.now, acceptance=True, catalog_matches_storage=True)[1], [])
        self.assertEqual(self.plan(backup_running=True)[1], [])
        self.assertIn("minimum redundancy", self.plan(self.backups[:2])[0])

    def test_catalog_mismatch_duplicate_and_archival_protection_are_closed(self):
        self.assertEqual(MODULE.plan_dry_run(self.backups, "success", self.now, now=self.now, acceptance=True, catalog_matches_storage=False)[1], [])
        duplicate = [self.backups[0], self.backups[0], self.backups[1]]
        self.assertEqual(self.plan(duplicate)[1], [])
        protected = [MODULE.Backup(b.backup_id, b.completed_at, b.base_bytes, archival_protected=True) for b in self.backups]
        self.assertEqual(self.plan(protected)[1], [])

    def test_post_delete_requires_only_candidate_removed_and_two_remaining(self):
        self.assertTrue(MODULE.verify_post_delete(self.backups, self.backups[:2], "2"))
        self.assertFalse(MODULE.verify_post_delete(self.backups, self.backups[:1], "2"))
        self.assertFalse(MODULE.verify_post_delete(self.backups, self.backups[1:], "2"))


if __name__ == "__main__":
    unittest.main()
