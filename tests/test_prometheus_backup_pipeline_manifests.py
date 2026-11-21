"""Offline Prometheus contracts and native admission for both K8up pipelines.

See tests/README.md for the owned K3s API and verified CRD prerequisites.
Only suspended CronJobs and a few CRs are persisted; all child Jobs are server
dry runs. The tests never use the host's kubectl context or create Secrets.
"""

from contextlib import redirect_stdout
import copy
from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

import yaml

from workload_contracts import render_kustomization

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "09-kube-prometheus-stack/restore-test"
sys.path.insert(0, str(ROOT / "adapters/k8up-pipeline"))
NS = "kube-prometheus-stack"
PIPELINE = "prometheus-backup-pipeline"
RESTORE = "prometheus-restore-test"
SA = f"system:serviceaccount:{NS}:{PIPELINE}"
UID = "12345678-abcd-4321-abcd-123456789012"
RUN_TAG = f"backup-run-{UID}"
NATIVE = "20261121T123456Z-1234567890abcdef"
API_IMAGE = "rancher/k3s:v1.37.1-k3s1@sha256:ca7f37d993d82ef0dcdcfecb2e0e2618ea541dbaffc620c8cedebe01a82acd0d"
# Official chart 4.10.0, also verified against K8up v2.16.0
# (343b99d1519385d75738dbb07d6ff45785c3f186), charts/k8up/crds/.
CRD_HASHES = {
    "archives": "3e1fd121a4af2261c01ef77fbf0252ff6cbcccf77ac6629d11351c5987025d58",
    "backups": "bea0ae865c0da358f865e1e17fa824934179b66591c38eb7959bfd7eee933ecc",
    "checks": "4e66c3e641e80e1298bd324a42cff3acffebbc367645c1ef9abeed85aba8e45c",
    "podconfigs": "8246296be01111e85ae2fc5f0721b05f0bed8a320e3917a6fff01716a55b82d2",
    "prebackuppods": "cc5be08243522eec080e722946675106d110d7f34ba11cde04f64893e93a519c",
    "prunes": "fb943f72ace0cc61d8333b47aa984fa6c4b1ef7d845506099cebbed14b697163",
    "restores": "32a4a2491b71667a4fcfdfe8c041bdc5e38596d3753dbae31caf8f207eab91a2",
    "schedules": "4821bf5c432d40baebec890e3c5c7fae899bb203089c4604492ac008a31d918e",
}
TEST_CONTAINER = os.environ.get("K8UP_PIPELINE_TEST_CONTAINER", "")


def manifests():
    return {
        (d["kind"], d["metadata"]["name"]): d
        for d in render_kustomization(ROOT, PATH)
    }


class PipelineManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = manifests()

    def test_runner_mounts_only_scripts_snapshot_subdirectory_and_api_token(self):
        cron = self.docs["CronJob", PIPELINE]["spec"]
        self.assertTrue(cron["suspend"])
        self.assertEqual(cron["concurrencyPolicy"], "Forbid")
        self.assertEqual(cron["timeZone"], "Europe/Berlin")
        self.assertRegex(cron["schedule"], r"^\d+ \d+ \* \* \*$")
        job = cron["jobTemplate"]["spec"]
        self.assertEqual(job["backoffLimit"], 0)
        self.assertLess(job["activeDeadlineSeconds"], 7 * 3600)
        pod = job["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], PIPELINE)
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertFalse(pod["enableServiceLinks"])
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(pod["securityContext"], {
            "runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 2000,
            "seccompProfile": {"type": "RuntimeDefault"},
        })
        replica = "prometheus-kube-prometheus-stack-prometheus-0"
        self.assertEqual(pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"], [
            {"labelSelector": {"matchLabels": {"statefulset.kubernetes.io/pod-name": replica}},
             "topologyKey": "kubernetes.io/hostname"},
        ])
        container, = pod["containers"]
        self.assertRegex(container["image"], r"^python:3\.13-slim-bookworm@sha256:[a-f0-9]{64}$")
        self.assertEqual(container["command"][-1], "/pipeline/pipeline.py")
        self.assertEqual(container["env"], [
            {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
        ])
        self.assertEqual(container["securityContext"], {
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        })
        self.assertEqual(container["volumeMounts"], [
            {"name": "pipeline", "mountPath": "/pipeline", "readOnly": True},
            {"name": "runner", "mountPath": "/runner", "readOnly": True},
            {"name": "producer", "mountPath": "/producer", "readOnly": True},
            {"name": "snapshots", "mountPath": "/snapshots", "subPath": "prometheus-db/snapshots"},
            {"name": "api-access", "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount", "readOnly": True},
        ])
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(set(volumes), {"pipeline", "runner", "producer", "snapshots", "api-access"})
        shared = self.docs["ConfigMap", volumes["runner"]["configMap"]["name"]]
        self.assertTrue(shared["immutable"])
        self.assertEqual(shared["data"], {"k8up_pipeline.py": (ROOT / "adapters/k8up-pipeline/k8up_pipeline.py").read_text()})
        cm = self.docs["ConfigMap", volumes["pipeline"]["configMap"]["name"]]
        self.assertTrue(cm["immutable"])
        self.assertEqual(cm["data"], {"pipeline.py": (PATH / "pipeline.py").read_text()})
        self.assertEqual(volumes["producer"]["configMap"]["name"], "prometheus-snapshot-contract-v1")
        self.assertEqual(volumes["producer"]["configMap"]["items"], [{"key": "snapshot.py", "path": "snapshot.py"}])
        self.assertEqual(volumes["snapshots"]["persistentVolumeClaim"], {
            "claimName": f"prometheus-kube-prometheus-stack-prometheus-db-{replica}",
        })
        sources = volumes["api-access"]["projected"]["sources"]
        self.assertEqual([list(source) for source in sources], [["serviceAccountToken"], ["configMap"], ["downwardAPI"]])
        self.assertEqual(sources[0], {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}})

    def test_runner_init_prepares_only_the_operator_snapshot_directory(self):
        pod = self.docs["CronJob", PIPELINE]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        init, = pod["initContainers"]
        runner, = pod["containers"]
        self.assertEqual(init["name"], "prepare-snapshots")
        self.assertEqual(init["image"], runner["image"])
        self.assertEqual(init["securityContext"], runner["securityContext"])
        self.assertEqual(init["resources"], runner["resources"])
        self.assertNotIn("env", init)
        self.assertNotIn("envFrom", init)
        self.assertEqual(init["volumeMounts"], [
            {"name": "snapshots", "mountPath": "/source", "subPath": "prometheus-db"},
        ])
        self.assertEqual(init["command"], ["/usr/local/bin/python", "-IB", "-c"])
        script, source_arg = init["args"]
        self.assertEqual(source_arg, "/source")

        restore = self.docs["CronJob", RESTORE]["spec"]
        self.assertIs(restore["suspend"], True)
        restore_pod = restore["jobTemplate"]["spec"]["template"]["spec"]
        self.assertFalse(restore_pod["automountServiceAccountToken"])
        self.assertEqual([c["name"] for c in restore_pod["initContainers"]], ["restore", "validate-archive"])
        self.assertEqual({v["name"] for v in restore_pod["volumes"]}, {"repository", "scratch", "tools"})

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "prometheus-db"
            source.mkdir()
            snapshots = source / "snapshots"
            wal = source / "wal"
            wal.write_text("keep")
            command = [sys.executable, *init["command"][1:], script, str(source)]

            def prepare():
                return subprocess.run(command, capture_output=True, text=True, timeout=5)

            result = prepare()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(snapshots.is_dir())
            self.assertEqual(set(source.iterdir()), {snapshots, wal})
            self.assertEqual(set(root.iterdir()), {source})
            original = snapshots.stat()
            result = prepare()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.samestat(original, snapshots.stat()))

            snapshots.rmdir()
            other = root / "other"
            other.mkdir()
            snapshots.symlink_to(other, target_is_directory=True)
            self.assertNotEqual(prepare().returncode, 0)
            self.assertTrue(snapshots.is_symlink())
            self.assertEqual(list(other.iterdir()), [])
            snapshots.unlink()
            snapshots.write_text("not a directory")
            self.assertNotEqual(prepare().returncode, 0)
            self.assertEqual(snapshots.read_text(), "not a directory")
            self.assertEqual(wal.read_text(), "keep")

    def test_rbac_and_egress_are_scoped(self):
        self.assertEqual(self.docs["Role", PIPELINE]["rules"], [
            {"apiGroups": ["batch"], "resources": ["cronjobs"], "resourceNames": [RESTORE], "verbs": ["get"]},
            {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["prometheus-tsdb-snapshot"], "verbs": ["get"]},
            {"apiGroups": ["k8up.io"], "resources": ["backups", "checks"], "verbs": ["get", "create"]},
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "create"]},
        ])
        binding = self.docs["RoleBinding", PIPELINE]
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": PIPELINE, "namespace": NS}])
        self.assertEqual(binding["roleRef"]["name"], PIPELINE)
        self.assertEqual(self.docs["NetworkPolicy", f"isolate-{PIPELINE}"]["spec"], {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": PIPELINE}},
            "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": [],
        })

    def test_admission_fails_closed_and_requires_exact_cronjob_parameter(self):
        for suffix, resources in (("k8up", ["backups", "checks", "prunes"]), ("verify", ["jobs"])):
            name = f"{PIPELINE}-{suffix}"
            policy = self.docs["ValidatingAdmissionPolicy", name]["spec"]
            self.assertEqual(policy["failurePolicy"], "Fail")
            scope = policy["matchConstraints"]
            self.assertEqual(scope["namespaceSelector"], {"matchLabels": {"kubernetes.io/metadata.name": NS}})
            rule, = scope["resourceRules"]
            self.assertEqual(rule["resources"], resources)
            self.assertEqual(rule["operations"], ["CREATE", "UPDATE"] if suffix == "k8up" else ["CREATE"])
            self.assertEqual(rule["scope"], "Namespaced")
            binding = self.docs["ValidatingAdmissionPolicyBinding", name]["spec"]
            self.assertEqual(binding["validationActions"], ["Deny"])
            self.assertEqual(binding["policyName"], name)
            if suffix == "verify":
                self.assertEqual(policy["paramKind"], {"apiVersion": "batch/v1", "kind": "CronJob"})
                self.assertEqual(binding["paramRef"], {
                    "name": RESTORE, "namespace": NS, "parameterNotFoundAction": "Deny",
                })


