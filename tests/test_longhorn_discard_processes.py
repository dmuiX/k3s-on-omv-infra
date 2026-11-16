"""Real-process regressions for the ConfigMap entrypoint; no keys or devices."""

import ctypes
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "06-platform/longhorn-config/resources/encrypted-discard.yaml"
RUN = subprocess.run


@unittest.skipUnless(
    sys.platform == "linux" and shutil.which("flock"), "Linux/flock required"
)
class ProcessCleanupTests(unittest.TestCase):
    """Exercise the actual host wrapper around real flock and a delayed child."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="discard-process-test-")
        self.root = Path(self.temporary.name)
        self.runners: list[subprocess.Popen[bytes]] = []
        self.libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(self.libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.previous_subreaper = previous.value
        self.assertEqual(self.libc.prctl(36, 1, 0, 0, 0), 0)
        source = next(
            x
            for x in yaml.safe_load_all(MANIFEST.read_text())
            if x["kind"] == "ConfigMap"
        )["data"]["discard.py"]
        self.program = self.root / "discard.py"
        self.program.write_text(source)
        spec = importlib.util.spec_from_file_location("process_policy", self.program)
        assert spec is not None and spec.loader is not None
        self.policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.policy)
        self.policy.STOP.clear()
        self.leader = self.root / "leader.pid"
        self.child = self.root / "child.pid"
        self.marker = self.root / "late-action"
        self.lock = self.root / "lock"
        self.stub = self.root / "nsenter"
        self.stub.write_text(
            f"#!{sys.executable}\nimport os,sys\nfrom pathlib import Path\n"
            f"Path({str(self.leader)!r}).write_text(str(os.getpid()))\n"
            "args=sys.argv[sys.argv.index('--')+1:]\nos.execv(args[0], args)\n"
        )
        self.stub.chmod(0o700)
        self.child_code = (
            "import os,signal,time;from pathlib import Path;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"Path({str(self.child)!r}).write_text(str(os.getpid()));"
            f"time.sleep(2);Path({str(self.marker)!r}).write_text('late');time.sleep(10)"
        )
        self.command = [
            "/usr/bin/flock",
            "-w",
            "5",
            str(self.lock),
            sys.executable,
            "-c",
            self.child_code,
        ]

    def tearDown(self) -> None:
        # Stop the synthetic entrypoint first so its remaining descendants are
        # adopted here. Never signal a PID that is no longer our child.
        for runner in self.runners:
            if runner.poll() is None:
                runner.kill()
            runner.wait(timeout=2)
        for path in (self.leader, self.child):
            if not path.exists():
                continue
            pid = int(path.read_text())
            try:
                result, _ = os.waitpid(pid, os.WNOHANG)
                if result == 0:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        for runner in self.runners:
            runner.communicate(timeout=2)
        self.libc.prctl(36, self.previous_subreaper, 0, 0, 0)
        self.temporary.cleanup()

    def assert_descendant_finished(self) -> None:
        """Observe real exit, lock release and no delayed action independently."""
        self.assertTrue(self.child.exists(), "Synthetic descendant did not start")
        pid = int(self.child.read_text())
        result, _ = os.waitpid(pid, os.WNOHANG)
        deadline = time.monotonic() + 0.5
        while result == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
            result, _ = os.waitpid(pid, os.WNOHANG)
        self.assertEqual(result, pid, "Host wrapper left its descendant running")
        self.assertFalse(self.marker.exists(), "An action occurred after cancellation")
        self.assertEqual(
            RUN(
                ["/usr/bin/flock", "-n", str(self.lock), "/bin/true"], check=False
            ).returncode,
            0,
        )

    def test_timeout_stops_descendant_and_releases_lock_before_return(self) -> None:
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.runners.append(unrelated)

        def short_deadline(
            *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            kwargs["timeout"] = 0.75
            return RUN(*args, **kwargs)  # type: ignore[call-overload,no-any-return]

        with patch.object(self.policy, "NSENTER", str(self.stub)), patch.object(
            self.policy, "COMMAND_TIMEOUT", 0.75, create=True
        ), patch.object(self.policy, "TERM_GRACE", 0.05, create=True), patch.object(
            self.policy.subprocess, "run", short_deadline
        ):
            with self.assertRaises(self.policy.Failure):
                self.policy.host(*self.command)
        self.assert_descendant_finished()
        self.assertIsNone(
            unrelated.poll(), "Cancellation affected an unrelated process"
        )

    def test_cleanup_deadline_is_fatal_instead_of_retrying(self) -> None:
        process = Mock(pid=987654, returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired("SENSITIVE", 5)
        with patch.object(self.policy.os, "killpg") as kill, patch.object(
            self.policy.time, "sleep"
        ):
            with self.assertRaises(SystemExit) as error:
                self.policy.terminate_operation(process)
        self.assertNotIn("SENSITIVE", str(error.exception))
        self.assertEqual(
            [call.args for call in kill.call_args_list],
            [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)],
        )

    def test_signal_failure_is_fatal_instead_of_retrying(self) -> None:
        process = Mock(pid=987654, returncode=None)
        with patch.object(
            self.policy.os, "killpg", side_effect=PermissionError("SENSITIVE")
        ):
            with self.assertRaises(SystemExit) as error:
                self.policy.terminate_operation(process)
        self.assertNotIn("SENSITIVE", str(error.exception))

    def test_reaped_group_leader_is_not_signalled_again(self) -> None:
        process = Mock(pid=987654, returncode=0)
        with patch.object(self.policy.os, "killpg") as kill:
            with self.assertRaises(SystemExit):
                self.policy.terminate_operation(process)
        kill.assert_not_called()

    def test_real_sigterm_to_entrypoint_stops_owned_operation(self) -> None:
        runner_script = self.root / "runner.py"
        runner_script.write_text(
            "import importlib.util\n"
            f"s=importlib.util.spec_from_file_location('entrypoint', {str(self.program)!r})\n"
            "p=importlib.util.module_from_spec(s);s.loader.exec_module(p)\n"
            f"p.NSENTER={str(self.stub)!r}\np.COMMAND_TIMEOUT=10\np.TERM_GRACE=0.05\n"
            f"def scan():\n p.host(*{self.command!r})\n return True\n"
            "p.reconcile=scan\nraise SystemExit(p.main())\n"
        )
        runner = subprocess.Popen(
            [sys.executable, "-B", str(runner_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.runners.append(runner)
        deadline = time.monotonic() + 3
        while not self.child.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.child.exists(), "Synthetic descendant did not start")
        runner.send_signal(signal.SIGTERM)
        runner.communicate(timeout=3)
        self.assertEqual(runner.returncode, 0)
        self.assert_descendant_finished()


if __name__ == "__main__":
    unittest.main()
