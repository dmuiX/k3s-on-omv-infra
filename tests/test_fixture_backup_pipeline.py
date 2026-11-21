"""Exercise the rendered fixture workflow, including no-action cutover gates."""

from copy import deepcopy
from pathlib import Path
import runpy
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

import yaml

from workload_contracts import render_kustomization, selector_matches

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "08-backup-validation/restore-test"
sys.path.insert(0, str(ROOT / "adapters/k8up-pipeline"))
from k8up_pipeline import PipelineError  # noqa: E402

UID = "12345678-abcd-4321-abcd-123456789012"
RUNNER = "k8up-fixture-pipeline"
COMPLETE = {"type": "Complete", "status": "True"}
SUCCEEDED = {"type": "Completed", "status": "True", "reason": "Succeeded"}
RUN_LABEL = "platform.k3s-on-omv.io/fixture-runner"


class FixtureApi:
    def reset(self, code, docs):
        self.code = code
        self.core, self.k8up, self.batch = (
            code[key] for key in ("CORE", "K8UP", "BATCH")
        )
        self.posts = []
        self.live = {}
        self.replace = set()
        self.completion = {
            "fixture-seed": COMPLETE,
            "fx": SUCCEEDED,
            "fixture-check": SUCCEEDED,
            "fixture-verify": COMPLETE,
        }
        self.lists = {
            (
                f"{self.core}/persistentvolumeclaims",
                f"{code['SELECTOR']}={code['SOURCE']}",
            ): [self.observed(docs["PersistentVolumeClaim", code["SOURCE"]])],
        }
        self.reads = {
            self.core: {
                "metadata": {
                    "name": code["NAMESPACE"],
                    "labels": {code["ACTIVATION"]: "enabled"},
                }
            },
            f"{self.k8up}/podconfigs/{code['POD_CONFIG']}": self.observed(
                docs["PodConfig", code["POD_CONFIG"]]
            ),
        }
        for template in (code["SEED"], code["VERIFIER"]):
            self.reads[f"{self.batch}/cronjobs/{template}"] = deepcopy(
                docs["CronJob", template]
            )

    @staticmethod
    def observed(body):
        body = deepcopy(body)
        body["metadata"]["uid"] = UID
        return body

    def request(self, path, body=None):
        if body is not None:
            self.posts.append((path, deepcopy(body)))
            observed = self.observed(body)
            name = body["metadata"]["name"]
            stage = name.removesuffix(f"-{UID}")
            running = deepcopy(observed)
            if stage in self.replace:
                running["metadata"]["uid"] = UID.replace("9012", "9013")
            running["status"] = {"conditions": [self.completion[stage]]}
            self.live[f"{path}/{name}"] = running
            return observed
        if "?" in path:
            parsed = urlsplit(path)
            label = parse_qs(parsed.query).get("labelSelector", [""])[0]
            return {"metadata": {}, "items": self.lists.get((parsed.path, label), [])}
        return self.live[path] if path in self.live else self.reads[path]

    def run_pipeline(self):
        self.code["run"](self, UID)


