"""Offline tests for OpenBao IaC; no OpenBao connection or credentials."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENBAO_ADDR", "http://example.invalid:8200")
SCRIPT = Path(__file__).resolve().parents[1] / "04-openbao-access-config/workload/reconcile.py"
spec = importlib.util.spec_from_file_location("openbao_reconcile", SCRIPT)
reconcile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconcile)
ROLE = {"name": "vault-secrets-webhook", "service_account": "vault-secrets-webhook",
        "namespace": "vault-secrets-webhook", "policies": ["vault-secrets-webhook-read"], "token_ttl": "1h"}
MOUNT = {"kv/": {"type": "kv", "options": {"version": "2"}}}


class ReconcileTest(unittest.TestCase):
    def test_initial_reconcile_and_second_noop(self):
        old_role = {"data": {"bound_service_account_names": ["vault-secrets-webhook"],
                             "bound_service_account_namespaces": ["vault-secrets-webhook"],
                             "token_policies": ["cert-manager-cloudflare-read"], "token_ttl": 3600}}
        role_current = {"data": {**old_role["data"], "token_policies": ["vault-secrets-webhook-read"]}}
        desired = 'path "kv/data/*" {}'
        for current_policy, role, writes in [(None, old_role, 2),
                                              ({"data": {"policy": desired}}, role_current, 0)]:
            responses = [{"auth": {"client_token": "dummy-token"}}, MOUNT,
                         {"userpass/": {"type": "userpass"}}, role]
            if role == old_role:
                responses.append({})
            responses.append(current_policy)
            if current_policy is None:
                responses.append({})
            responses.append({"data": {"policy": 'path "sys/auth" {}'}})
            files = [io.StringIO("dummy-jwt"), io.StringIO(json.dumps({
                "kv_mount": "kv", "human_auth_mount": "userpass", "webhook_role": ROLE})),
                     io.StringIO(desired), io.StringIO('path "sys/auth" {}')]
            with patch.object(reconcile, "request", side_effect=responses) as api, \
                 patch("builtins.open", side_effect=files), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout:
                reconcile.main()
            self.assertNotIn("dummy-token", stdout.getvalue())
            self.assertNotIn("dummy-jwt", stdout.getvalue())
            self.assertEqual(sum(1 for call in api.call_args_list if "payload" in call.kwargs), 1 + writes)

    def test_role_drift_fails_before_any_policy_write(self):
        for drift in ({"bound_service_account_names": ["unexpected-account"]},
                      {"token_policies": ["unreviewed-admin"]}):
            existing = {"bound_service_account_names": [ROLE["service_account"]],
                        "bound_service_account_namespaces": [ROLE["namespace"]],
                        "token_policies": ROLE["policies"], **drift}
            files = [io.StringIO("dummy-jwt"), io.StringIO(json.dumps({
                "kv_mount": "kv", "human_auth_mount": "userpass", "webhook_role": ROLE}))]
            responses = [{"auth": {"client_token": "dummy-token"}}, MOUNT,
                         {"userpass/": {"type": "userpass"}}, {"data": existing}]
            with self.subTest(drift=drift), patch.object(reconcile, "request", side_effect=responses) as api, \
                 patch("builtins.open", side_effect=files):
                with self.assertRaises(reconcile.RequestFailure):
                    reconcile.main()
            self.assertEqual(api.call_count, 4)
            self.assertEqual([c.args[0] for c in api.call_args_list if "payload" in c.kwargs],
                             ["auth/kubernetes/login"])

    def test_rejects_other_role_policies(self):
        with patch.object(reconcile, "request", return_value={"data": {
                "bound_service_account_names": ["vault-secrets-webhook"],
                "bound_service_account_namespaces": ["vault-secrets-webhook"],
                "token_policies": ["unreviewed-admin"]}}) as api:
            with self.assertRaises(reconcile.RequestFailure):
                reconcile.reconcile_role("dummy-token", ROLE)
            self.assertEqual(api.call_count, 1)

    def test_rejects_missing_or_empty_existing_role_bindings(self):
        for field in ("bound_service_account_names", "bound_service_account_namespaces"):
            for value in (None, [], ""):
                with self.subTest(field=field, value=value), \
                     patch.object(reconcile, "request", return_value={"data": {
                         "bound_service_account_names": [ROLE["service_account"]],
                         "bound_service_account_namespaces": [ROLE["namespace"]],
                         field: value}}) as api:
                    with self.assertRaises(reconcile.RequestFailure):
                        reconcile.reconcile_role("dummy-token", ROLE)
                    self.assertEqual(api.call_count, 1)

    def test_rejects_namespace_selector_even_when_role_otherwise_current(self):
        existing = {"bound_service_account_names": [ROLE["service_account"]],
                    "bound_service_account_namespaces": [ROLE["namespace"]],
                    "token_policies": ROLE["policies"], "token_ttl": 3600,
                    "bound_service_account_namespace_selector": '{"matchLabels":{"webhook":"enabled"}}'}
        with patch.object(reconcile, "request", return_value={"data": existing}) as api:
            with self.assertRaises(reconcile.RequestFailure):
                reconcile.reconcile_role("dummy-token", ROLE)
        self.assertEqual(api.call_count, 1)

    def test_empty_namespace_selector_allows_current_role(self):
        existing = {"bound_service_account_names": [ROLE["service_account"]],
                    "bound_service_account_namespaces": [ROLE["namespace"]],
                    "token_policies": ROLE["policies"], "token_ttl": 3600,
                    "bound_service_account_namespace_selector": ""}
        with patch.object(reconcile, "request", return_value={"data": existing}) as api:
            reconcile.reconcile_role("dummy-token", ROLE)
        self.assertEqual(api.call_count, 1)

    def test_rejects_zero_and_malformed_ttl_before_api_calls(self):
        for ttl in ("0s", "0m", "0h", "00s", "-1h", "1.5h", "1d", "", "١h", None, True):
            with self.subTest(ttl=ttl), patch.object(reconcile, "request") as api:
                with self.assertRaisesRegex(reconcile.RequestFailure, "positive token TTL"):
                    reconcile.reconcile_role("dummy-token", {**ROLE, "token_ttl": ttl})
            api.assert_not_called()

    def test_positive_ttl_units_match_existing_seconds(self):
        for ttl, seconds in (("1s", 1), ("1m", 60), ("1h", 3600), ("01h", 3600)):
            existing = {"bound_service_account_names": [ROLE["service_account"]],
                        "bound_service_account_namespaces": [ROLE["namespace"]],
                        "token_policies": ROLE["policies"], "token_ttl": seconds}
            with self.subTest(ttl=ttl), \
                 patch.object(reconcile, "request", return_value={"data": existing}) as api, \
                 contextlib.redirect_stdout(io.StringIO()):
                reconcile.reconcile_role("dummy-token", {**ROLE, "token_ttl": ttl})
            self.assertEqual(api.call_count, 1)
            self.assertNotIn("payload", api.call_args.kwargs)

    def test_preserves_explicit_token_lifetime_limit(self):
        existing = {"bound_service_account_names": [ROLE["service_account"]],
                    "bound_service_account_namespaces": [ROLE["namespace"]],
                    "token_policies": ["cert-manager-cloudflare-read"],
                    "token_explicit_max_ttl": 7200}
        with patch.object(reconcile, "request", side_effect=[{"data": existing}, {}]) as api:
            reconcile.reconcile_role("dummy-token", ROLE)
        self.assertEqual(api.call_args.kwargs["payload"]["token_explicit_max_ttl"], 7200)

    def test_proxy_settings_are_ignored(self):
        # Import with a proxy configured; neither environment nor OS proxy discovery
        # may route localhost admin tokens or in-cluster Job tokens elsewhere.
        with patch("urllib.request.getproxies", return_value={"http": "http://proxy.invalid:8080"}) as proxies:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        proxies.assert_not_called()

    def test_redirects_are_rejected(self):
        import urllib.error
        import urllib.request
        # Exercise urllib's actual redirect handling without a network request.
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code), patch.object(reconcile.OPENER, "open") as redirected_request:
                req = urllib.request.Request("http://example.invalid/v1/sys/auth",
                                             headers={"X-Vault-Token": "dummy-token"})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    reconcile.OPENER.error("http", req, io.BytesIO(), code, "redirect",
                                           {"location": "http://other.invalid/"})
                error.exception.close()
                redirected_request.assert_not_called()

    def test_rejects_wrong_kv_version(self):
        with patch.object(reconcile, "request", return_value={"kv/": {"type": "kv", "options": {"version": "1"}}}):
            with self.assertRaises(reconcile.RequestFailure):
                reconcile.verify_mount("dummy-token", "kv")

    def test_userpass_mount_must_not_be_silently_recreated(self):
        with patch.object(reconcile, "request", return_value={}) as api:
            with self.assertRaises(reconcile.RequestFailure):
                reconcile.verify_userpass("dummy-token", "userpass")
            self.assertEqual(api.call_count, 1)

    def test_does_not_include_response_body_in_errors(self):
        import urllib.error
        response = urllib.error.HTTPError("http://example.invalid", 403, "hidden", {}, io.BytesIO(b"dummy-secret"))
        try:
            with patch.object(reconcile.OPENER, "open", side_effect=response):
                with self.assertRaises(reconcile.RequestFailure) as error:
                    reconcile.request("sys/policies/acl/vault-secrets-webhook-read")
            self.assertNotIn("dummy-secret", str(error.exception))
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
