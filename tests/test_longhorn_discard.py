"""Contracts and failure-path tests for the Longhorn v1 discard default."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "06-platform/longhorn-config/resources/encrypted-discard.yaml"
VOLUME = "pvc-6548dde2-ecfa-4b00-a193-eced3b1f6fcf"


class DiscardTests(unittest.TestCase):
    """Exercise the actual ConfigMap program with a simulated host boundary."""

    def setUp(self) -> None:
        self.documents = list(yaml.safe_load_all(MANIFEST.read_text()))
        config = next(x for x in self.documents if x["kind"] == "ConfigMap")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        source = Path(self.temporary.name) / "discard.py"
        source.write_text(config["data"]["discard.py"])
        spec = importlib.util.spec_from_file_location("discard_policy", source)
        assert spec is not None and spec.loader is not None
        self.policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.policy)
        self.policy.STOP.clear()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        self.calls: list[tuple[tuple[str, ...], bool]] = []
        self.active = False
        self.persistent = False
        self.target = "iqn.2019-10.io.longhorn:" + VOLUME
        self.device_id = "8:70"
        self.mapping_id = "8:70"
        self.names = [VOLUME]
        self.fail_refresh = False
        self.kdf_memory = 290116
        self.available_memory = 8 * 1024 * 1024

    def host(
        self, *args: str, key: bool = False, missing_ok: bool = False
    ) -> str | None:
        """Model only commands the policy is permitted to issue."""
        self.calls.append((args, key))
        command = Path(args[0]).name
        if command == "test":
            return ""
        if command == "ls":
            return "\n".join(self.names)
        if command == "stat":
            device = (
                self.device_id
                if args[-1].startswith("/dev/longhorn/")
                else self.mapping_id
            )
            return "block special file|" + device
        if command == "readlink":
            return "/sys/devices/platform/host7/session7/target7:0:0/7:0:0:1"
        if command == "cat":
            if args[-1] == "/proc/meminfo":
                return f"MemAvailable: {self.available_memory} kB"
            return self.target
        if command == "cryptsetup" and args[1] == "status":
            flags = "  flags: discards\n" if self.active else ""
            return "  type: LUKS2\n  device: /dev/sdh\n  mode: read/write\n" + flags
        if command == "cryptsetup" and args[1] == "luksDump":
            return json.dumps(
                {
                    "config": {"flags": ["allow-discards"] if self.persistent else []},
                    "keyslots": {"0": {"kdf": {"memory": self.kdf_memory}}},
                }
            )
        if command == "flock":
            if self.fail_refresh:
                raise self.policy.Failure("Host operation failed: flock")
            self.active = True
            self.persistent = True
            return ""
        raise AssertionError("Unexpected host operation: " + repr(args))

    def reconcile(self, audit: bool = False) -> bool:
        with patch.object(self.policy, "host", self.host):
            return bool(self.policy.reconcile(audit=audit))

    def test_enables_and_verifies_persistent_discard_once(self) -> None:
        self.assertTrue(self.reconcile())
        writes = [(args, key) for args, key in self.calls if key]
        self.assertEqual(len(writes), 1)
        self.assertIn("--persistent", writes[0][0])
        self.assertIn("--allow-discards", writes[0][0])
        self.assertIn("--key-file=-", writes[0][0])
        self.calls.clear()
        self.assertTrue(self.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_active_but_not_persistent_is_not_a_completed_policy(self) -> None:
        self.active = True
        self.assertTrue(self.reconcile())
        self.assertTrue(any(key for _, key in self.calls))
        self.assertTrue(self.persistent)

    def test_audit_is_read_only_and_reports_missing_discard(self) -> None:
        self.assertFalse(self.reconcile(audit=True))
        self.assertFalse(any(key for _, key in self.calls))

    def test_wrong_iscsi_identity_cannot_touch_host_disk(self) -> None:
        self.target = "iqn.example:host-system-disk"
        self.assertFalse(self.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_mapping_backing_device_mismatch_fails_closed(self) -> None:
        self.mapping_id = "259:0"
        self.assertFalse(self.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_path_injection_is_rejected_without_device_access(self) -> None:
        self.names = ["../nvme0n1", "--help", "bad;touch /tmp/owned"]
        self.assertFalse(self.reconcile())
        self.assertFalse(
            any(Path(args[0]).name == "cryptsetup" for args, _ in self.calls)
        )

    def test_no_devices_is_healthy_and_does_not_read_key(self) -> None:
        self.names = []
        self.assertTrue(self.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_unattached_or_unencrypted_mapping_is_skipped(self) -> None:
        def host(*args: str, **kwargs: bool) -> str | None:
            if args[:2] == ("/usr/bin/test", "-b") and args[-1].startswith(
                "/dev/mapper/"
            ):
                return None
            return self.host(*args, **kwargs)

        with patch.object(self.policy, "host", host):
            self.assertTrue(self.policy.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_key_failure_does_not_claim_success_or_log_sensitive_output(self) -> None:
        self.fail_refresh = True
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.reconcile())
        self.assertIn("failed", output.getvalue().lower())
        self.assertNotIn("password", output.getvalue().lower())

    def test_excessive_kdf_memory_is_not_attempted(self) -> None:
        self.kdf_memory = 2 * 1024 * 1024
        self.assertFalse(self.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_large_existing_kdf_is_supported_with_sufficient_host_memory(self) -> None:
        self.kdf_memory = 1024 * 1024
        self.assertTrue(self.reconcile())
        self.assertTrue(any(key for _, key in self.calls))

    def test_nondefault_flags_are_not_silently_overwritten(self) -> None:
        for persistent in (False, True):

            def host(*args: str, **kwargs: bool) -> str | None:
                result = self.host(*args, **kwargs)
                if args[:2] == ("/usr/sbin/cryptsetup", "status") and not persistent:
                    return str(result) + "  flags: same_cpu_crypt\n"
                if args[:2] == ("/usr/sbin/cryptsetup", "luksDump") and persistent:
                    metadata = json.loads(str(result))
                    metadata["config"]["flags"] = ["same_cpu_crypt"]
                    return json.dumps(metadata)
                return result

            with self.subTest(persistent=persistent), patch.object(
                self.policy, "host", host
            ):
                self.assertFalse(self.policy.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_low_host_memory_defers_authentication_without_reading_key(self) -> None:
        self.available_memory = self.kdf_memory
        self.assertFalse(self.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_already_persistent_volume_does_not_need_kdf_memory(self) -> None:
        self.active = self.persistent = True
        self.available_memory = 0
        self.assertTrue(self.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_host_wrapper_never_logs_failed_process_output(self) -> None:
        process = Mock(returncode=5, stdout=None, stderr=None)
        process.communicate.return_value = (b"SENSITIVE", b"SENSITIVE")
        with patch.object(self.policy.subprocess, "Popen", return_value=process):
            with self.assertRaises(self.policy.Failure) as error:
                self.policy.host("/usr/bin/ls", "/dev/longhorn")
        self.assertNotIn("SENSITIVE", str(error.exception))

    def test_inventory_failure_cannot_report_ready(self) -> None:
        with patch.object(
            self.policy, "host", side_effect=self.policy.Failure("unavailable")
        ):
            with patch("sys.argv", ["discard.py", "--once"]):
                self.assertEqual(self.policy.main(), 1)

    def test_absent_longhorn_directory_is_a_noop(self) -> None:
        with patch.object(self.policy, "host", return_value=None):
            self.assertTrue(self.policy.reconcile())

    def test_bad_device_type_and_non_iscsi_are_rejected(self) -> None:
        for failed_command, value in [
            ("stat", "regular file|8:70"),
            ("readlink", "/sys/devices/pci/nvme0n1"),
        ]:

            def host(*args: str, **kwargs: bool) -> str | None:
                if Path(args[0]).name == failed_command:
                    return value
                return self.host(*args, **kwargs)

            with self.subTest(command=failed_command), patch.object(
                self.policy, "host", host
            ):
                self.assertFalse(self.policy.reconcile())
        self.assertFalse(any(key for _, key in self.calls))

    def test_postcondition_failure_does_not_report_success(self) -> None:
        def host(*args: str, **kwargs: bool) -> str | None:
            result = self.host(*args, **kwargs)
            if kwargs.get("key"):
                self.active = False
            return result

        with patch.object(self.policy, "host", host):
            self.assertFalse(self.policy.reconcile())

    def test_readiness_audits_actual_flags_without_reading_key(self) -> None:
        self.active = self.persistent = True
        with patch.object(self.policy, "host", self.host):
            with patch("sys.argv", ["discard.py", "--audit-only"]):
                self.assertEqual(self.policy.main(), 0)
                self.active = False
                self.assertEqual(self.policy.main(), 1)
        self.assertFalse(any(key for _, key in self.calls))

    def test_host_spawn_error_and_missing_result_are_sanitized(self) -> None:
        with patch.object(
            self.policy.subprocess,
            "Popen",
            side_effect=OSError("SENSITIVE"),
        ):
            with self.assertRaises(self.policy.Failure) as error:
                self.policy.host("/usr/bin/ls")
        self.assertNotIn("SENSITIVE", str(error.exception))
        with self.assertRaises(self.policy.Failure):
            self.policy.required(None)
        process = Mock(returncode=1, stdout=None, stderr=None)
        process.communicate.return_value = (b"SENSITIVE", b"SENSITIVE")
        with patch.object(self.policy.subprocess, "Popen", return_value=process):
            self.assertIsNone(self.policy.host("/usr/bin/test", missing_ok=True))

    def test_key_is_passed_by_file_descriptor_not_in_argv(self) -> None:
        key = Path(self.temporary.name) / "synthetic-key"
        key.write_bytes(b"SYNTHETIC_TEST_ONLY")
        process = Mock(returncode=0, stdout=None, stderr=None)
        process.communicate.return_value = (b"", b"")
        with patch.object(self.policy, "KEY", key), patch.object(
            self.policy.subprocess, "Popen", return_value=process
        ) as run:
            self.policy.host(
                "/usr/sbin/cryptsetup", "--key-file=-", "refresh", VOLUME, key=True
            )
        args, kwargs = run.call_args
        self.assertNotIn("SYNTHETIC_TEST_ONLY", repr(args))
        self.assertNotIn(str(key), repr(args))
        self.assertIn("--mount", args[0])
        self.assertIn("--ipc", args[0])
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["stdin"].closed)

    def test_once_mode_runs_real_reconcile_and_audit_cannot_refresh(self) -> None:
        with patch.object(self.policy, "host", self.host):
            with patch("sys.argv", ["discard.py", "--audit-only"]):
                self.assertEqual(self.policy.main(), 1)
            self.assertFalse(any(key for _, key in self.calls))
            with patch("sys.argv", ["discard.py", "--once"]):
                self.assertEqual(self.policy.main(), 0)

    def test_continuous_process_stops_without_leaving_state_files(self) -> None:
        def reconcile() -> bool:
            self.policy.STOP.set()
            return True

        with patch.object(self.policy, "reconcile", reconcile), patch.object(
            self.policy.signal, "signal"
        ):
            with patch("sys.argv", ["discard.py"]):
                self.assertEqual(self.policy.main(), 0)
        self.policy.STOP.clear()

    def test_daemon_inventory_failure_is_sanitized_and_retryable(self) -> None:
        def reconcile() -> bool:
            self.policy.STOP.set()
            raise self.policy.Failure("SENSITIVE")

        output = io.StringIO()
        with patch.object(self.policy, "reconcile", reconcile), patch.object(
            self.policy.signal, "signal"
        ), contextlib.redirect_stdout(output):
            with patch("sys.argv", ["discard.py"]):
                self.assertEqual(self.policy.main(), 0)
        self.assertIn("inventory failed", output.getvalue())
        self.assertNotIn("SENSITIVE", output.getvalue())
        self.policy.STOP.clear()

    def test_terminating_process_stops_before_mutating_a_device(self) -> None:
        self.policy.STOP.set()
        self.assertFalse(self.reconcile())
        self.assertFalse(any(key for _, key in self.calls))
        self.policy.STOP.clear()

    def test_script_digest_forces_rollout_on_configmap_change(self) -> None:
        config = next(x for x in self.documents if x["kind"] == "ConfigMap")
        daemon = next(x for x in self.documents if x["kind"] == "DaemonSet")
        digest = hashlib.sha256(config["data"]["discard.py"].encode()).hexdigest()
        self.assertEqual(
            daemon["spec"]["template"]["metadata"]["annotations"][
                "checksum/discard-policy"
            ],
            digest,
        )

    def test_pod_has_no_api_token_or_network_and_only_one_secret_key(self) -> None:
        daemon = next(x for x in self.documents if x["kind"] == "DaemonSet")
        spec = daemon["spec"]["template"]["spec"]
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertTrue(spec["hostPID"])
        self.assertFalse(spec.get("hostNetwork", False))
        self.assertFalse(any("hostPath" in v for v in spec["volumes"]))
        secret = next(v["secret"] for v in spec["volumes"] if "secret" in v)
        self.assertEqual(secret["secretName"], "longhorn-crypto")
        self.assertEqual(secret["items"], [{"key": "CRYPTO_KEY_VALUE", "path": "key"}])
        container = spec["containers"][0]
        self.assertIn("@sha256:", container["image"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertIn("--audit-only", container["readinessProbe"]["exec"]["command"])
        self.assertIn("livenessProbe", container)
        network = next(x for x in self.documents if x["kind"] == "NetworkPolicy")
        self.assertEqual(network["spec"]["ingress"], [])
        self.assertEqual(network["spec"]["egress"], [])
        self.assertFalse(
            any(x["kind"] in {"Role", "ClusterRole", "Secret"} for x in self.documents)
        )

    def test_existing_application_and_bootstrap_secret_own_the_policy(self) -> None:
        app = yaml.safe_load((ROOT / "06-platform/longhorn-config/app.yml").read_text())
        self.assertIn(
            "06-platform/longhorn-config/resources",
            [source.get("path") for source in app["spec"]["sources"]],
        )
        self.assertTrue(app["spec"]["syncPolicy"]["automated"]["enabled"])
        self.assertNotIn("StorageClass", [x["kind"] for x in self.documents])
        self.assertNotIn("PersistentVolumeClaim", [x["kind"] for x in self.documents])

    def test_native_trim_job_does_not_delete_snapshots_or_attach_detached_volumes(
        self,
    ) -> None:
        job = next(x for x in self.documents if x["kind"] == "RecurringJob")
        self.assertEqual(job["spec"]["task"], "filesystem-trim")
        self.assertEqual(job["spec"]["cron"], "30 18 * * *")
        self.assertEqual(job["spec"]["groups"], ["default"])
        self.assertEqual(job["spec"]["concurrency"], 1)
        self.assertNotIn("Setting", [x["kind"] for x in self.documents])


if __name__ == "__main__":
    unittest.main()