class FixturePipeline(FixtureApi, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docs = render_kustomization(ROOT, ROOT / "08-backup-validation/resources")
        cls.docs = {(obj["kind"], obj["metadata"]["name"]): obj for obj in docs}
        cls.code = runpy.run_path(str(CONFIG / "fixture_pipeline.py"))

    def setUp(self):
        self.reset(self.code, self.docs)

    def test_automatic_seed_backup_check_and_verifier_are_uid_bound(self):
        self.run_pipeline()
        self.assertEqual(
            [path for path, _ in self.posts],
            [
                f"{self.batch}/jobs",
                f"{self.k8up}/backups",
                f"{self.k8up}/checks",
                f"{self.batch}/jobs",
            ],
        )
        seed, backup, check, verifier = (body for _, body in self.posts)
        self.assertEqual(
            seed["spec"],
            self.docs["CronJob", self.code["SEED"]]["spec"]["jobTemplate"]["spec"],
        )
        spec = backup["spec"]
        self.assertEqual(spec["tags"], [self.code["SOURCE"], f"backup-run-{UID}"])
        self.assertEqual(
            spec["labelSelectors"],
            [{"matchLabels": {self.code["SELECTOR"]: self.code["SOURCE"]}}],
        )
        fields = {k: v for k, v in spec.items() if k not in {"tags", "labelSelectors"}}
        self.assertEqual(check["spec"], fields)
        pinned = deepcopy(
            self.docs["CronJob", self.code["VERIFIER"]]["spec"]["jobTemplate"]["spec"]
        )
        pinned["template"]["metadata"]["annotations"] = {
            self.code["RUN_TAG"]: f"backup-run-{UID}"
        }
        self.assertEqual(verifier["spec"], pinned)
        self.assertEqual(
            [body["metadata"]["name"] for _, body in self.posts],
            [f"{stage}-{UID}" for stage in self.completion],
        )

    def test_unarmed_schedule_or_leftover_worker_blocks_even_the_seed(self):
        namespace = self.reads[self.core]["metadata"]
        labels = namespace.pop("labels")
        with self.assertRaisesRegex(PipelineError, "not armed"):
            self.run_pipeline()
        namespace["labels"] = labels
        for path, label, message in (
            (f"{self.k8up}/schedules", "", "Schedule still exists"),
            (f"{self.k8up}/prunes", "", "unfinished K8up"),
            (f"{self.k8up}/backups", "", "unfinished K8up"),
            (f"{self.batch}/jobs", "k8upjob=true", "unfinished fixture or K8up Job"),
            (
                f"{self.batch}/jobs",
                "platform.k3s-on-omv.io/fixture-child=true",
                "unfinished fixture or K8up Job",
            ),
        ):
            with self.subTest(path=path, label=label):
                self.lists[path, label] = [
                    {
                        "spec": {"suspend": True},
                        "metadata": {"deletionTimestamp": "2026-11-21T12:00:00Z"},
                    }
                ]
                with self.assertRaisesRegex(PipelineError, message):
                    self.run_pipeline()
                del self.lists[path, label]
        self.assertEqual(self.posts, [])

    def test_wrong_source_extra_targets_and_pod_config_block_seed(self):
        (key,) = self.lists
        (source,) = self.lists[key]
        for change in ("wrong-name", "another-claim", "stream-pod", "pod-config"):
            with self.subTest(change=change):
                self.setUp()
                if change == "wrong-name":
                    self.lists[key][0]["metadata"]["name"] = "unrelated"
                elif change == "another-claim":
                    self.lists[key].append(source)
                elif change == "stream-pod":
                    self.lists[f"{self.core}/pods", "k8up.io/ownerBackupUID"] = [source]
                else:
                    self.reads[f"{self.k8up}/podconfigs/{self.code['POD_CONFIG']}"][
                        "spec"
                    ]["template"]["spec"]["volumes"] = []
                with self.assertRaises(PipelineError):
                    self.run_pipeline()
                self.assertEqual(self.posts, [])

    def test_failure_or_replacement_at_each_stage_stops_continuation(self):
        for index, (stage, status) in enumerate(self.completion.items(), start=1):
            for failure in ("terminal", "replacement"):
                with self.subTest(stage=stage, failure=failure):
                    self.posts.clear()
                    self.replace.clear()
                    if failure == "replacement":
                        self.replace.add(stage)
                    elif status == COMPLETE:
                        self.completion[stage] = {"type": "Failed", "status": "True"}
                    else:
                        self.completion[stage] = dict(SUCCEEDED, reason="Failed")
                    with self.assertRaises(PipelineError):
                        self.run_pipeline()
                    self.assertEqual(len(self.posts), index)
                    self.completion[stage] = status

    def test_only_runner_has_api_egress_and_child_templates_are_suspended(self):
        cron = self.docs["CronJob", "k8up-backup-validation"]["spec"]
        self.assertTrue(cron["suspend"])
        self.assertEqual(cron["schedule"], "15 9 * * *")
        self.assertEqual(cron["concurrencyPolicy"], "Forbid")
        self.assertFalse(any(kind == "Schedule" for kind, _ in self.docs))
        app = yaml.safe_load((ROOT / "08-backup-validation/app.yml").read_text())
        self.assertNotIn(
            self.code["ACTIVATION"],
            app["spec"]["syncPolicy"]["managedNamespaceMetadata"]["labels"],
        )
        runner = cron["jobTemplate"]["spec"]["template"]
        self.assertEqual(runner["spec"]["serviceAccountName"], RUNNER)
        volumes = {v["name"]: v for v in runner["spec"]["volumes"]}
        self.assertEqual(set(volumes), {"api-access", "pipeline", "runner"})
        for script in ("pipeline", "runner"):
            self.assertTrue(
                self.docs["ConfigMap", volumes[script]["configMap"]["name"]][
                    "immutable"
                ]
            )
        for name in (
            "k8up-backup-validation",
            self.code["VERIFIER"],
            self.code["SEED"],
        ):
            with self.subTest(name=name):
                child = self.docs["CronJob", name]["spec"]
                self.assertTrue(child["suspend"])
                pod = child["jobTemplate"]["spec"]["template"]
                self.assertIs(pod["spec"]["automountServiceAccountToken"], False)
                allowed = [
                    n
                    for (k, n), doc in self.docs.items()
                    if k == "NetworkPolicy"
                    and selector_matches(
                        doc["spec"]["podSelector"], pod["metadata"]["labels"]
                    )
                    and doc["spec"].get("egress")
                ]
                self.assertEqual(
                    allowed,
                    (
                        ["allow-k8up-fixture-runner-api"]
                        if name == "k8up-backup-validation"
                        else []
                    ),
                )
        self.assertEqual(runner["metadata"]["labels"][RUN_LABEL], "true")

    def test_only_runner_denial_is_active_before_opt_in(self):
        armed = {
            "k8up-fixture-repository-session",
            "k8up-fixture-prune-job",
            "k8up-fixture-source",
            "k8up-fixture-seed-clone",
            "k8up-fixture-verify-clone",
        }
        unarmed = "k8up-fixture-unarmed-runner"
        policies = {
            name: doc["spec"]
            for (kind, name), doc in self.docs.items()
            if kind == "ValidatingAdmissionPolicy" and name.startswith("k8up-fixture-")
        }
        self.assertEqual(set(policies), armed | {unarmed})
        for name, policy in policies.items():
            with self.subTest(policy=name):
                self.assertEqual(policy["failurePolicy"], "Fail")
                scope = policy["matchConstraints"]["namespaceSelector"]
                labels = {"kubernetes.io/metadata.name": self.code["NAMESPACE"]}
                if name in armed:
                    labels[self.code["ACTIVATION"]] = "enabled"
                    self.assertEqual(scope, {"matchLabels": labels})
                else:
                    self.assertEqual(
                        scope,
                        {
                            "matchLabels": labels,
                            "matchExpressions": [
                                {
                                    "key": self.code["ACTIVATION"],
                                    "operator": "NotIn",
                                    "values": ["enabled"],
                                }
                            ],
                        },
                    )
                    (expression,) = policy["matchConditions"]
                    self.assertIn(f":{RUNNER}'", expression["expression"])
                self.assertTrue(
                    all(
                        "namespaceObject" not in condition["expression"]
                        for condition in policy.get("matchConditions", [])
                    )
                )
                binding = self.docs["ValidatingAdmissionPolicyBinding", name]["spec"]
                self.assertEqual(binding["policyName"], name)
                self.assertEqual(binding["validationActions"], ["Deny"])

    def test_job_grant_has_fail_closed_exact_template_guards(self):
        for stage, template in (
            ("seed", self.code["SEED"]),
            ("verify", self.code["VERIFIER"]),
        ):
            name = f"k8up-fixture-{stage}-clone"
            policy = self.docs["ValidatingAdmissionPolicy", name]
            binding = self.docs["ValidatingAdmissionPolicyBinding", name]
            self.assertNotIn("namespace", policy["metadata"])
            self.assertNotIn("namespace", binding["metadata"])
            self.assertEqual(policy["spec"]["failurePolicy"], "Fail")
            self.assertIn(
                "variables.job.template.spec == variables.source.template.spec",
                [v["expression"] for v in policy["spec"]["validations"]],
            )
            self.assertEqual(binding["spec"]["validationActions"], ["Deny"])
            self.assertEqual(
                binding["spec"]["paramRef"],
                {
                    "name": template,
                    "namespace": "backup-validation",
                    "parameterNotFoundAction": "Deny",
                },
            )
