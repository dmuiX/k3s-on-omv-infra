import unittest
from contracts import ROOT, text


class RenderSchemaContractTests(unittest.TestCase):
    def test_known_values_schemas_are_checked_in(self):
        expected = {
            "cert-manager", "cloudnative-pg", "k8up", "kube-prometheus-stack",
            "longhorn", "openbao", "plugin-barman-cloud",
        }
        found = {path.parent.name for path in ROOT.glob("values-schemas/*/values.schema.json")}
        self.assertTrue(expected <= found)

    def test_unverified_schema_or_compatibility_is_not_claimed_supported(self):
        registry = text("deployment-blockers.json")
        for marker in (
            "CROSSPLANE_VALUES_SCHEMA_UNVERIFIED",
            "VSO_VALUES_SCHEMA_UNVERIFIED",
            "ISTIO_AMBIENT_COMPATIBILITY_UNVERIFIED",
            "OPENBAO_CONFIG_PROVIDER_SCHEMA_UNVERIFIED",
            "FLUENT_BIT_RSYSLOG_INPUT_UNRESOLVED",
            "BACKUP_VALIDATION_SCHEMAS_AND_IMAGES_UNRESOLVED",
        ):
            self.assertIn(marker, registry)

    def test_required_ambient_namespace_contracts_are_declared(self):
        paths = (
            "01-bootstrap/argocd-config/app.yml",
            "02-controllers/cert-manager/app.yml",
            "02-controllers/k8up/app.yml",
            "02-controllers/crossplane/app.yml",
            "02-controllers/vault-secrets-operator/app.yml",
            "02-controllers/cloudnative-pg/app.yml",
            "04-core/openbao/app.yml",
            "08-observability/kube-prometheus-stack/app.yml",
        )
        for path in paths:
            self.assertIn("istio.io/dataplane-mode: ambient", text(path), path)

    def test_openbao_chart_has_no_invented_snapshot_values(self):
        values = text("04-core/openbao/values.yml")
        self.assertNotIn("snapshotAgent:", values)
        self.assertNotIn("s3ExpireDays", values)


if __name__ == "__main__":
    unittest.main()
