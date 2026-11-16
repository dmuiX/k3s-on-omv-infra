"""Resource-only SSA must not adopt the protected ObjectStore configuration."""

from pathlib import Path
from typing import Any
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1] / "07-k3s-postgres"


class BarmanSidecarResourcesTest(unittest.TestCase):
    def test_existing_postgres_application_owns_only_the_sidecar_resource_fields(
        self,
    ) -> None:
        profile: dict[str, Any] = yaml.safe_load(
            (ROOT / "resources/objectstore-resources.yaml").read_text()
        )
        self.assertEqual(profile["apiVersion"], "barmancloud.cnpg.io/v1")
        self.assertEqual(profile["kind"], "ObjectStore")
        self.assertEqual(profile["metadata"]["namespace"], "cloudnative-pg")
        cluster = next(
            yaml.safe_load_all((ROOT / "resources/cluster.yaml").read_text())
        )
        self.assertEqual(
            profile["metadata"]["name"],
            cluster["spec"]["plugins"][0]["parameters"]["barmanObjectName"],
        )
        self.assertEqual(
            profile["spec"],
            {
                "instanceSidecarConfiguration": {
                    "resources": {
                        "requests": {"cpu": "250m", "memory": "192Mi"},
                        "limits": {"cpu": "250m", "memory": "256Mi"},
                    }
                }
            },
        )
        options = set(
            profile["metadata"]["annotations"]["argocd.argoproj.io/sync-options"].split(
                ","
            )
        )
        self.assertEqual(
            options,
            {"ServerSideApply=true", "Validate=false", "Prune=false", "Delete=false"},
        )
        app = yaml.safe_load((ROOT / "app.yml").read_text())
        self.assertIn(
            "ClientSideApplyMigration=false", app["spec"]["syncPolicy"]["syncOptions"]
        )
        self.assertEqual(
            app["metadata"]["annotations"]["argocd.argoproj.io/compare-options"],
            "ServerSideDiff=true",
        )
        self.assertEqual(
            app["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "7"
        )
        self.assertTrue(
            any(
                s.get("path") == "07-k3s-postgres/resources"
                for s in app["spec"]["sources"]
            )
        )


if __name__ == "__main__":
    unittest.main()
