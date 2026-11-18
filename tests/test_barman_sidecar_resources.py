"""Adopt the existing Barman ObjectStore without spec or sizing drift."""

from pathlib import Path
import subprocess
from typing import Any
import unittest

import yaml

from workload_contracts import OWN_REPO, Repository, parse_documents

ROOT = Path(__file__).resolve().parents[1]
LIVE_REPO = "https://github.com/dmuiX/k3s-on-omv-live.git"
FIXTURE = "infra/r2/values.yml"


class BarmanSidecarResourcesTest(unittest.TestCase):
    def test_existing_postgres_application_uses_live_values_and_raw_resources(
        self,
    ) -> None:
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
            [s for s in spec["sources"] if s["repoURL"] == LIVE_REPO or "ref" in s],
            [
                {
                    "repoURL": LIVE_REPO,
                    "targetRevision": "main",
                    "path": "infra/k3s-postgres",
                    "ref": "live",
                }
            ],
        )
        self.assertEqual(
            [s for s in spec["sources"] if "helm" in s],
            [
                {
                    "repoURL": OWN_REPO,
                    "targetRevision": "main",
                    "path": "charts/barman-objectstore",
                    "helm": {"valueFiles": [f"$live/{FIXTURE}"]},
                }
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

    def test_single_complete_object_matches_cluster_and_live_sidecar_sizing(
        self,
    ) -> None:
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
        annotations = store["metadata"]["annotations"]
        self.assertEqual(
            annotations["k3s-on-omv.io/create-once-owner"],
            "protected-ceremonies/platform-r2",
        )
        self.assertEqual(
            set(annotations["argocd.argoproj.io/sync-options"].split(",")),
            {"Prune=false", "Delete=false"},
        )
        self.assertLess(
            int(annotations["argocd.argoproj.io/sync-wave"]),
            int(
                cluster["metadata"]
                .get("annotations", {})
                .get("argocd.argoproj.io/sync-wave", "0")
            ),
        )
        fixture = yaml.safe_load(
            (ROOT / "tests/fixtures/public-values" / FIXTURE).read_text()
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
              resources:
                requests: {cpu: 50m, memory: 64Mi}
                limits: {cpu: 250m, memory: 256Mi}
        """
        )
        expected["configuration"]["endpointURL"] = fixture["r2"]["endpoint"]
        self.assertEqual(store["spec"], expected)
        # A raw copy must not coexist with the Helm-rendered ObjectStore.
        self.assertFalse(
            (ROOT / "07-k3s-postgres/resources/objectstore-resources.yaml").exists()
        )

    def test_r2_endpoint_validation_is_strict(self) -> None:
        app = Repository().apps["k3s-postgres"]
        (source,) = [s for s in app["spec"]["sources"] if "helm" in s]
        command = [
            "helm",
            "template",
            "k3s-postgres",
            str(ROOT / source["path"]),
            "--namespace",
            app["spec"]["destination"]["namespace"],
            "-f",
            "-",
        ]

        def render(values: dict[str, Any]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                command,
                input=yaml.safe_dump(values),
                capture_output=True,
                text=True,
                timeout=30,
            )

        host = "r2.cloudflarestorage.com"
        account = "0" * 31 + "a"
        endpoint = f"https://{account}.{host}"
        missing: list[dict[str, Any]] = [{}, {"r2": {}}, {"r2": {"endpoint": ""}}]
        for values in missing:
            failure = render(values)
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn("r2.endpoint is required", failure.stderr)
            self.assertFalse(failure.stdout)
        for invalid in (
            False,
            f"http://{account}.{host}",
            f"https://{account.upper()}.{host}",
            f"https://{account[:-1]}.{host}",
            f"https://{account}.us.{host}",
            f"{endpoint}.example.invalid",
            f"{endpoint}:443",
            f"{endpoint}/",
            f"{endpoint}\n",
            f"{endpoint}\r",
            f"{endpoint} ",
        ):
            with self.subTest(invalid=invalid):
                failure = render({"r2": {"endpoint": invalid}})
                self.assertNotEqual(failure.returncode, 0)
                self.assertIn("r2.endpoint must be", failure.stderr)
                self.assertFalse(failure.stdout)
        for jurisdiction in ("", ".eu", ".fedramp"):
            with self.subTest(jurisdiction=jurisdiction):
                valid = f"https://{account}{jurisdiction}.{host}"
                result = render({"r2": {"endpoint": valid}})
                self.assertEqual(result.returncode, 0, result.stderr)
                (store,) = parse_documents(result.stdout)
                self.assertEqual(store["spec"]["configuration"]["endpointURL"], valid)


if __name__ == "__main__":
    unittest.main()
