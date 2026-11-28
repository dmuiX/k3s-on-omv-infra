"""Offline contracts for the OpenBao API (port 8200) Ambient AuthorizationPolicy."""

from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPIFFE = re.compile(r"^cluster\.local/ns/[a-z0-9-]+/sa/[a-z0-9-]+$")


class OpenBaoAPIAuthorizationTest(unittest.TestCase):
    def test_openbao_api_policy_scopes_port_8200_to_audited_principals(self) -> None:
        (policy,) = [
            doc
            for doc in yaml.safe_load_all(
                (ROOT / "04-openbao/resources/authorization.yaml").read_text()
            )
            if doc and doc["metadata"]["name"] == "openbao-api"
        ]
        self.assertEqual(policy["apiVersion"], "security.istio.io/v1")
        self.assertEqual(policy["kind"], "AuthorizationPolicy")
        self.assertEqual(policy["metadata"]["name"], "openbao-api")
        self.assertEqual(policy["metadata"]["namespace"], "openbao")
        self.assertLess(
            int(policy["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]), 0
        )
        self.assertEqual(
            policy["spec"]["selector"],
            {
                "matchLabels": {
                    "app.kubernetes.io/name": "openbao",
                    "app.kubernetes.io/instance": "openbao",
                    "component": "server",
                }
            },
        )
        self.assertEqual(policy["spec"]["action"], "DENY")
        (rule,) = policy["spec"]["rules"]
        self.assertEqual(rule["to"], [{"operation": {"ports": ["8200"]}}])

        principals = rule["from"][0]["source"]["notPrincipals"]
        self.assertEqual(len(principals), len(set(principals)))
        for principal in principals:
            self.assertRegex(principal, SPIFFE)

        allowed = set(principals)
        for required in (
            "cluster.local/ns/openbao/sa/openbao",
            "cluster.local/ns/openbao/sa/openbao-snapshot",
            "cluster.local/ns/istio-gateway/sa/istio-gateway-istio",
            "cluster.local/ns/vault-secrets-operator/"
            "sa/vault-secrets-operator-controller-manager",
            "cluster.local/ns/crossplane-system/sa/provider-vault",
            "cluster.local/ns/longhorn/sa/longhorn-oidc-proxy",
            "cluster.local/ns/tekton-pipelines/sa/tekton-dashboard-oidc-proxy",
            "cluster.local/ns/kyverno/sa/policy-reporter-oidc-proxy",
            "cluster.local/ns/kyverno/sa/policy-reporter-provisioner",
            "cluster.local/ns/opencost/sa/opencost-oidc-proxy",
            "cluster.local/ns/project-bot/sa/project-bot-oidc-proxy",
            "cluster.local/ns/radar/sa/radar-auth-provisioner",
            "cluster.local/ns/opencost/sa/opencost-oidc-provisioner",
            "cluster.local/ns/project-bot/sa/project-bot-oidc-provisioner",
            "cluster.local/ns/audiobookshelf/sa/audiobookshelf-configurator",
        ):
            self.assertIn(required, allowed)

        # Every infra-authored provisioner Job and snapshot CronJob calling 8200
        # must be present in notPrincipals so rollouts cannot lock themselves out.
        for manifest in (
            "06-platform/openbao-backups/resources/snapshot.yaml",
            "06-platform/longhorn-config/resources/provision-credentials.yaml",
            "07-k3s-postgres/resources/provision-credentials.yaml",
            "09-kube-prometheus-stack/resources/provision-credentials.yaml",
        ):
            for doc in yaml.safe_load_all((ROOT / manifest).read_text()):
                if not isinstance(doc, dict):
                    continue
                if doc.get("kind") == "CronJob":
                    pod = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                elif doc.get("kind") == "Job":
                    pod = doc["spec"]["template"]["spec"]
                else:
                    continue
                ns = doc["metadata"]["namespace"]
                sa = pod["serviceAccountName"]
                self.assertIn(f"cluster.local/ns/{ns}/sa/{sa}", allowed)
