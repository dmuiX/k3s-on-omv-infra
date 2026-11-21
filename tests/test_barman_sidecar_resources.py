"""Contract for the complete raw Barman ObjectStore and its Secret reference."""

import unittest

import yaml

from workload_contracts import OWN_REPO, Repository

LIVE_REPO = "https://github.com/dmuiX/k3s-on-omv-live.git"


class BarmanSidecarResourcesTest(unittest.TestCase):
    def test_existing_postgres_application_keeps_only_its_raw_sources(self) -> None:
        app = Repository().apps["k3s-postgres"]
        self.assertEqual(app["metadata"]["namespace"], "argocd")
        self.assertEqual(
            app["metadata"]["annotations"],
            {
                "argocd.argoproj.io/sync-wave": "7",
                "argocd.argoproj.io/compare-options": "ServerSideDiff=true",
            },
        )
        spec = app["spec"]
        self.assertEqual(
            spec["destination"], {"name": "in-cluster", "namespace": "cloudnative-pg"}
        )
        self.assertEqual(
            spec["sources"],
            [
                {"repoURL": repo, "targetRevision": "main", "path": path}
                for repo, path in (
                    (OWN_REPO, "07-k3s-postgres/resources"),
                    (OWN_REPO, "adapters/credential-custody"),
                    (OWN_REPO, "adapters/postgresql-size-retention"),
                    (LIVE_REPO, "infra/k3s-postgres"),
                )
            ],
        )
        self.assertEqual(
            set(spec["syncPolicy"]["syncOptions"]),
            {
                "ServerSideApply=true",
                "ClientSideApplyMigration=false",
                "SkipDryRunOnMissingResource=true",
            },
        )

    def test_one_complete_object_store_with_required_endpoint_reference(self) -> None:
        documents = Repository().render("k3s-postgres")
        self.assertFalse(any(d["kind"] == "Secret" for d in documents))
        (store,) = [d for d in documents if d["kind"] == "ObjectStore"]
        (cluster,) = [d for d in documents if d["kind"] == "Cluster"]
        self.assertEqual(store["apiVersion"], "barmancloud.cnpg.io/v1")
        self.assertEqual(store["metadata"]["namespace"], "cloudnative-pg")
        self.assertEqual(store["metadata"]["name"], "k3s-postgres-backups")
        self.assertEqual(
            store["metadata"]["name"],
            cluster["spec"]["plugins"][0]["parameters"]["barmanObjectName"],
        )
        self.assertEqual(
            store["metadata"]["annotations"],
            {
                "argocd.argoproj.io/sync-wave": "-1",
                "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
                "k3s-on-omv.io/create-once-owner": "protected-ceremonies/platform-r2",
            },
        )
        self.assertLess(
            -1,
            int(
                cluster["metadata"]
                .get("annotations", {})
                .get("argocd.argoproj.io/sync-wave", "0")
            ),
        )
        expected = yaml.safe_load(
            """
            configuration:
              destinationPath: s3://cloudnativepg-backups/
              s3Credentials:
                accessKeyId: {name: postgresql-r2-credentials, key: access-key-id}
                secretAccessKey: {name: postgresql-r2-credentials, key: secret-access-key}
                region: {name: postgresql-r2-credentials, key: region}
              data: {compression: gzip, encryption: AES256, jobs: 2}
              wal: {compression: gzip, encryption: AES256, maxParallel: 2}
            instanceSidecarConfiguration:
              logLevel: info
              retentionPolicyIntervalSeconds: 1800
              env:
                - name: AWS_ENDPOINT_URL_S3
                  valueFrom:
                    secretKeyRef:
                      name: postgresql-r2-credentials
                      key: S3_ENDPOINT
                      optional: false
              resources:
                requests: {cpu: 50m, memory: 64Mi}
                limits: {cpu: 250m, memory: 256Mi}
        """
        )
        self.assertEqual(store["spec"], expected)
        endpoint_ref = expected["instanceSidecarConfiguration"]["env"][0]["valueFrom"][
            "secretKeyRef"
        ]
        (credential_source,) = [
            d["spec"]
            for d in documents
            if d["kind"] == "VaultStaticSecret"
            and d["metadata"]["namespace"] == store["metadata"]["namespace"]
            and d["spec"]["destination"]["name"] == endpoint_ref["name"]
        ]
        self.assertEqual(credential_source["path"], "platform/r2")
        templates = credential_source["destination"]["transformation"]["templates"]
        self.assertEqual(
            set(templates),
            {"access-key-id", "secret-access-key", "region", "S3_ENDPOINT"},
        )
        self.assertIn('get .Secrets "endpoint"', templates[endpoint_ref["key"]]["text"])

    def test_validator_config_map_is_the_selective_sync_target(self) -> None:
        repo = Repository()
        documents = repo.render("backup-validation")
        (job,) = [
            d for d in documents if d["kind"] == "CronJob"
            and d["metadata"]["name"] == "postgresql-restore-validation"
        ]
        self.assertEqual(job["metadata"]["namespace"], "backup-validation")
        self.assertIs(job["spec"]["suspend"], False, "Full sync can reopen the validator")
        pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        (volume,) = [v for v in pod["volumes"] if v["name"] == "validator"]
        name = volume["configMap"]["name"]
        self.assertEqual(name, "postgresql-restore-test-script")
        (config,) = [
            d for d in documents if d["kind"] == "ConfigMap"
            and d["metadata"]["namespace"] == "backup-validation"
            and d["metadata"]["name"] == name
        ]
        script = repo.root / "08-backup-validation/resources/postgresql_restore_test.py"
        self.assertEqual(config["data"], {script.name: script.read_text()})
        (container,) = [c for c in pod["containers"] if c["name"] == "validate"]
        (mount,) = [m for m in container["volumeMounts"] if m["name"] == volume["name"]]
        self.assertEqual(mount["mountPath"], "/validator")
        self.assertEqual(container["args"], [f"/validator/{script.name}"])


if __name__ == "__main__":
    unittest.main()
