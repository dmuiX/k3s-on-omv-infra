# Shutdown Pod cleanup preview

Kubernetes replaces terminal ReplicaSet Pods without deleting them on replacement readiness. Job TTL does not apply to Deployment Pods; PodGC counts all terminal Pods rather than checking the shutdown reason or owner health. Planned maintenance should still drain first. The preview is for leftovers after a node shutdown without drain.

Run the read-only preview from the repository root with an explicit approved context:

```sh
python3 -B 06-platform/shutdown-pod-preview/preview.py --context <reviewed-context>
```

Pass `--delete` to delete verified candidates (in-cluster deletes send `DeleteOptions.preconditions.uid` so only the exact verified Pod UID can be removed):

```sh
python3 -B 06-platform/shutdown-pod-preview/preview.py --context <reviewed-context> --delete
```

It inspects Pods, ReplicaSets and Deployments in the seven allowlisted namespaces (`argocd`, `audiobookshelf`, `kube-prometheus-stack`, `kube-system`, `longhorn`, `openbao`, `tekton-pipelines`). It reports candidate identities only, not raw objects or Pod logs. A reported Pod must be terminal `Failed`, have the exact kubelet node-shutdown reason and message, and have ended at least one hour ago. The controller UID chain must resolve to a listed Deployment whose current generation is fully updated and available. The old node can remain off. A candidate is **not** evidence that its original volume unmount succeeded. Do not use this as a storage health check or as proof of a clean shutdown.

The Kustomize bundle is **not registered with the root Application**. Its CronJob is suspended (`--delete` configured); the dedicated ServiceAccount has `list` + `delete` on Pods and `list` on Deployments/ReplicaSets via namespaced RoleBindings only (no ClusterRoleBinding, no Secret access). DNS egress to `kube-dns` is included; exact Kubernetes API service/control-plane egress peers must be supplied from Live before registering and unsuspending the CronJob.

Contract tests use synthetic inventories and render the actual bundle with Kustomize. A read-only local comparison is not evidence that in-cluster networking or scheduling works.
