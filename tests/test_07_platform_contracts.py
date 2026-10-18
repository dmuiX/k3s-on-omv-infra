import unittest
from contracts import text


class PlatformContractTests(unittest.TestCase):
    def test_longhorn_encryption_and_replication_contracts(self):
        encrypted = text("02-controllers/longhorn/encrypted-storageclass-template.yaml")
        postgres = text("07-data/postgresql/cluster/postgres-storageClass.yaml")
        monitoring = text("02-controllers/longhorn/prometheus-storageclass.yaml")
        self.assertIn("encrypted: \"true\"", encrypted)
        self.assertIn('numberOfReplicas: "3"', encrypted)
        self.assertIn('numberOfReplicas: "1"', postgres)
        self.assertIn('numberOfReplicas: "1"', monitoring)

    def test_postgresql_ha_tls_and_backup_contracts(self):
        cluster = text("07-data/postgresql/cluster/postgres-cluster.yaml")
        self.assertIn("instances: 3", cluster)
        self.assertIn("number: 1", cluster)
        self.assertIn("hostnossl all all all reject", cluster)
        self.assertIn("isWALArchiver: true", cluster)
        self.assertIn("target: prefer-standby", cluster)

    def test_monitoring_ha_contract_is_not_silently_claimed_when_unverified(self):
        values = text("08-observability/kube-prometheus-stack/values.yml")
        # Milestone 2 must expose missing HA values rather than treating runtime
        # acceptance as complete. This remains a static contract check only.
        self.assertIn("retention: 15d", values)
        self.assertIn("retentionSize: 18GB", values)
        self.assertIn("storageClassName: longhorn-monitoring", values)

    def test_backup_thresholds_are_present_in_spec_owned_monitoring(self):
        combined = text("08-observability/kube-prometheus-stack/values.yml") + text("deployment-blockers.json")
        # Validation implementation is blocked; thresholds may not be invented.
        self.assertIn("BACKUP_VALIDATION_SCHEMAS_AND_IMAGES_UNRESOLVED", combined)


if __name__ == "__main__":
    unittest.main()
