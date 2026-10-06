"""Offline bootstrap checks. No OpenBao network connection or admin token."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
import warnings
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "04-openbao-access-config/bootstrap/bootstrap.py"
spec = importlib.util.spec_from_file_location("openbao_bootstrap", SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTest(unittest.TestCase):
    def test_preserves_existing_mount_and_auth_and_binds_only_dedicated_sa(self):
        role = {"data": {"bound_service_account_names": ["vault-secrets-webhook"],
                         "bound_service_account_namespaces": ["vault-secrets-webhook"],
                         "token_policies": ["cert-manager-cloudflare-read"]}}
        restrictions = {"audience": "https://kubernetes.default.svc",
                        "alias_name_source": "serviceaccount_name",
                        "token_max_ttl": 600, "token_explicit_max_ttl": 600,
                        "token_period": 0, "token_bound_cidrs": ["192.0.2.0/24"],
                        "token_no_default_policy": True, "token_num_uses": 10,
                        "token_type": "service"}
        replies = [{"kv/": {"type": "kv", "options": {"version": "2"}}},
                   {"kubernetes/": {"type": "kubernetes"}, "userpass/": {"type": "userpass"}},
                   {"data": {"kubernetes_host": "https://kubernetes.default.svc:443"}},
                   {"data": {"bound_service_account_names": ["openbao-access-config"],
                             "bound_service_account_namespaces": ["openbao"], **restrictions}},
                   role, {}, {}, {}, {}, {}]
        with patch.object(bootstrap.getpass, "getpass", return_value="dummy-admin-token"), \
             patch("builtins.input", return_value=""), \
             patch.object(bootstrap.reconcile, "request", side_effect=replies) as api, \
             patch.object(bootstrap.reconcile, "verify_mount") as verify:
            bootstrap.main()
        verify.assert_called_once_with("dummy-admin-token", "kv")
        writer_role = [c for c in api.call_args_list
                       if c.args[0] == "auth/kubernetes/role/openbao-access-config" and "payload" in c.kwargs]
        self.assertEqual(len(writer_role), 1)
        self.assertEqual(writer_role[0].kwargs["payload"]["bound_service_account_names"], ["openbao-access-config"])
        self.assertEqual(writer_role[0].kwargs["payload"]["bound_service_account_namespaces"], ["openbao"])
        self.assertNotIn("dummy-admin-token", writer_role[0].kwargs["payload"].values())
        for key, value in restrictions.items():
            self.assertEqual(writer_role[0].kwargs["payload"][key], value)

    def test_role_drift_fails_before_any_policy_write(self):
        for drift in ({"bound_service_account_names": ["unexpected-account"]},
                      {"token_policies": ["unreviewed-admin"]}):
            existing = {"bound_service_account_names": ["vault-secrets-webhook"],
                        "bound_service_account_namespaces": ["vault-secrets-webhook"],
                        "token_policies": ["vault-secrets-webhook-read"], **drift}
            replies = [{"kv/": {"type": "kv", "options": {"version": "2"}}},
                       {"kubernetes/": {"type": "kubernetes"}, "userpass/": {"type": "userpass"}},
                       {"data": {"kubernetes_host": "https://kubernetes.default.svc:443"}},
                       None, {"data": existing}]
            with self.subTest(drift=drift), \
                 patch.object(bootstrap.getpass, "getpass", return_value="dummy-admin-token"), \
                 patch.object(bootstrap.reconcile, "request", side_effect=replies) as api, \
                 patch.object(bootstrap.reconcile, "verify_mount"), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(bootstrap.reconcile.RequestFailure):
                    bootstrap.main()
            self.assertEqual(api.call_count, 5)
            self.assertTrue(all("payload" not in call.kwargs for call in api.call_args_list))

    def test_rejects_wrong_kubernetes_auth_mount_type(self):
        with patch.object(bootstrap.getpass, "getpass", return_value="dummy-admin-token"), \
             patch.object(bootstrap.reconcile, "request", side_effect=[
                 {"kv/": {"type": "kv", "options": {"version": "2"}}},
                 {"kubernetes/": {"type": "userpass"}}]) as api, \
             patch.object(bootstrap.reconcile, "verify_mount"):
            with self.assertRaises(bootstrap.reconcile.RequestFailure):
                bootstrap.main()
        self.assertTrue(all("payload" not in call.kwargs for call in api.call_args_list))

    def test_rejects_unconfigured_existing_kubernetes_auth_mount(self):
        # A prior run can enable the mount but fail before writing its config.
        for config in (None, {}, {"data": {"kubernetes_host": ""}}):
            with self.subTest(config=config), \
                 patch.object(bootstrap.getpass, "getpass", return_value="dummy-admin-token"), \
                 patch.object(bootstrap.reconcile, "request", side_effect=[
                     {"kv/": {"type": "kv", "options": {"version": "2"}}},
                     {"kubernetes/": {"type": "kubernetes"}, "userpass/": {"type": "userpass"}},
                     config]) as api, \
                 patch.object(bootstrap.reconcile, "verify_mount"):
                with self.assertRaisesRegex(bootstrap.reconcile.RequestFailure, "unconfigured"):
                    bootstrap.main()
            self.assertTrue(all("payload" not in call.kwargs for call in api.call_args_list))

    def test_creates_missing_kv_and_auth_mounts(self):
        replies = [{}, {}, {}, {}, {}, {}, None, None, {}, {}, {}, {}, {}]
        with patch.object(bootstrap.getpass, "getpass", return_value="dummy-admin-token"), \
             patch("builtins.input", return_value=""), \
             patch.object(bootstrap.reconcile, "request", side_effect=replies) as api, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            bootstrap.main()
        paths = [call.args[0] for call in api.call_args_list]
        self.assertIn("sys/mounts/kv", paths)
        self.assertIn("sys/auth/kubernetes", paths)
        self.assertIn("auth/kubernetes/config", paths)
        self.assertIn("sys/auth/userpass", paths)
        self.assertIn("sys/policies/acl/human-admin", paths)
        self.assertNotIn("dummy-admin-token", stdout.getvalue())

    def test_writer_binding_drift_fails_before_policy_or_role_writes(self):
        for drift in ({"bound_service_account_names": ["*"]},
                      {"bound_service_account_namespaces": ["*"]},
                      {"bound_service_account_namespace_selector": '{"matchLabels":{"access":"enabled"}}'},
                      {"bound_service_account_names": []}):
            writer = {"bound_service_account_names": ["openbao-access-config"],
                      "bound_service_account_namespaces": ["openbao"], **drift}
            replies = [{"kv/": {"type": "kv", "options": {"version": "2"}}},
                       {"kubernetes/": {"type": "kubernetes"}, "userpass/": {"type": "userpass"}},
                       {"data": {"kubernetes_host": "https://kubernetes.default.svc:443"}},
                       {"data": writer}]
            with self.subTest(drift=drift), \
                 patch.object(bootstrap.getpass, "getpass", return_value="dummy-admin-token"), \
                 patch.object(bootstrap.reconcile, "request", side_effect=replies) as api, \
                 patch.object(bootstrap.reconcile, "verify_mount"), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(bootstrap.reconcile.RequestFailure):
                    bootstrap.main()
            self.assertEqual(api.call_count, 4)
            self.assertTrue(all("payload" not in call.kwargs for call in api.call_args_list))

    def test_hidden_input_failure_never_reads_echoed_secret(self):
        def fallback(prompt):
            warnings.warn("Can not control echo on the terminal.", bootstrap.getpass.GetPassWarning)
            return input(prompt)

        with patch.object(bootstrap.getpass, "getpass", side_effect=fallback), \
             patch("builtins.input") as echoed_input:
            with self.assertRaisesRegex(bootstrap.reconcile.RequestFailure, "Hidden input unavailable"):
                bootstrap.read_secret("Secret: ")
        echoed_input.assert_not_called()

    def test_creates_personal_user_without_logging_password(self):
        with patch("builtins.input", return_value="daniel"), \
             patch.object(bootstrap.getpass, "getpass", side_effect=["dummy-long-password", "dummy-long-password"]), \
             patch.object(bootstrap.reconcile, "request", side_effect=[None, {}]) as api, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            bootstrap.create_human_user("dummy-admin-token", "personal")
        self.assertEqual(api.call_args_list[1].args[0], "auth/personal/users/daniel")
        self.assertEqual(api.call_args_list[1].kwargs["payload"],
                         {"password": "dummy-long-password", "policies": "human-admin"})
        self.assertNotIn("dummy-long-password", stdout.getvalue())

    def test_existing_personal_user_password_is_not_reset(self):
        with patch("builtins.input", return_value="daniel"), \
             patch.object(bootstrap.getpass, "getpass") as password_prompt, \
             patch.object(bootstrap.reconcile, "request", return_value={"data": {"policies": ["human-admin"]}}) as api:
            bootstrap.create_human_user("dummy-admin-token", "userpass")
        password_prompt.assert_not_called()
        self.assertEqual(api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
