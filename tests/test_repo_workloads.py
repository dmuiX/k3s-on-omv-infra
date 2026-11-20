"""Workload contracts grounded in the deployed controllers and owning CRs."""

import unittest

import yaml


class RepositoryWorkloads(unittest.TestCase):
    def test_producer_jobs_use_bounded_native_retries_and_diagnostic_retention(
        self,
    ) -> None:
        import copy
        import hashlib
        import json

        from workload_contracts import Repository

        # Original complete specs: normalize only the reviewed lifecycle deltas.
        # This also guards deadlines, identities, commands and Secret references.
        cases = (
            (
                "openbao-backups",
                "openbao-snapshot",
                "CronJob",
                "16fbb2d2d3bfa560ad61fa474ffa13185ab74110bef2eb68493fde485ac6b142",
            ),
            (
                "k3s-postgres",
                "k3s-postgres-manager-provisioner",
                "Job",
                "1766471924335d1c3fe85f0794320d3df5ea6d31707e7007b0801abc24a4dbd5",
            ),
            (
                "longhorn-config",
                "longhorn-proxy-provisioner",
                "Job",
                "5f81c185c941619867900e214e96812eb0e978c7452bd66df99004f33448a0b3",
            ),
            (
                "kube-prometheus-stack",
                "grafana-admin-provisioner",
                "Job",
                "74cd024b8b115a9b9cb97b28be6d2575402444b5e179b3c07f5a14d0514164e8",
            ),
        )
        repo = Repository()
        for app, name, kind, baseline in cases:
            with self.subTest(application=app):
                (job,) = [
                    d
                    for d in repo.render(app)
                    if d["kind"] == kind and d["metadata"]["name"] == name
                ]
                spec = copy.deepcopy(job["spec"])
                if kind == "CronJob":
                    self.assertEqual(spec.pop("successfulJobsHistoryLimit", None), 1)
                    self.assertEqual(spec.pop("failedJobsHistoryLimit", None), 1)
                    native = spec["jobTemplate"]["spec"]
                    self.assertEqual(native.pop("backoffLimit", None), 6)
                    self.assertEqual(native.pop("ttlSecondsAfterFinished", None), 86400)
                else:
                    annotations = job["metadata"]["annotations"]
                    self.assertEqual(annotations["argocd.argoproj.io/hook"], "Sync")
                    self.assertEqual(
                        annotations["argocd.argoproj.io/hook-delete-policy"],
                        "BeforeHookCreation,HookSucceeded",
                    )
                    native = spec
                    self.assertNotIn("ttlSecondsAfterFinished", native)
                    self.assertEqual(
                        native["backoffLimit"],
                        10 if name == "grafana-admin-provisioner" else 2,
                    )
                    native["backoffLimit"] = 2
                pod = native["template"]["spec"]
                self.assertEqual(pod["restartPolicy"], "OnFailure")
                pod["restartPolicy"] = "Never"
                self.assertEqual(
                    hashlib.sha256(
                        json.dumps(spec, sort_keys=True).encode()
                    ).hexdigest(),
                    baseline,
                )

    def test_credential_producer_scripts_preserve_create_once_custody_on_rerun(
        self,
    ) -> None:
        import contextlib
        import io
        import types
        from typing import TypedDict
        from unittest.mock import patch

        from workload_contracts import Repository

        class CustodyWrite(TypedDict):
            options: dict[str, int]
            data: dict[str, str]

        repo = Repository()
        for app in ("k3s-postgres", "longhorn-config", "kube-prometheus-stack"):
            with self.subTest(application=app):
                docs = repo.render(app)
                helper = types.ModuleType("credential_helpers")
                source = next(
                    d["data"]["credential_helpers.py"]
                    for d in docs
                    if d["kind"] == "ConfigMap"
                    and d["metadata"]["name"] == "credential-custody"
                )
                exec(compile(source, "credential_helpers.py", "exec"), helper.__dict__)
                stored: dict[str, dict[str, str]] = {}
                writes: list[str] = []

                def request(
                    _base: str, path: str, _token: str, data: CustodyWrite | None = None
                ) -> dict[str, object]:
                    if path == "identity/oidc/client/longhorn":
                        return {
                            "data": {
                                "client_id": "fixture-client",
                                "client_secret": "fixture-secret",
                            }
                        }
                    if data is not None:
                        self.assertEqual(data["options"], {"cas": 0})
                        self.assertNotIn(path, stored)
                        stored[path] = data["data"].copy()
                        writes.append(path)
                        return {}
                    if path in stored:
                        return {"data": {"data": stored[path].copy()}}
                    raise vars(helper)["APIError"](404)

                producer = next(
                    d["data"]["provision.py"]
                    for d in docs
                    if d["kind"] == "ConfigMap" and "provision.py" in d.get("data", {})
                )
                namespace: dict[str, object] = {"__name__": "fixture"}
                with (
                    patch.dict("sys.modules", {"credential_helpers": helper}),
                    patch.object(helper, "request", new=request),
                    patch.object(
                        helper, "login", return_value="fixture-token"
                    ) as login,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    exec(compile(producer, "provision.py", "exec"), namespace)
                    main = namespace["main"]
                    if not callable(main):
                        self.fail("Credential producer must expose a callable main")
                    main()
                    first = {key: value.copy() for key, value in stored.items()}
                    main()
                self.assertEqual(stored, first)
                self.assertEqual(len(writes), 1)
                self.assertEqual(login.call_count, 2)

    def test_barman_controller_is_ha_bounded_and_uses_its_native_account(self) -> None:
        from workload_contracts import Repository

        repo = Repository()
        docs = repo.render("barman-cloud")
        (deployment,) = [d for d in docs if d["kind"] == "Deployment"]
        self.assertEqual(deployment["spec"]["replicas"], 2)
        pod = deployment["spec"]["template"]["spec"]
        account = pod["serviceAccountName"]
        self.assertNotEqual(account, "default")
        self.assertTrue(
            any(
                d["kind"] == "ServiceAccount" and d["metadata"]["name"] == account
                for d in docs
            )
        )
        container = pod["containers"][0]
        self.assertTrue(container.get("readinessProbe"))
        self.assertTrue(container.get("livenessProbe"))
        self.assertEqual(set(container["resources"]["requests"]), {"cpu", "memory"})
        self.assertEqual(set(container["resources"]["limits"]), {"cpu", "memory"})

    def test_every_infra_child_has_a_declared_scope_and_public_sources_render(
        self,
    ) -> None:
        from workload_contracts import Repository

        repo = Repository()
        expected = {
            "letsencrypt-dns01-issuer",
            "timezone-defaults",
            "resource-defaults",
            "argocd-config",
            "istio-core",
            "istio-ambient",
            "prometheus-operator-crds",
            "metrics-server",
            "barman-cloud",
            "cert-manager",
            "cloudnative-pg",
            "crossplane",
            "csi-driver-smb",
            "k8up",
            "longhorn",
            "vault-secrets-operator",
            "provider-vault",
            "openbao",
            "provider-vault-connection",
            "platform-credentials",
            "letsencrypt-dns01-credentials",
            "longhorn-config",
            "openbao-backups",
            "openbao-config",
            "k3s-postgres",
            "backup-validation",
            "kube-prometheus-stack",
        }
        self.assertEqual(
            set(repo.apps),
            expected,
            "New Applications require an explicit workload contract",
        )
        for name in sorted(expected):
            with self.subTest(application=name):
                docs = repo.render(name)
                if name == "letsencrypt-dns01-issuer":
                    self.assertEqual(
                        docs,
                        [],
                        "Actual private issuer contents are integration coverage",
                    )
                else:
                    self.assertTrue(docs)
                identities = [
                    (
                        d["apiVersion"],
                        d["kind"],
                        d["metadata"].get("namespace", ""),
                        d["metadata"]["name"],
                    )
                    for d in docs
                ]
                self.assertNotIn(
                    ("batch/v1", "Job", "backup-validation", "k8up-canary-seed-v1"),
                    identities,
                    "The supervised seed Job must never have a GitOps owner",
                )
                if name == "longhorn":
                    # Helm and the later raw override must each supply this exact UI.
                    ui = ("apps/v1", "Deployment", "longhorn", "longhorn-ui")
                    self.assertEqual(identities.count(ui), 2)
                    identities.remove(ui)
                self.assertEqual(
                    len(identities),
                    len(set(identities)),
                    "Duplicate resource owner within Application",
                )

    def test_cert_manager_has_three_ha_controllers_and_native_reader_identity(
        self,
    ) -> None:
        from workload_contracts import Repository

        docs = Repository().render("cert-manager")
        deployments = [d for d in docs if d["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 3)
        for deployment in deployments:
            with self.subTest(controller=deployment["metadata"]["name"]):
                self.assertEqual(deployment["spec"]["replicas"], 2)
                pod = deployment["spec"]["template"]["spec"]
                self.assertNotEqual(pod["serviceAccountName"], "default")
                # Native cainjector has no health endpoint; do not invent probes.
                if deployment["metadata"]["name"] != "cert-manager-cainjector":
                    self.assertTrue(
                        any(c.get("livenessProbe") for c in pod["containers"])
                    )
        self.assertTrue(
            any(
                d["kind"] == "ServiceAccount"
                and d["metadata"]["name"] == "cert-manager"
                for d in docs
            )
        )

    def test_argo_tls_and_all_native_monitor_service_ports_resolve(self) -> None:
        from workload_contracts import Repository, monitor_has_target

        docs = Repository().render("argocd-config")
        (redis,) = [
            d
            for d in docs
            if d["kind"] == "Deployment" and d["metadata"]["name"] == "argocd-redis"
        ]
        self.assertIs(
            redis["spec"]["template"]["spec"]["automountServiceAccountToken"], False
        )
        params = next(
            d
            for d in docs
            if d["kind"] == "ConfigMap"
            and d["metadata"]["name"] == "argocd-cmd-params-cm"
        )
        self.assertEqual(str(params["data"]["server.insecure"]).lower(), "false")
        monitors = [d for d in docs if d["kind"] == "ServiceMonitor"]
        self.assertEqual(len(monitors), 5)
        for monitor in monitors:
            self.assertTrue(
                monitor_has_target(monitor, docs), monitor["metadata"]["name"]
            )
        server = next(
            d
            for d in docs
            if d["kind"] == "Deployment" and d["metadata"]["name"] == "argocd-server"
        )
        self.assertFalse(
            any(
                "--insecure" in c.get("args", [])
                for c in server["spec"]["template"]["spec"]["containers"]
            )
        )

    def test_ambient_mesh_has_native_node_agents_and_ztunnel_requests_are_consumed(
        self,
    ) -> None:
        from workload_contracts import Repository

        repo = Repository()
        docs = repo.render("istio-core")
        agents = {d["metadata"]["name"]: d for d in docs if d["kind"] == "DaemonSet"}
        self.assertIn("ztunnel", agents)
        self.assertIn("istio-cni-node", agents)
        self.assertFalse(
            agents["ztunnel"]["spec"]["template"]["spec"].get("hostNetwork", False)
        )
        container = agents["ztunnel"]["spec"]["template"]["spec"]["containers"][0]
        source = next(
            s
            for s in repo.apps["istio-core"]["spec"]["sources"]
            if s.get("chart") == "ztunnel"
        )
        rendered = {
            k: {r: str(v) for r, v in values.items()}
            for k, values in container["resources"].items()
        }
        expected = {
            k: {r: str(v) for r, v in values.items()}
            for k, values in source["helm"]["valuesObject"]["resources"].items()
        }
        self.assertEqual(rendered, expected)
        self.assertTrue(container.get("readinessProbe"))
        cni = agents["istio-cni-node"]["spec"]["template"]["spec"]
        self.assertTrue(any(c.get("readinessProbe") for c in cni["containers"]))
        self.assertFalse(
            any(
                c.get("securityContext", {}).get("privileged", False)
                for c in cni["containers"]
            )
        )
        # Gateway Deployment is operator-generated from this public overlay;
        # the private Gateway/Certificate binding itself is integration scope.
        config = next(
            d for d in repo.render("istio-ambient") if d["kind"] == "ConfigMap"
        )
        deployment = yaml.safe_load(config["data"]["deployment"])["spec"]
        budget = yaml.safe_load(config["data"]["podDisruptionBudget"])["spec"]
        self.assertEqual(deployment["replicas"], 3)
        self.assertEqual(
            deployment["strategy"]["rollingUpdate"],
            {"maxSurge": 0, "maxUnavailable": 1},
        )
        self.assertEqual(budget["minAvailable"], 2)
        (spread,) = deployment["template"]["spec"]["topologySpreadConstraints"]
        self.assertEqual(
            (
                spread["whenUnsatisfiable"],
                spread["topologyKey"],
                spread["maxSkew"],
                spread["minDomains"],
            ),
            ("DoNotSchedule", "kubernetes.io/hostname", 2, 2),
        )
        self.assertEqual(
            spread["labelSelector"],
            {
                "matchLabels": {
                    "gateway.networking.k8s.io/gateway-name": "istio-gateway"
                }
            },
        )
        self.assertEqual(spread["labelSelector"], budget["selector"])
        self.assertEqual(
            deployment["template"]["spec"]["securityContext"]["seccompProfile"]["type"],
            "RuntimeDefault",
        )

    def test_smb_storage_uses_secret_refs_and_keeps_the_node_plugin_eligible(
        self,
    ) -> None:
        from workload_contracts import Repository

        docs = Repository().render("csi-driver-smb")
        storage = next(
            d
            for d in docs
            if d["kind"] == "StorageClass" and d["metadata"]["name"] == "smb"
        )
        self.assertEqual(storage["provisioner"], "smb.csi.k8s.io")
        self.assertEqual(storage["reclaimPolicy"], "Retain")
        self.assertNotIn("username", storage["parameters"])
        self.assertNotIn("password", storage["parameters"])
        for phase in ("provisioner", "node-stage"):
            self.assertEqual(
                storage["parameters"][f"csi.storage.k8s.io/{phase}-secret-name"],
                "smb-credentials",
            )
        node = next(
            d
            for d in docs
            if d["kind"] == "DaemonSet" and d["metadata"]["name"] == "csi-smb-node"
        )
        self.assertNotIn(
            "compute-class", node["spec"]["template"]["spec"].get("nodeSelector", {})
        )
        self.assertTrue(node["spec"]["template"]["spec"]["containers"])

    def test_longhorn_replacement_storage_classes_preserve_volume_settings(
        self,
    ) -> None:
        from workload_contracts import Repository

        classes = [
            d for d in Repository().render("longhorn") if d["kind"] == "StorageClass"
        ]
        by_name = {d["metadata"]["name"]: d for d in classes}
        self.assertEqual(
            len(by_name), len(classes), "StorageClass names must be unique"
        )
        replacements = {
            "longhorn": "longhorn-replicated-retain",
            "longhorn-local": "longhorn-single-node-retain",
            "longhorn-monitoring": "longhorn-single-node-omv-wyse-retain",
            "longhorn-restore-test": "longhorn-single-node-delete",
        }
        for old, new in replacements.items():
            with self.subTest(storage_class=new):
                self.assertEqual(
                    by_name[new], {**by_name[old], "metadata": {"name": new}}
                )
        annotation = "storageclass.kubernetes.io/is-default-class"
        self.assertEqual(
            [
                name
                for name, doc in by_name.items()
                if doc["metadata"].get("annotations", {}).get(annotation) == "true"
            ],
            ["longhorn"],
        )

    def test_longhorn_preserves_encrypted_local_storage_and_trim_only_maintenance(
        self,
    ) -> None:
        from workload_contracts import Repository

        repo = Repository()
        docs = repo.render("longhorn")
        classes = {
            d["metadata"]["name"]: d for d in docs if d["kind"] == "StorageClass"
        }
        self.assertIn("longhorn-local", classes)
        self.assertEqual(classes["longhorn-local"]["parameters"]["encrypted"], "true")
        self.assertEqual(
            classes["longhorn-local"]["parameters"]["numberOfReplicas"], "1"
        )
        policy = repo.render("longhorn-config")
        jobs = [d for d in policy if d["kind"] == "RecurringJob"]
        self.assertTrue(jobs)
        self.assertTrue(all(d["spec"]["task"] == "filesystem-trim" for d in jobs))
        self.assertTrue(
            all(
                d["spec"]["retain"] == 0 and d["spec"]["concurrency"] == 1 for d in jobs
            )
        )
        values = repo.apps["longhorn"]["spec"]["sources"][0]["helm"]["valuesObject"]
        for field in (
            "attacherReplicaCount",
            "provisionerReplicaCount",
            "resizerReplicaCount",
            "snapshotterReplicaCount",
        ):
            self.assertEqual(values["csi"][field], 3)
        self.assertTrue(
            any(
                d["kind"] == "DaemonSet"
                and d["metadata"]["name"] == "longhorn-encrypted-discard"
                for d in policy
            )
        )

    def test_postgresql_has_three_instances_supervised_primary_and_plugin_backups(
        self,
    ) -> None:
        from workload_contracts import Repository

        docs = Repository().render("k3s-postgres")
        cluster = next(d for d in docs if d["kind"] == "Cluster")
        spec = cluster["spec"]
        self.assertEqual(spec["instances"], 3)
        self.assertEqual(spec["primaryUpdateStrategy"], "supervised")
        self.assertFalse(spec["enableSuperuserAccess"])
        self.assertTrue(spec["enablePDB"])
        self.assertEqual(
            spec["storage"],
            {"size": "40Gi", "storageClass": "longhorn-single-node-retain"},
        )
        self.assertEqual(spec["affinity"]["podAntiAffinityType"], "required")
        self.assertTrue(spec["postgresql"]["synchronous"]["failoverQuorum"])
        self.assertTrue(
            any(
                p["name"] == "barman-cloud.cloudnative-pg.io" and p["isWALArchiver"]
                for p in spec["plugins"]
            )
        )
        backup = next(d for d in docs if d["kind"] == "ScheduledBackup")
        self.assertEqual(backup["spec"]["method"], "plugin")
        self.assertEqual(backup["spec"]["cluster"]["name"], cluster["metadata"]["name"])

    def test_crossplane_provider_is_pinned_and_activation_is_not_wildcard(self) -> None:
        from workload_contracts import Repository

        repo = Repository()
        docs = repo.render("provider-vault")
        provider = next(d for d in docs if d["kind"] == "Provider")
        self.assertNotIn(":latest", provider["spec"]["package"])
        config = next(d for d in docs if d["kind"] == "DeploymentRuntimeConfig")
        self.assertEqual(
            provider["spec"]["runtimeConfigRef"]["name"], config["metadata"]["name"]
        )
        policy = next(d for d in docs if d["kind"] == "ManagedResourceActivationPolicy")
        self.assertEqual(len(policy["spec"]["activate"]), 16)
        self.assertNotIn("*", policy["spec"]["activate"])
        self.assertEqual(len(set(policy["spec"]["activate"])), 16)
        control = repo.render("crossplane")
        self.assertTrue(
            any(
                d["kind"] == "Deployment" and d["metadata"]["name"] == "crossplane"
                for d in control
            )
        )

    def test_openbao_server_agent_and_test_share_a_digest_pin(self) -> None:
        from workload_contracts import Repository

        docs = {
            (doc["kind"], doc["metadata"]["name"]): doc
            for doc in Repository().render("openbao")
        }
        server = docs["StatefulSet", "openbao"]["spec"]["template"]["spec"]
        image = next(c["image"] for c in server["containers"] if c["name"] == "openbao")
        self.assertRegex(
            image,
            r"^quay\.io/openbao/openbao:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$",
        )
        injector = docs["Deployment", "openbao-agent-injector"]
        containers = injector["spec"]["template"]["spec"]["containers"]
        sidecar = next(c for c in containers if c["name"] == "sidecar-injector")
        (agent_image,) = [
            e["value"]
            for e in sidecar["env"]
            if e["name"] == "AGENT_INJECT_VAULT_IMAGE"
        ]
        self.assertEqual(agent_image, image)
        check = docs["Pod", "openbao-server-test"]["spec"]
        self.assertEqual(check["containers"][0]["image"], image)

    def test_vso_shares_transport_but_has_no_default_authentication(self) -> None:
        from workload_contracts import Repository

        docs = Repository().render("vault-secrets-operator")
        connections = [d for d in docs if d["kind"] == "VaultConnection"]
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0]["metadata"]["name"], "default")
        self.assertFalse(
            any(d["kind"] in {"VaultAuth", "VaultAuthGlobal"} for d in docs)
        )
        helper = next(d for d in docs if d["kind"] == "SecretTransformation")
        self.assertEqual(helper["metadata"]["name"], "required-openbao-value")
        self.assertTrue(helper["spec"]["sourceTemplates"])

    def test_k8up_leader_election_and_native_metrics_have_a_real_controller(
        self,
    ) -> None:
        from workload_contracts import Repository

        docs = Repository().render("k8up")
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 2)
        env = [
            e
            for c in deployment["spec"]["template"]["spec"]["containers"]
            for e in c.get("env", [])
        ]
        self.assertIn({"name": "BACKUP_ENABLE_LEADER_ELECTION", "value": "true"}, env)
        self.assertTrue(any(d["kind"] == "ServiceMonitor" for d in docs))

    def test_monitoring_is_ha_and_grafana_uses_secret_refs_not_a_generated_admin(
        self,
    ) -> None:
        from workload_contracts import Repository

        docs = Repository().render("kube-prometheus-stack")
        prometheus = next(d for d in docs if d["kind"] == "Prometheus")
        self.assertEqual(prometheus["spec"]["replicas"], 2)
        required = prometheus["spec"]["affinity"]["podAntiAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]
        self.assertTrue(required)
        self.assertTrue(
            all(term["topologyKey"] == "kubernetes.io/hostname" for term in required)
        )
        alertmanager = next(d for d in docs if d["kind"] == "Alertmanager")
        self.assertEqual(
            alertmanager["spec"]["replicas"],
            0,
            "Do not accidentally reactivate Alertmanager",
        )
        grafana = next(
            d
            for d in docs
            if d["kind"] == "Deployment"
            and d["metadata"]["name"] == "kube-prometheus-stack-grafana"
        )
        self.assertEqual(grafana["spec"]["replicas"], 2)
        self.assertEqual(
            grafana["spec"]["strategy"]["rollingUpdate"],
            {"maxSurge": 0, "maxUnavailable": 1},
        )
        self.assertFalse(
            any(
                d["kind"] == "Secret" and d["metadata"]["name"] == "grafana-admin"
                for d in docs
            )
        )
        container = next(
            c
            for c in grafana["spec"]["template"]["spec"]["containers"]
            if c["name"] == "grafana"
        )
        env = {e["name"]: e for e in container["env"]}
        for variable, key in (
            ("GF_SECURITY_ADMIN_USER", "admin-user"),
            ("GF_SECURITY_ADMIN_PASSWORD", "admin-password"),
        ):
            self.assertEqual(
                env[variable]["valueFrom"]["secretKeyRef"],
                {"name": "grafana-admin", "key": key},
            )

    def test_backup_cronjobs_are_bounded_exclusive_and_use_scoped_or_no_identity(
        self,
    ) -> None:
        from workload_contracts import Repository

        repo = Repository()
        jobs = [
            d
            for name in ("openbao-backups", "k3s-postgres", "backup-validation")
            for d in repo.render(name)
            if d["kind"] == "CronJob"
        ]
        self.assertEqual(
            {d["metadata"]["name"] for d in jobs},
            {
                "openbao-snapshot",
                "postgresql-size-retention",
                "postgresql-restore-validation",
                "k8up-restore-test",
            },
        )
        for job in jobs:
            with self.subTest(job=job["metadata"]["name"]):
                self.assertEqual(job["spec"]["concurrencyPolicy"], "Forbid")
                pod = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                if job["metadata"]["name"] == "k8up-restore-test":
                    self.assertIs(pod["automountServiceAccountToken"], False)
                else:
                    self.assertNotEqual(pod["serviceAccountName"], "default")
                self.assertTrue(
                    job["spec"]["jobTemplate"]["spec"].get("activeDeadlineSeconds")
                )


if __name__ == "__main__":
    unittest.main()
