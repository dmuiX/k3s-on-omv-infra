import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

import yaml

PATH = Path(__file__).parents[1] / "adapters/postgresql-size-retention/retention.py"
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
        script = root / "08-backup-validation/resources/postgresql_pitr.py"
        spec = spec_from_file_location("postgresql_pitr", script)
        adapter = module_from_spec(spec)
        spec.loader.exec_module(adapter)
        target = "2026-11-20T03:06:00Z"
        restore = adapter.validation_cluster(target)
        self.assertEqual(restore["metadata"], {"namespace": "backup-validation", "name": "postgresql-pitr-validation"})
        self.assertEqual(restore["spec"]["storage"], {"size": "5Gi", "storageClass": "longhorn-pitr-scratch"})
        self.assertEqual(restore["spec"]["inheritedMetadata"]["annotations"]["k8up.io/backup"], "false")
        self.assertEqual(restore["spec"]["bootstrap"]["recovery"]["recoveryTarget"], {"targetTime": target})
        classes = {c["metadata"]["name"]: c for c in yaml.safe_load_all(
            (root / "02-controllers/longhorn/resources/storageclasses.yaml").read_text())}
        production, = (doc for doc in yaml.safe_load_all(
            (root / "07-k3s-postgres/resources/cluster.yaml").read_text())
            if doc["kind"] == "Cluster" and doc["metadata"]["name"] == "k3s-postgres")
        self.assertEqual(classes[production["spec"]["storage"]["storageClass"]]["reclaimPolicy"], "Retain")
        self.assertEqual(classes[restore["spec"]["storage"]["storageClass"]]["reclaimPolicy"], "Delete")

    def test_restore_validator_pod_uses_runtime_default_seccomp(self):
        path = PATH.parents[2] / "08-backup-validation/resources/validation.yaml"
        job, = (doc for doc in yaml.safe_load_all(path.read_text())
                if doc["kind"] == "CronJob" and doc["metadata"]["name"] == "postgresql-restore-validation")
        pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(pod.get("securityContext", {}).get("seccompProfile"), {"type": "RuntimeDefault"})

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
        validator = root / "08-backup-validation/resources/postgresql_pitr.py"
        spec = spec_from_file_location("postgresql_pitr", validator)
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
                    "name": "postgresql-pitr-validation", "namespace": "backup-validation",
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
