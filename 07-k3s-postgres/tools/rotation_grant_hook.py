"""Build an Argo CD Sync hook for a single explicitly approved CNPG primary."""

import hashlib
import re
from dataclasses import dataclass
from typing import Any

K3S_IMAGE = "rancher/k3s:v1.37.2-k3s1@sha256:21ea6e0fac4e3930c2ce8aa40ddd41a8b18cb3e9a0222c030dc005e7e88c4332"
DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
SYSTEM_ID = re.compile(r"[1-9][0-9]{0,19}\Z")

RUN = """MIGRATION_PATH=/migration/grants.sql
kubectl() {
  /bin/kubectl --cache-dir=/tmp/kube-cache --request-timeout=20s -n "$TARGET_NAMESPACE" "$@"
}

cluster=$(kubectl get clusters.postgresql.cnpg.io "$TARGET_CLUSTER" \\
  -o 'jsonpath={.metadata.uid}/{.status.currentPrimary}/{.status.targetPrimary}/{.status.systemID}/{.spec.managed.roles[?(@.name=="openbao_manager")].name}')
case "$cluster" in
  */openbao_manager)
    echo 'CloudNativePG still owns openbao_manager; refusing to apply grants.' >&2
    exit 1 ;;
esac
if [ "$cluster" != "$TARGET_CLUSTER_UID/$TARGET_PRIMARY/$TARGET_PRIMARY/$TARGET_SYSTEM_ID/" ]; then
  echo 'Approved Cluster identity or primary changed; refusing to apply grants.' >&2
  exit 1
fi
owners=$(kubectl get databaseroles.postgresql.cnpg.io \\
  -o 'jsonpath={range .items[?(@.spec.name=="openbao_manager")]}{.spec.cluster.name}/{end}')
case "/$owners" in
  *"/$TARGET_CLUSTER/"*)
    echo 'CloudNativePG DatabaseRole owns openbao_manager; refusing to apply grants.' >&2
    exit 1 ;;
esac
pod=$(kubectl get pod "$TARGET_PRIMARY" \\
  -o 'jsonpath={.metadata.uid}/{.metadata.ownerReferences[?(@.controller==true)].uid}')
if [ "$pod" != "$TARGET_POD_UID/$TARGET_CLUSTER_UID" ]; then
  echo 'Primary Pod identity or ownership changed; refusing to apply grants.' >&2
  exit 1
fi
digest=$(sha256sum "$MIGRATION_PATH")
if [ "${digest%% *}" != "$EXPECTED_SQL_SHA256" ]; then
  echo 'Migration does not match the approved hook configuration.' >&2
  exit 1
fi

kubectl exec -i "$TARGET_PRIMARY" -c postgres -- \\
  env -i PATH=/usr/bin:/bin psql -X -w -h /controller/run -p 5432 -U postgres -d postgres \\
  -v ON_ERROR_STOP=1 -f - < "$MIGRATION_PATH"
"""


@dataclass(frozen=True)
class Target:
    namespace: str
    cluster: str
    cluster_uid: str
    primary: str
    pod_uid: str
    system_id: str

    def __post_init__(self) -> None:
        for value in (self.namespace, self.cluster, self.primary):
            if not DNS_LABEL.fullmatch(value):
                raise ValueError(
                    "cluster, namespace and primary must be Kubernetes DNS labels"
                )
        if not UID.fullmatch(self.cluster_uid) or not UID.fullmatch(self.pod_uid):
            raise ValueError("cluster and pod UIDs must be Kubernetes UUIDs")
        if not SYSTEM_ID.fullmatch(self.system_id) or int(self.system_id) >= 2**64:
            raise ValueError(
                "PostgreSQL system identifier must be an unsigned 64-bit decimal"
            )


def manifests(target: Target, sql: str) -> list[dict[str, Any]]:
    """Return standalone manifests; do not add the hook to production sources."""
    # An immutable input name binds a repeatable hook to its exact SQL content.
    name = f"postgres-rotation-grants-{target.cluster_uid[:8]}"
    digest = hashlib.sha256(sql.encode()).hexdigest()
    config = f"{name}-{digest[:12]}"
    namespace = target.namespace
    inputs: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": config, "namespace": namespace},
            "immutable": True,
            "data": {"grants.sql": sql},
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": name, "namespace": namespace},
            "automountServiceAccountToken": False,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": name, "namespace": namespace},
            "rules": [
                {
                    "apiGroups": ["postgresql.cnpg.io"],
                    "resources": ["clusters"],
                    "resourceNames": [target.cluster],
                    "verbs": ["get"],
                },
                {
                    "apiGroups": ["postgresql.cnpg.io"],
                    "resources": ["databaseroles"],
                    "verbs": ["list"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "resourceNames": [target.primary],
                    "verbs": ["get"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["pods/exec"],
                    "resourceNames": [target.primary],
                    "verbs": ["create", "get"],
                },
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": namespace},
            "subjects": [
                {"kind": "ServiceAccount", "name": name, "namespace": namespace}
            ],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": name,
            },
        },
    ]
    for resource in inputs:
        resource["metadata"]["annotations"] = {"argocd.argoproj.io/sync-wave": "-12"}
    return [
        *inputs,
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "annotations": {
                    "argocd.argoproj.io/hook": "Sync",
                    "argocd.argoproj.io/sync-wave": "-11",
                    "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation,HookSucceeded",
                },
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 120,
                "template": {
                    "spec": {
                        "serviceAccountName": name,
                        "automountServiceAccountToken": True,
                        "restartPolicy": "Never",
                        "enableServiceLinks": False,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 65532,
                            "fsGroup": 65532,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "apply",
                                "image": K3S_IMAGE,
                                "command": ["/bin/sh", "-ec", RUN],
                                "env": [
                                    {"name": key, "value": value}
                                    for key, value in {
                                        "TARGET_NAMESPACE": namespace,
                                        "TARGET_CLUSTER": target.cluster,
                                        "TARGET_CLUSTER_UID": target.cluster_uid,
                                        "TARGET_PRIMARY": target.primary,
                                        "TARGET_POD_UID": target.pod_uid,
                                        "TARGET_SYSTEM_ID": target.system_id,
                                        "EXPECTED_SQL_SHA256": digest,
                                        "K3S_DATA_DIR": "/tmp/k3s",
                                    }.items()
                                ],
                                "resources": {
                                    "requests": {"cpu": "25m", "memory": "64Mi"},
                                    "limits": {"cpu": "200m", "memory": "128Mi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "migration",
                                        "mountPath": "/migration",
                                        "readOnly": True,
                                    },
                                    {"name": "tmp", "mountPath": "/tmp"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "migration", "configMap": {"name": config}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
                        ],
                    }
                },
            },
        },
    ]
