"""Seed the synthetic claim and verify one exact successful K8up run."""

from functools import partial
import json
import os
from pathlib import Path
import sys
from typing import Iterator
from urllib.parse import urlencode

if __name__ == "__main__":
    sys.path.insert(0, "/runner")

from k8up_pipeline import (
    KubeApi,
    Object,
    PipelineError,
    PREFIX,
    RUN_TAG,
    UID,
    clone_job,
    clone_verifier,
    conditions,
    create_and_wait,
    identity,
    k8up_complete,
    k8up_resource,
    verify_complete,
)

NAMESPACE = "backup-validation"
CORE = f"/api/v1/namespaces/{NAMESPACE}"
K8UP = f"/apis/k8up.io/v1/namespaces/{NAMESPACE}"
BATCH = f"/apis/batch/v1/namespaces/{NAMESPACE}"
ACTIVATION = PREFIX + "fixture-pipeline"
SELECTOR = PREFIX + "backup-set"
SOURCE = "k8up-validation-canary"
POD_CONFIG = "fixture-k8up-omv-v1"
SEED = "k8up-canary-seed-template"
VERIFIER = "k8up-backup-validation-verify"
OPERATION = partial(k8up_resource, NAMESPACE, "k8up-repository", POD_CONFIG)


def inventory(api: KubeApi, path: str, selector: str = "") -> Iterator[Object]:
    token = ""
    for _ in range(100):
        query = urlencode({"limit": 50, "labelSelector": selector, "continue": token})
        page = api.request(f"{path}?{query}")
        if not isinstance(page["items"], list):
            raise PipelineError("invalid Kubernetes inventory")
        yield from page["items"]
        token = page["metadata"].get("continue", "")
        if token == "":
            return
        if not isinstance(token, str):
            raise PipelineError("invalid Kubernetes inventory continuation")
    raise PipelineError("Kubernetes inventory exceeded its page limit")


def require_idle(api: KubeApi) -> None:
    namespace = api.request(CORE)["metadata"]
    if (
        namespace.get("name") != NAMESPACE
        or namespace.get("labels", {}).get(ACTIVATION) != "enabled"
    ):
        raise PipelineError("repository cutover fence is not armed")
    # The legacy Schedule's finalizer must finish before it disappears. Merely
    # seeing an empty cron field does not prove the operator stopped scheduling.
    if next(inventory(api, f"{K8UP}/schedules"), None) is not None:
        raise PipelineError(
            "K8up Schedule still exists; no fixture operation submitted"
        )
    for kind in ("backups", "checks", "prunes", "archives", "restores"):
        for operation in inventory(api, f"{K8UP}/{kind}"):
            if conditions(operation).get("Completed", {}).get("status") != "True":
                raise PipelineError(f"unfinished K8up operation in {kind}")
    for label in ("k8upjob=true", PREFIX + "fixture-child=true"):
        for job in inventory(api, f"{BATCH}/jobs", label):
            status = conditions(job)
            if not any(
                status.get(t, {}).get("status") == "True"
                for t in ("Complete", "Failed")
            ):
                raise PipelineError("unfinished fixture or K8up Job")


def require_source(api: KubeApi) -> None:
    label = f"{SELECTOR}={SOURCE}"
    claims = list(inventory(api, f"{CORE}/persistentvolumeclaims", label))
    if len(claims) != 1:
        raise PipelineError("fixture selector does not identify exactly one claim")
    claim = claims[0]
    identity(claim, NAMESPACE, SOURCE)
    annotations = claim["metadata"].get("annotations", {})
    if (
        annotations.get("k8up.io/backup") != "true"
        or {"k8up.io/hostname", "k8up.io/backup-restic-args"}.intersection(annotations)
        or claim["spec"].get("accessModes") != ["ReadWriteOnce"]
        or claim["spec"].get("storageClassName") != "longhorn"
    ):
        raise PipelineError("canary claim does not meet the fixed source contract")
    for path, selector in (
        (f"{CORE}/pods", label),
        (f"{CORE}/pods", "k8up.io/ownerBackupUID"),
        (f"{K8UP}/prebackuppods", label),
    ):
        if next(inventory(api, path, selector), None) is not None:
            raise PipelineError("fixture selector could include a Pod backup")
    config = api.request(f"{K8UP}/podconfigs/{POD_CONFIG}")
    identity(config, NAMESPACE, POD_CONFIG)
    expected = json.loads(
        Path(__file__).with_name("fixture-pod-config.json").read_text()
    )
    # K8up v2.16 also copies the PodConfig's top-level labels and annotations.
    metadata = config["metadata"]
    if (
        config.get("spec") != expected["spec"]
        or set(metadata.get("labels", {})) - {"app.kubernetes.io/instance"}
        or set(metadata.get("annotations", {}))
        - (
            set(expected["metadata"]["annotations"])
            | {
                "argocd.argoproj.io/tracking-id",
                "argocd.argoproj.io/installation-id",
                "kubectl.kubernetes.io/last-applied-configuration",
            }
        )
    ):
        raise PipelineError("PodConfig does not match the reviewed fixture contract")


def run(api: KubeApi, pod_uid: str) -> None:
    if not UID.fullmatch(pod_uid):
        raise PipelineError("missing or invalid runner Pod UID")
    require_idle(api)
    require_source(api)
    seed = clone_job(
        api, NAMESPACE, f"{BATCH}/cronjobs/{SEED}", f"fixture-seed-{pod_uid}"
    )
    seed_uid = create_and_wait(
        api, f"{BATCH}/jobs", seed, partial(verify_complete, stage="fixture seed")
    )

    # The seed ran in a separate tokenless Job. Complete means its Pod stopped
    # before K8up tries to attach the RWO claim, even if scheduled on another node.
    require_idle(api)
    require_source(api)
    run_tag = f"backup-run-{pod_uid}"
    backup = OPERATION(
        "Backup",
        f"fx-{pod_uid}",
        labelSelectors=[{"matchLabels": {SELECTOR: SOURCE}}],
        tags=[SOURCE, run_tag],
    )
    backup_uid = create_and_wait(api, f"{K8UP}/backups", backup, k8up_complete)

    require_idle(api)
    check = OPERATION("Check", f"fixture-check-{pod_uid}")
    check_uid = create_and_wait(api, f"{K8UP}/checks", check, k8up_complete)

    require_idle(api)
    job = clone_verifier(
        api,
        NAMESPACE,
        f"{BATCH}/cronjobs/{VERIFIER}",
        f"fixture-verify-{pod_uid}",
        (("restore", "K8UP_FIXTURE_RUN_TAG", RUN_TAG),),
        {RUN_TAG: run_tag},
    )
    verifier_uid = create_and_wait(api, f"{BATCH}/jobs", job, verify_complete)
    print(
        f"stage=fixture-pipeline result=PASS run_tag={run_tag} seed_uid={seed_uid} "
        f"backup_uid={backup_uid} check_uid={check_uid} verifier_uid={verifier_uid}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        run(KubeApi(), os.environ.get("POD_UID", ""))
    except PipelineError as exc:
        sys.exit(f"Fixture backup pipeline failed: {exc}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        sys.exit(f"Fixture backup pipeline failed: {type(exc).__name__}")