class OwnedK8upApi:
    @staticmethod
    def kubectl(*args, resource=None, text=None):
        return subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                TEST_CONTAINER,
                "/bin/kubectl",
                "--kubeconfig=/etc/rancher/k3s/k3s.yaml",
                *args,
            ],
            input=json.dumps(resource) if resource is not None else text,
            capture_output=True,
            text=True,
            timeout=30,
        )

    @classmethod
    def require(cls, *args, **kwargs):
        result = cls.kubectl(*args, **kwargs)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout

    @classmethod
    def create(cls, resource, user=SA, pod_uid=UID):
        args = ["create", "-f", "-", "-o", "json", "--dry-run=server"]
        if user:
            args.append(f"--as={user}")
            if pod_uid:
                args.append(
                    f"--as-user-extra=authentication.kubernetes.io/pod-uid={pod_uid}"
                )
        return cls.kubectl(*args, resource=resource)

    @classmethod
    def wait_for_denial(cls, resource, policy, user=SA):
        deadline = time.monotonic() + 18
        while True:
            result = cls.create(resource, user)
            if (
                result.returncode
                and f"ValidatingAdmissionPolicy '{policy}'" in result.stderr
            ):
                if "expression resulted in error" in result.stderr.lower():
                    raise AssertionError(result.stderr)
                return result.stderr
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"Policy {policy} did not activate: {result.stderr}"
                )
            time.sleep(0.25)

    @classmethod
    def install_crds(cls):
        # Never write to a Docker host context via kubectl or a mounted kubeconfig.
        inspected = subprocess.run(
            ["docker", "inspect", "--type=container", TEST_CONTAINER],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        (container,) = json.loads(inspected.stdout)
        config, host = container["Config"], container["HostConfig"]
        if not (
            TEST_CONTAINER == container["Id"]
            and container["State"]["Running"]
            and config["Image"] == API_IMAGE
            and config["Labels"].get("purpose") == "k8up-pipeline-test"
            and config["Labels"].get("owner")
            and "--disable-agent" in " ".join(container["Args"]).split()
            and host["NetworkMode"] == "none"
            and not host["Privileged"]
            and host["CapDrop"] == ["ALL"]
            and host["SecurityOpt"] == ["no-new-privileges"]
            and not host.get("PortBindings")
            and host["PidMode"] == ""
            and not host.get("CapAdd")
            and not host.get("Devices")
            and not host.get("DeviceRequests")
            and all(m["Type"] == "volume" for m in container["Mounts"])
            and not host.get("Binds")
            and not host.get("Mounts")
        ):
            raise AssertionError(
                "Native tests require the exact disposable, agentless and isolated K3s container"
            )
        crds = []
        crd_dir = Path(os.environ["K8UP_PIPELINE_CRDS"])
        for name, digest in CRD_HASHES.items():
            data = (crd_dir / f"k8up.io_{name}.yaml").read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise AssertionError(f"K8up CRD hash mismatch for {name}")
            crds.append(data.decode())
        cls.require("apply", "--server-side", "-f", "-", text="\n---\n".join(crds))
        cls.require(
            "wait",
            "--for=condition=Established",
            "--timeout=15s",
            *(f"crd/{name}.k8up.io" for name in CRD_HASHES),
        )

    def assert_admitted(self, obj, user=SA):
        result = self.create(obj, user)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_policy_denies(self, obj, policy, user=SA, pod_uid=UID):
        result = self.create(obj, user, pod_uid)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"ValidatingAdmissionPolicy '{policy}'", result.stderr)
        self.assertNotIn("expression resulted in error", result.stderr.lower())


