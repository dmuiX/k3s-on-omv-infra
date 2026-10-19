# Prometheus snapshot to K8up runbook

This workflow backs up only a native Prometheus TSDB snapshot. It must never point K8up at the live Prometheus PVC or copy live block files. K8up 2.16 has no conditional post-backup hook, so source cleanup is deliberately a separate, fail-closed step.

## Preconditions

1. Verify that the pinned multi-architecture `python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641` index still resolves to Linux amd64 and arm64 manifests. The snapshot implementation uses only Python's standard library.
2. Verify the rendered replica-0 PVC name used by `prometheus-tsdb-snapshot`, same-node RWO attachment, admin API reachability, SMB mount permissions, repository locking, and independent custody of the Restic password.
3. Verify `prometheus-k8up-repository` is bound through StorageClass `smb` and maps to retained subdirectory `kube-prometheus-stack/prometheus-k8up-repository` beneath `//192.168.1.5/k3s-cluster` on the mounted backup disk.
4. Use a new UTC run ID for every object below. Never apply these examples with the literal `RUN_ID`.

## 1. Submit one one-shot Backup

Create exactly one uniquely named resource from this template:

```yaml
apiVersion: k8up.io/v1
kind: Backup
metadata:
  name: prometheus-tsdb-RUN_ID
  namespace: kube-prometheus-stack
spec:
  backend:
    repoPasswordSecretRef: {name: prometheus-k8up-repository, key: password}
    local: {mountPath: /repository}
    volumeMounts: [{name: repository, mountPath: /repository}]
  podConfigRef: {name: prometheus-k8up-omv}
  labelSelectors:
    - matchLabels: {platform.k3s-on-omv.io/backup-set: prometheus-tsdb}
  tags: [prometheus-tsdb, prometheus-replica-0]
  activeDeadlineSeconds: 7200
  failedJobsHistoryLimit: 3
  successfulJobsHistoryLimit: 3
```

The selector intentionally matches the `PreBackupPod` and no PVC. The pre-backup command invokes the native snapshot API and streams only the returned snapshot directory to Restic.

## 2. Verify Backup and repository

Do not infer success from `status.finished`. Require the Backup condition `type=Completed`, `status=True`, and `reason=Succeeded`, no failed K8up Job for the run, and a new K8up `Snapshot` record for repository `/repository`.

Then create a uniquely named repository check:

```yaml
apiVersion: k8up.io/v1
kind: Check
metadata:
  name: prometheus-tsdb-check-RUN_ID
  namespace: kube-prometheus-stack
spec:
  backend:
    repoPasswordSecretRef: {name: prometheus-k8up-repository, key: password}
    local: {mountPath: /repository}
    volumeMounts: [{name: repository, mountPath: /repository}]
  podConfigRef: {name: prometheus-k8up-omv}
  activeDeadlineSeconds: 7200
  failedJobsHistoryLimit: 3
  successfulJobsHistoryLimit: 3
```

Require the same `Completed=True`, `reason=Succeeded` condition on the Check. A restore into an isolated target and checksum comparison remains the acceptance test; repository metadata alone is insufficient.

## 3. Clean up the native snapshot

The pre-backup command writes the strictly validated native snapshot name to `/prometheus/snapshots/.k8up-pending`. If either the Backup or Check gate fails, retain the marker and snapshot for investigation and retry.

Create the following Job with a new run ID while it is suspended. It mounts replica 0's PVC on the same node, validates the marker before constructing the target path, deletes exactly that snapshot directory, and removes the marker last.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: prometheus-tsdb-cleanup-RUN_ID
  namespace: kube-prometheus-stack
spec:
  suspend: true
  backoffLimit: 0
  activeDeadlineSeconds: 600
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 2000, fsGroup: 2000, seccompProfile: {type: RuntimeDefault}}
      affinity:
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector: {matchLabels: {statefulset.kubernetes.io/pod-name: prometheus-kube-prometheus-stack-prometheus-0}}
              topologyKey: kubernetes.io/hostname
      containers:
        - name: cleanup
          image: python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641
          command: [/usr/local/bin/python, -c]
          args:
            - |
              from pathlib import Path
              import re
              import shutil
              root = Path("/prometheus/snapshots")
              marker = root / ".k8up-pending"
              name = marker.read_text().strip()
              if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9]+", name):
                  raise SystemExit("invalid pending snapshot name")
              target = root / name
              if target.parent != root or not target.is_dir() or target.is_symlink():
                  raise SystemExit("pending snapshot target is not a safe directory")
              shutil.rmtree(target)
              marker.unlink()
          resources: {requests: {cpu: 25m, memory: 32Mi}, limits: {cpu: 250m, memory: 128Mi}}
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          volumeMounts: [{name: prometheus-data, mountPath: /prometheus}]
      volumes:
        - name: prometheus-data
          persistentVolumeClaim: {claimName: prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0}
```

After independently verifying both exact success gates, start only this reviewed Job with `kubectl patch job prometheus-tsdb-cleanup-RUN_ID -n kube-prometheus-stack --type=merge -p '{"spec":{"suspend":false}}'`. Require Job completion and verify both the marker and named snapshot directory are absent. If any gate or cleanup check fails, leave the Job suspended and retain the snapshot. Never place deletion in the `PreBackupPod`, a lifecycle hook, or an unconditional K8up post-backup action.

## 4. Apply retention after success

After verified Backup and Check success (cleanup may remain blocked), create a uniquely named native Prune:

```yaml
apiVersion: k8up.io/v1
kind: Prune
metadata:
  name: prometheus-tsdb-prune-RUN_ID
  namespace: kube-prometheus-stack
spec:
  backend:
    repoPasswordSecretRef: {name: prometheus-k8up-repository, key: password}
    local: {mountPath: /repository}
    volumeMounts: [{name: repository, mountPath: /repository}]
  podConfigRef: {name: prometheus-k8up-omv}
  retention:
    keepDaily: 7
    keepWeekly: 4
    tags: [prometheus-tsdb]
  activeDeadlineSeconds: 7200
  failedJobsHistoryLimit: 3
  successfulJobsHistoryLimit: 3
```

Require `Completed=True`, `reason=Succeeded`. Prune controls Restic retention; it does not authorize deletion of a native Prometheus snapshot.
