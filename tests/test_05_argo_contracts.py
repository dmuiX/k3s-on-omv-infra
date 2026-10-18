import re
import unittest
from contracts import app_files, text


class ArgoContractsTests(unittest.TestCase):
    def test_root_and_children_self_heal_without_automated_prune(self):
        for path in [*app_files()]:
            content = text(path)
            self.assertRegex(content, r"selfHeal:\s*true", str(path))
            self.assertNotRegex(content, r"prune:\s*true", str(path))
        root = text("infra.yml")
        self.assertIn("selfHeal: true", root)
        self.assertIn("prune: false", root)

    def test_exact_source_order(self):
        expected = {
            "argocd-config": ["infra-resource", "live-values"],
            "prometheus-operator-crds": ["chart"],
            "barman-cloud": ["chart", "infra-values", "infra-resource"],
            "cert-manager": ["chart", "infra-values", "live-values"],
            "cloudnative-pg": ["chart", "infra-values", "infra-resource"],
            "crossplane": ["chart", "infra-values"],
            "istio-ambient": ["chart", "chart", "chart", "infra-values"],
            "k8up": ["chart", "infra-values", "infra-resource"],
            "longhorn": ["chart", "infra-values", "live-values", "infra-resource"],
            "vault-secrets-operator": ["chart", "infra-values"],
            "provider-vault": ["infra-resource"],
            "openbao": ["chart", "infra-values", "live-values"],
            "provider-vault-connection": ["infra-resource"],
            "letsencrypt-dns01": ["live-resource"],
            "openbao-backups": ["live-resource"],
            "openbao-config": ["infra-resource"],
            "openbao-pki": ["infra-resource"],
            "postgresql": ["infra-resource"],
            "backup-validation": ["infra-resource"],
            "fluent-bit": ["chart", "infra-values"],
            "kube-prometheus-stack": ["chart", "infra-values", "live-values", "infra-resource", "infra-resource"],
        }
        found = {}
        for path in app_files():
            content = text(path)
            starts = [match.start() for match in re.finditer(r"repoURL:", content)]
            segments = [content[start:(starts[index + 1] if index + 1 < len(starts) else len(content))] for index, start in enumerate(starts)]
            kinds = []
            for segment in segments:
                if "chart:" in segment:
                    kinds.append("chart")
                elif "ref: values" in segment:
                    kinds.append("infra-values")
                elif "ref: private" in segment or "ref: snapshot-values" in segment:
                    kinds.append("live-values")
                elif "k3s-on-omv-live.git" in segment:
                    kinds.append("live-resource")
                else:
                    kinds.append("infra-resource")
            name = re.search(r"(?m)^  name: ([a-z0-9-]+)$", content).group(1)
            found[name] = kinds
        self.assertEqual(found, expected)

    def test_application_health_customization_is_selected(self):
        root = text("infra.yml")
        health = text("01-bootstrap/argocd-config/templates/application-health.yaml")
        self.assertNotIn("application-health.yaml", root)
        self.assertIn("path: 01-bootstrap/argocd-config", text("01-bootstrap/argocd-config/app.yml"))
        self.assertIn("resource.customizations.health.argoproj.io_Application", health)

    def test_provider_vault_exact_activation_and_ordering(self):
        activation = text("03-controllers/provider-vault/database-resource-activation.yaml")
        types = re.findall(r"(?m)^    - ([a-z0-9.]+)$", activation)
        expected = [
            "secretbackendconnections.database.vault.upbound.io",
            "secretbackendstaticroles.database.vault.upbound.io",
            "oidcclients.identity.vault.upbound.io", "oidckeys.identity.vault.upbound.io",
            "oidcproviders.identity.vault.upbound.io", "oidcscopes.identity.vault.upbound.io",
            "mounts.vault.vault.upbound.io", "policies.vault.vault.upbound.io",
            "secretbackendv2s.kv.vault.upbound.io", "backends.auth.vault.upbound.io",
            "policies.password.vault.upbound.io", "authbackendroles.kubernetes.vault.upbound.io",
            "secretbackendroles.database.vault.upbound.io", "groups.identity.vault.upbound.io",
            "oidcroles.identity.vault.upbound.io", "secretbackendroles.pki.vault.upbound.io",
            "secretbackendconfigurls.pki.vault.upbound.io",
            "secretbackendcrlconfigs.pki.vault.upbound.io",
            "oidcassignments.identity.vault.upbound.io",
        ]
        self.assertEqual(types, expected)
        self.assertIn('sync-wave: "-20"', activation)
        runtime = text("03-controllers/provider-vault/provider-runtime.yaml")
        self.assertGreaterEqual(runtime.count('sync-wave: "-10"'), 2)
        self.assertIn('sync-wave: "0"', runtime)
        self.assertLess(runtime.index("kind: DeploymentRuntimeConfig"), runtime.index("kind: Provider\n"))


if __name__ == "__main__":
    unittest.main()
