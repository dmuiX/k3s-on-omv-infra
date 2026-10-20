# Longhorn 1.13 startup conflicts

## Observed evidence (2026-10-20)

- Wyse manager startup failed with an optimistic-concurrency conflict on
  `settings.longhorn.io/default-replica-count`. The OMV manager's admission logs
  show repeated updates converting scalar `3` into `{"v1":"3","v2":"3"}`.
- A replacement Wyse manager failed once on its own Pod object after starting
  the recovery backend. It then restarted successfully. Pod managed fields show
  Kubernetes managing status and Longhorn managing webhook/recovery labels.
  Managed fields are not an audit trail; the exact conflicting request is unknown.
- Both managers subsequently became Ready; no hardware failure was established.
- A separate Argo sync failure concerns immutable parameters of StorageClass
  `longhorn`. The chart's `longhorn-storageclass` ConfigMap instructs Longhorn to
  create an unencrypted, non-default class. Infra declares an encrypted default
  class with the same name. The live class matches the chart ConfigMap, not Infra.

## Version-specific mechanisms

- [`UpdateCustomizedSettings`](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/datastore/longhorn.go)
  compares raw customized values against persisted settings. Its sync path writes
  the scalar representation again, while the admission webhook normalizes it.
  The initial setting update is not protected by the retry that covers subsequent
  annotation cleanup. Concurrent startup/configmap reconciliation can conflict.
- [`AddLabelToManagerPod`](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/datastore/kubernetes.go)
  reads a Pod and performs a full update without a conflict retry. Pod status
  changes can invalidate that resource version even when different fields change.
- [`startManager`](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/app/daemon.go)
  returns these errors to a fatal handler. Kubernetes then restarts the container.
- The [chart's supported `persistence.createStorageClass` option](https://github.com/longhorn/charts/blob/longhorn-1.13.0/charts/longhorn/values.yaml)
  disables the competing ConfigMap producer. Setting `defaultClass: false` alone
  only changes its default-class annotation; it does not disable class creation.
- The [ConfigMap controller](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/controller/kubernetes_configmap_controller.go)
  can delete/recreate the default StorageClass from that ConfigMap. Leaving both
  configuration sources active is not safe ownership.

## Changes and limits

Use the canonical JSON replica setting and disable the chart StorageClass producer.
Verify the rendered chart: canonical setting, no `longhorn-storageclass` ConfigMap.
The contract test guards the repository configuration, not Longhorn's concurrent
startup internals. No custom Longhorn image, downgrade, or retry wrapper is used.
The Pod-label retry defect is upstream and is not fixed by these values changes.

Because pruning is disabled, removing the ConfigMap from desired state does not
remove an already-created ConfigMap. Existing installations require explicit
cleanup of that obsolete producer. The wrongly-created StorageClass has immutable
parameters and requires a separately approved recreation after inspecting claims,
PVs and Longhorn volumes. Never delete volumes or claims to repair this class.
Disabling the producer alone does not repair the existing class.
