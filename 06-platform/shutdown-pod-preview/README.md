# Shutdown Pod cleanup preview

Kubernetes replaces terminal ReplicaSet Pods without deleting them on replacement readiness. Job TTL does not apply to Deployment Pods; PodGC counts all terminal Pods rather than checking the shutdown reason or owner health. Planned maintenance should still drain first. The preview is for leftovers after a node shutdown without drain.

Run the read-only preview from the repository root with an explicit approved context:

```sh
python3 -B 06-platform/shutdown-pod-preview/preview.py --context <reviewed-context>
```

It uses only `kubectl get --raw` for Pods, ReplicaSets and Deployments in four named namespaces. It reports candidate identities only, not raw objects or Pod logs. A reported Pod must be terminal `Failed`, have the exact kubelet node-shutdown reason and message, and have ended at least one hour ago. The controller UID chain must resolve to a listed Deployment whose current generation is fully updated and available. The old node can remain off. A candidate is **not** evidence that its original volume unmount succeeded. Do not use this as a storage health check or as proof of a clean shutdown.

The Kustomize bundle is **not registered with the root Application**. Its CronJob is suspended; the dedicated ServiceAccount can list only the three resource types in the four RoleBinding namespaces and cannot write or read Secrets. It has no egress until a separately reviewed exact API/DNS policy is supplied from Live. A future in-cluster preview must first check that all target namespaces exist, add that policy and register the suspended bundle before separately enabling scheduling. Preview only: there is no deletion code, permission or mode to enable. The checkout can be reverted without changing the running cluster. No automatic deletion is part of this change; Job lifecycle policy is separate.

Contract tests use synthetic inventories and render the actual bundle with Kustomize. A read-only local comparison is not evidence that in-cluster networking or scheduling works.