@unittest.skipUnless(TEST_CONTAINER, "Requires a pinned, owned, isolated K3s API")
class NativePipelineAdmissionTest(OwnedK8upApi, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install_crds()
        cls.require("create", "namespace", NS)
        cls.require("create", "namespace", "prometheus-pipeline-unrelated")
        cls.docs = manifests()
        cls.runner = runpy.run_path(str(PATH / "pipeline.py"))
        restore = cls.docs["CronJob", RESTORE]
        policy_documents = [d for (kind, name), d in cls.docs.items() if name.startswith(PIPELINE) and kind in {
            "ServiceAccount", "Role", "RoleBinding", "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding",
        }]
        cls.require("apply", "-f", "-", text=yaml.safe_dump_all(policy_documents))
        cls.wait_for_denial(cls.backup(), f"{PIPELINE}-k8up", user=None)
        missing = cls.wait_for_denial(cls.verifier(restore), f"{PIPELINE}-verify")
        if "no params" not in missing.lower():
            raise AssertionError(f"Expected fail-closed missing parameter, got: {missing}")
        cls.require("create", "-f", "-", resource=restore)
        deadline = time.monotonic() + 18
        while True:
            cron = json.loads(cls.require("get", "cronjob", RESTORE, "-n", NS, "-o", "json", f"--as={SA}"))
            cls.good_verifier = cls.verifier(cron)
            result = cls.create(cls.good_verifier)
            if not result.returncode:
                break
            if time.monotonic() >= deadline:
                raise AssertionError(result.stderr)
            time.sleep(0.25)

    @classmethod
    def backup(cls):
        return cls.runner["k8up_resource"]("Backup", f"pt-{UID}",
            labelSelectors=[{"matchLabels": {"platform.k3s-on-omv.io/backup-set": "prometheus-tsdb"}}],
            tags=["prometheus-tsdb", "prometheus-replica-0", RUN_TAG],
        )

    @classmethod
    def check(cls, pod_uid=UID):
        return cls.runner["k8up_resource"]("Check", f"prometheus-check-{pod_uid}")

    @classmethod
    def verifier(cls, cron):
        class Api:
            def request(self, _):
                return cron
        return cls.runner["verifier"](Api(), f"prometheus-verify-{UID}", RUN_TAG, NATIVE)

    def assert_policy_denies(self, obj, suffix, user=SA, pod_uid=UID):
        super().assert_policy_denies(obj, f"{PIPELINE}-{suffix}", user, pod_uid)

    def test_k8up_positive_and_namespace_scope(self):
        for obj in (self.backup(), self.check()):
            self.assert_admitted(obj)
            self.assert_policy_denies(obj, "k8up", user=None)
            self.assert_policy_denies(obj, "k8up", pod_uid=None)
            self.assert_policy_denies(obj, "k8up", pod_uid="87654321-abcd-4321-abcd-123456789012")
            obj["metadata"]["namespace"] = "prometheus-pipeline-unrelated"
            self.assert_admitted(obj, user=None)
        prune = self.runner["k8up_resource"]("Prune", "not-authorized", retention={"keepDaily": 7})
        self.assert_policy_denies(prune, "k8up", user=None)
        prune["metadata"]["namespace"] = "prometheus-pipeline-unrelated"
        self.assert_admitted(prune, user=None)

    def test_k8up_operation_spec_cannot_change_but_metadata_can(self):
        # This Check persists, so give it a different run UID from the dry-run fixtures.
        pod_uid = "12345678-abcd-4321-abcd-123456789013"
        saved = json.loads(self.require(
            "create", "-f", "-", "-o", "json", f"--as={SA}",
            f"--as-user-extra=authentication.kubernetes.io/pod-uid={pod_uid}", resource=self.check(pod_uid),
        ))
        saved["metadata"]["annotations"] = {"example.invalid/fixture": "set"}
        self.require("replace", "-f", "-", "--dry-run=server", resource=saved)
        saved["spec"]["backend"]["local"]["mountPath"] = "/unreviewed"
        denied = self.kubectl("replace", "-f", "-", "--dry-run=server", resource=saved)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("immutable after creation", denied.stderr)

    def test_native_job_name_fits_k8up_prebackup_children(self):
        job = copy.deepcopy(self.good_verifier)
        job["metadata"]["name"] = "backup-" + self.backup()["metadata"]["name"] + "-prebackup"
        actual = self.assert_admitted(job, user=None)
        self.assertEqual(actual["spec"]["template"]["metadata"]["labels"]["job-name"], job["metadata"]["name"])

    def test_k8up_rejects_backend_selector_overrides_and_spoofed_run(self):
        for field, value in (
            ("labelSelectors", [{"matchLabels": {"app": "other"}}]),
            ("tags", ["prometheus-tsdb", "prometheus-replica-0", "backup-run-another"]),
            ("podConfigRef", {"name": "other"}),
            ("successfulJobsHistoryLimit", 0),
            ("keepJobs", 1),
        ):
            with self.subTest(field=field):
                backup = self.backup()
                backup["spec"][field] = value
                self.assert_policy_denies(backup, "k8up")
        for field, value in (
            ("repoPasswordSecretRef", {"name": "other", "key": "password"}),
            ("local", {"mountPath": "/another"}),
            ("volumeMounts", [{"name": "repository", "mountPath": "/repository", "subPath": "another"}]),
            ("s3", {"endpoint": "https://example.invalid", "bucket": "other"}),
        ):
            with self.subTest(backend_field=field):
                check = self.check()
                check["spec"]["backend"][field] = value
                self.assert_policy_denies(check, "k8up")

    def test_actual_job_defaulting_preserves_the_trusted_clone(self):
        self.assertTrue(self.assert_admitted(self.docs["CronJob", PIPELINE], user=None)["spec"]["suspend"])
        actual = self.assert_admitted(self.good_verifier)
        self.assertEqual(actual["spec"]["parallelism"], 1)
        self.assertEqual(actual["spec"]["completions"], 1)
        self.assertIn("batch.kubernetes.io/controller-uid", actual["spec"]["selector"]["matchLabels"])
        self.assertEqual(actual["spec"]["template"]["spec"], self.good_verifier["spec"]["template"]["spec"])
        self.assert_policy_denies(self.good_verifier, "verify", pod_uid=None)
        self.assert_policy_denies(self.good_verifier, "verify", pod_uid="87654321-abcd-4321-abcd-123456789012")
        outsider = copy.deepcopy(self.good_verifier)
        outsider["metadata"]["name"] = "unrelated-job"
        self.assert_admitted(outsider, user=None)

    def test_rejects_workload_metadata_and_retention_overrides(self):
        cases = (
            ("metadata", {"name": "wrong-job"}),
            ("metadata", {"finalizers": ["example.invalid/hold"]}),
            ("metadata", {"labels": {"another": "job"}}),
            ("metadata", {"annotations": {"example.invalid/inject": "true"}}),
            ("spec", {"ttlSecondsAfterFinished": 360000}),
            ("spec", {"parallelism": 5}),
            ("spec", {"managedBy": "example.invalid/other"}),
            ("spec.template.spec", {"hostNetwork": True}),
            ("spec.template.spec", {"serviceAccountName": PIPELINE}),
            ("spec.template.metadata.labels", {"istio.io/dataplane-mode": "ambient"}),
            ("spec.template.metadata.annotations", {"sidecar.istio.io/inject": "true"}),
            ("spec.template.metadata.annotations", {"platform.k3s-on-omv.io/backup-run-tag": "wrong-run"}),
            ("spec.template.metadata.annotations", {"platform.k3s-on-omv.io/native-snapshot": "wrong-snapshot"}),
        )
        for path, change in cases:
            with self.subTest(path=path, change=change):
                verifier = copy.deepcopy(self.good_verifier)
                node = verifier
                for key in path.split("."):
                    node = node[key]
                node.update(change)
                self.assert_policy_denies(verifier, "verify")


@unittest.skipUnless(TEST_CONTAINER, "Requires a pinned, owned, isolated K3s API")
class NativeFixtureAdmissionTest(OwnedK8upApi, unittest.TestCase):
    namespace = "backup-validation"
    unrelated = "fixture-fence-unrelated"
    account = f"system:serviceaccount:{namespace}:k8up-fixture-pipeline"
    unarmed_policy = "k8up-fixture-unarmed-runner"
    session_policy = "k8up-fixture-repository-session"
    prune_policy = "k8up-fixture-prune-job"

    @classmethod
    def setUpClass(cls):
        cls.install_crds()
        cls.require("create", "namespace", cls.namespace)
        cls.require("create", "namespace", cls.unrelated)
        fixture_path = ROOT / "08-backup-validation"
        docs = render_kustomization(ROOT, fixture_path / "resources")
        cls.docs = {(d["kind"], d["metadata"]["name"]): d for d in docs}
        admission = [
            doc
            for (kind, name), doc in cls.docs.items()
            if name.startswith("k8up-fixture-")
            and kind
            in {
                "Role",
                "RoleBinding",
                "ServiceAccount",
                "ValidatingAdmissionPolicy",
                "ValidatingAdmissionPolicyBinding",
            }
        ]
        cls.require("apply", "-f", "-", text=yaml.safe_dump_all(admission))
        cls.fixture = runpy.run_path(
            str(fixture_path / "restore-test/fixture_pipeline.py")
        )

    def fixture_requests(self):
        # Exercise the actual runner, including the exact defaulted template clones.
        from test_fixture_backup_pipeline import FixtureApi

        runner = FixtureApi()
        runner.reset(self.fixture, self.docs)
        for name in (self.fixture["SEED"], self.fixture["VERIFIER"]):
            runner.reads[f"{self.fixture['BATCH']}/cronjobs/{name}"] = json.loads(
                self.require(
                    "create",
                    "-f",
                    "-",
                    "-o",
                    "json",
                    resource=self.docs["CronJob", name],
                ),
            )
        # Suppress the mock completion message; only API assertions are evidence here.
        with redirect_stdout(StringIO()):
            runner.run_pipeline()
        return [resource for _, resource in runner.posts]

    def test_cutover_admits_legacy_until_armed_then_only_exact_runner_requests(self):
        seed, backup, check, verifier = self.fixture_requests()
        for request in (seed, backup, check, verifier):
            self.wait_for_denial(request, self.unarmed_policy, self.account)

        # Representative operations from the legacy Schedule, issued by a
        # non-runner. They must pass before any opt-in (label absent).
        def operation(kind, name, **spec):
            return self.fixture["k8up_resource"](
                self.namespace,
                "k8up-repository",
                "legacy-podconfig",
                kind,
                name,
                **spec,
            )

        legacy_backup = operation("Backup", "legacy-backup")
        schedule = {
            "apiVersion": "k8up.io/v1",
            "kind": "Schedule",
            "metadata": {"name": "validation", "namespace": self.namespace},
            "spec": {
                "backend": legacy_backup["spec"]["backend"],
                "backup": {"schedule": "0 8 * * *"},
                "check": {"schedule": "0 10 * * 1"},
                "prune": {"schedule": "0 11 * * 1", "retention": {"keepDaily": 7}},
            },
        }
        legacy = (
            schedule,
            legacy_backup,
            operation("Check", "legacy-check"),
            operation("Prune", "legacy-prune", retention={"keepDaily": 7}),
        )
        for request in legacy:
            with self.subTest(unarmed=request["kind"]):
                self.assert_admitted(request, user=None)

        # Persist a synthetic Prune CR before opting in, then exercise the
        # native metadata a delayed reconcile would put on its batch Job.
        prune = json.loads(
            self.require("create", "-f", "-", "-o", "json", resource=legacy[-1])
        )
        edited_prune = copy.deepcopy(prune)
        edited_prune["spec"]["retention"]["keepDaily"] = 14
        self.require("replace", "-f", "-", "--dry-run=server", resource=edited_prune)
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": "prune-legacy-prune",
                "namespace": self.namespace,
                "labels": {"k8upjob": "true", "k8up.io/type": "prune"},
                "ownerReferences": [
                    {
                        "apiVersion": prune["apiVersion"],
                        "kind": prune["kind"],
                        "name": prune["metadata"]["name"],
                        "uid": prune["metadata"]["uid"],
                        "controller": True,
                    }
                ],
            },
            "spec": {
                "template": {
                    "metadata": {"labels": {"k8upjob": "true"}},
                    "spec": {
                        "restartPolicy": "OnFailure",
                        "containers": [
                            {
                                "name": "prune",
                                "image": "registry.k8s.io/pause:3.10",
                            }
                        ],
                    },
                }
            },
        }
        self.assert_admitted(job, user=None)
        activation = f"{self.fixture['ACTIVATION']}=enabled"
        self.require("label", "namespace", self.namespace, activation)
        self.require("label", "namespace", self.unrelated, activation)
        self.wait_for_denial(schedule, self.session_policy, user=None)
        self.wait_for_denial(job, self.prune_policy, user=None)
        denied = self.kubectl(
            "replace", "-f", "-", "--dry-run=server", resource=edited_prune
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("specs are immutable", denied.stderr)
        prune["metadata"]["annotations"] = {"example.invalid/probe": "metadata-only"}
        self.require("replace", "-f", "-", "--dry-run=server", resource=prune)
        # Use a different name for the legacy Prune CREATE probe so it's not
        # rejected merely as AlreadyExists by the API's storage layer.
        legacy[-1]["metadata"]["name"] = "another-legacy-prune"
        for request in legacy:
            with self.subTest(armed=request["kind"]):
                self.assert_policy_denies(request, self.session_policy, user=None)

        outsider = copy.deepcopy(job)
        outsider["metadata"]["namespace"] = self.unrelated
        del outsider["metadata"]["ownerReferences"]
        self.assert_admitted(outsider, user=None)
        for label in ("backup", "check", None):
            ordinary = copy.deepcopy(job)
            del ordinary["metadata"]["ownerReferences"]
            if label is None:
                del ordinary["metadata"]["labels"]
            else:
                ordinary["metadata"]["labels"]["k8up.io/type"] = label
            self.assert_admitted(ordinary, user=None)

        for request in (backup, check):
            self.assert_admitted(request, user=self.account)
            self.assert_policy_denies(request, self.session_policy, user=None)
            self.assert_policy_denies(
                request, self.session_policy, user=self.account, pod_uid=None
            )
            changed = copy.deepcopy(request)
            changed["spec"]["podConfigRef"]["name"] = "another-podconfig"
            self.assert_policy_denies(changed, self.session_policy, user=self.account)
        for request, stage in ((seed, "seed"), (verifier, "verify")):
            policy = f"k8up-fixture-{stage}-clone"
            self.assert_admitted(request, user=self.account)
            self.assert_policy_denies(request, policy, user=self.account, pod_uid=None)
            changed = copy.deepcopy(request)
            changed["spec"]["template"]["spec"]["enableServiceLinks"] = True
            self.assert_policy_denies(changed, policy, user=self.account)
        verifier["spec"]["template"]["metadata"]["annotations"][
            self.fixture["RUN_TAG"]
        ] = "wrong-tag"
        self.assert_policy_denies(
            verifier, "k8up-fixture-verify-clone", user=self.account
        )


if __name__ == "__main__":
    unittest.main()
