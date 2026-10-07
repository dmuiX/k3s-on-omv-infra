"""Offline unit tests for the mandatory staged OpenBao PKI reconciler."""
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import mock_open, patch

os.environ.setdefault("OPENBAO_ADDR", "http://example.invalid:8200")
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "05-platform/openbao-pki/workload/reconcile.py"
SPEC = importlib.util.spec_from_file_location("openbao_pki_reconcile", SCRIPT)
reconcile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconcile)
SERVICE = {"mount": "pki-services", **reconcile.EXPECTED_IDENTITIES["pki-services"]}


class OpenBaoPkiReconcileTest(unittest.TestCase):
    def test_profiles_are_exact_and_limited_to_leaf_signing(self):
        with patch.object(reconcile, "CONFIG_PATH", str(ROOT / "05-platform/openbao-pki/workload/config.json")):
            config = reconcile.load_config()
        self.assertEqual([entry["mount"] for entry in config["mounts"]],
                         ["pki-services", "pki-clients"])
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('sys/mounts/', source)
        self.assertNotIn('/root/generate', source)
        self.assertNotIn('/intermediate/generate', source)
        for entry in config["mounts"]:
            policy = ROOT / "05-platform/openbao-pki/workload/policies" / f'{entry["policy"]}.hcl'
            self.assertEqual(policy.read_text(encoding="utf-8").strip(),
                             f'path "{entry["mount"]}/sign/{entry["pki_role"]}" {{\n'
                             '  capabilities = ["update"]\n}')

    def test_signed_chains_are_verified_before_reconciliation(self):
        mounts = {"data": {"pki-services/": {"type": "pki"},
                            "pki-clients/": {"type": "pki"}}}
        certificate = "-----BEGIN CERTIFICATE-----\nintermediate\n-----END CERTIFICATE-----"
        root = "-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----"
        signing = {"certificate": certificate, "ca_chain": [root], "key_id": "key-uuid",
                   "usage": ["issuing-certificates", "crl-signing"]}
        entries = [{"mount": "pki-services"}, {"mount": "pki-clients"}]
        with patch.object(reconcile, "request", side_effect=[
                mounts, {"data": signing}, {"data": {**signing, "ca_chain": []}}]) as api:
            with self.assertRaisesRegex(reconcile.RequestFailure, "pre-signed CA chain"):
                reconcile.verify_prerequisites("token", entries)
        self.assertTrue(all("payload" not in call.kwargs for call in api.call_args_list))
        self.assertEqual(api.call_args_list[1].args[0], "pki-services/issuer/default")
        self.assertEqual(api.call_args_list[2].args[0], "pki-clients/issuer/default")

    def test_accepts_openbao_comma_separated_signing_usage(self):
        certificate = "-----BEGIN CERTIFICATE-----\nintermediate\n-----END CERTIFICATE-----"
        root = "-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----"
        with patch.object(reconcile, "request", side_effect=[
                {"pki-services/": {"type": "pki"}},
                {"data": {"certificate": certificate, "ca_chain": [root],
                          "key_id": "key-uuid", "usage": "issuing-certificates,crl-signing"}}]):
            reconcile.verify_prerequisites("token", [{"mount": "pki-services"}])

    def test_profiles_have_90_day_distinct_leaf_roles(self):
        services = reconcile.desired_pki_role(SERVICE)
        client_entry = {"mount": "pki-clients", **reconcile.EXPECTED_IDENTITIES["pki-clients"]}
        clients = reconcile.desired_pki_role(client_entry)
        self.assertEqual((services["ttl"], services["max_ttl"]), ("90d", "90d"))
        self.assertEqual((clients["ttl"], clients["max_ttl"]), ("90d", "90d"))
        self.assertEqual(services["allowed_domains"], ["svc", "svc.cluster.local"])
        self.assertEqual(clients["allowed_domains"], ["clients.cluster.local"])
        self.assertEqual((services["server_flag"], services["client_flag"]), (True, False))
        self.assertEqual((clients["server_flag"], clients["client_flag"]), (False, True))
        self.assertFalse(services["allow_ip_sans"] or clients["allow_ip_sans"])
        self.assertFalse(services["allow_wildcard_certificates"] or clients["allow_wildcard_certificates"])
        self.assertEqual(services["allowed_uri_sans"], [])
        self.assertEqual(clients["allowed_other_sans"], [])

    def test_existing_auth_boundary_rejects_binding_audience_or_policy_drift(self):
        exact = {
            "bound_service_account_names": ["openbao-pki-services"],
            "bound_service_account_namespaces": ["cert-manager"],
            "bound_service_account_namespace_selector": "",
            "audience": "vault://openbao-pki-services",
            "token_policies": ["cert-manager-pki-services-sign"],
            "token_no_default_policy": True,
        }
        for drift in (
                {"bound_service_account_names": ["other"]},
                {"bound_service_account_namespaces": ["other"]},
                {"bound_service_account_namespace_selector": '{"matchLabels":{}}'},
                {"audience": "other"},
                {"token_policies": ["admin"]},
                {"token_no_default_policy": False}):
            with self.subTest(drift=drift), \
                    patch.object(reconcile, "request", return_value={"data": {**exact, **drift}}) as api:
                with self.assertRaisesRegex(reconcile.RequestFailure, "identity boundary"):
                    reconcile.verify_existing_auth_boundary("token", SERVICE)
            self.assertEqual(api.call_count, 1)
            self.assertNotIn("payload", api.call_args.kwargs)

    def test_auth_role_has_exact_identity_audience_and_no_default_policy(self):
        desired = reconcile.desired_auth_role(SERVICE)
        self.assertEqual(desired, {
            "bound_service_account_names": ["openbao-pki-services"],
            "bound_service_account_namespaces": ["cert-manager"],
            "audience": "vault://openbao-pki-services",
            "token_policies": ["cert-manager-pki-services-sign"],
            "token_ttl": "1h",
            "token_max_ttl": "1h",
            "token_explicit_max_ttl": 0,
            "token_period": 0,
            "token_num_uses": 0,
            "token_bound_cidrs": [],
            "token_type": "default",
            "alias_name_source": "serviceaccount_uid",
            "bound_service_account_namespace_selector": "",
            "token_no_default_policy": True,
        })

    def test_reconcile_only_writes_expected_role_endpoint(self):
        with patch.object(reconcile, "request", side_effect=[None, {}]) as api:
            reconcile.reconcile_pki_role("token", SERVICE)
        self.assertEqual(api.call_args_list[-1].args[0], "pki-services/roles/services")
        payload = api.call_args_list[-1].kwargs["payload"]
        self.assertEqual(payload["max_ttl"], "90d")
        self.assertEqual(payload["issuer_ref"], "default")

    def test_policy_rejects_expanded_capabilities_before_api_calls(self):
        expanded = 'path "pki-services/sign/services" { capabilities = ["update", "sudo"] }'
        with patch("builtins.open", mock_open(read_data=expanded)), \
             patch.object(reconcile, "request") as api:
            with self.assertRaisesRegex(reconcile.RequestFailure, "exact constrained"):
                reconcile.reconcile_policy("token", SERVICE)
        api.assert_not_called()

    def test_errors_do_not_include_api_response_body(self):
        import urllib.error
        response = urllib.error.HTTPError("http://example.invalid", 403, "hidden", {},
                                         io.BytesIO(b"sensitive-token"))
        with patch.object(reconcile.OPENER, "open", side_effect=response):
            with self.assertRaises(reconcile.RequestFailure) as error:
                reconcile.request("sys/mounts")
        self.assertNotIn("sensitive-token", str(error.exception))


if __name__ == "__main__":
    unittest.main()
