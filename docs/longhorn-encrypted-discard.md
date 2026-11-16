# Encrypted Longhorn discard: bootstrap policy and live rollout

## Status and scope

This change is a **PR proposal**, not a record of a live deployment. No encrypted mapping, LUKS header, host setting, snapshot policy or production workload was changed to test this PR. Merge is a production-impacting action: the existing automated `longhorn-config` Application will deploy the resources below.

The requested policy is persistent discard on **all Longhorn v1 LUKS2 volumes using the Bootstrap-created `longhorn/longhorn-crypto` key**, including volumes created after installation. It does not target host/system disks. All inspected cluster volumes currently use Longhorn v1 and this key. V2/SPDK, LUKS1, another encryption key and another iSCSI identity are not silently supported; unsupported active devices fail closed and require separate validation.

TRIM reveals which encrypted blocks are free/used; contents remain encrypted. This is a deliberate operator-approved policy, not a claim that discard has no security trade-off.

The implementation has exactly three responsibilities: (1) persistently enable discard on existing encrypted volumes, (2) do the same when a new volume is attached, and (3) let Longhorn's native scheduled trim reclaim freed blocks. **No StorageClass, PVC, encryption format, volume size or Longhorn image is created/replaced/changed.**

## Integration and ownership

`06-platform/longhorn-config/resources/encrypted-discard.yaml` is already under the canonical `longhorn-config` Application source. No separate bootstrap imperative apply or new root Application is needed. Bootstrap creates/verifies the retained `longhorn-crypto` Secret in `ansible/roles/gitops_bootstrap/tasks/longhorn-key.yml` **before** installing the GitOps Applications. The Application's wave 6 resources then install the policy; the existing key workflow remains authoritative and unchanged.

Resources in namespace `longhorn`:

1. ServiceAccount `longhorn-encrypted-discard`, with token automount disabled and no API RBAC.
2. ConfigMap `longhorn-encrypted-discard`, holding the reviewed Python program.
3. DaemonSet `longhorn-encrypted-discard`, one process per eligible Linux node, tolerating the storage Pi's existing taint.
4. NetworkPolicy `longhorn-encrypted-discard`, denying pod ingress and egress.
5. RecurringJob `encrypted-volume-trim`, task `filesystem-trim`, daily `35 18 * * *`, concurrency 1, group `default`. The cluster's native [timezone enforcement](timezone-defaults.md) requires `spec.timeZone: Europe/Berlin` when Longhorn creates or updates its generated CronJob: 18:35 local is 17:35 UTC in winter and 16:35 UTC in summer. The container's clock does not schedule the Job. Existing generated CronJobs without `timeZone` need the separately approved backfill described there; until then they still follow the controller's local timezone. Do not claim explicit timezone coverage before checking the live field.

All inspected existing volumes have `recurring-job-group.longhorn.io/default=enabled`; new default-group volumes inherit the job. A volume deliberately excluded from that group is not silently relabeled. No setting enabling recurring-job attachment of detached volumes is changed. No snapshot is deleted by this manifest, and `removeSnapshotsDuringFilesystemTrim` remains unchanged: snapshots can still legitimately retain old blocks. Cleanup of previously retained snapshots is a separate approval, not a side effect of the new global discard policy.

## How the host helper works

The process scans every 60 seconds. It only considers names under the host's `/dev/longhorn`, rejects unsafe names, checks block major/minor numbers against the actual LUKS backing device, and verifies the kernel iSCSI session's target is exactly `iqn.2019-10.io.longhorn:<volume-name>`. It does not identify devices merely by `/dev/sdX`, by a PVC-looking name, or by container-local mapper contents.

For an attached, writable LUKS2 mapping it checks **both** the live `discards` flag and the persistent LUKS metadata flag `allow-discards`. If needed it rechecks identity, takes a node-local flock, and executes the host's existing cryptsetup:

```text
cryptsetup --batch-mode --allow-discards --persistent --key-file=- refresh <validated-volume-name>
```

The existing Secret is mounted read-only with **only** `CRYPTO_KEY_VALUE`, mode 0400. Python opens that projected file as the subprocess stdin; no key is put in argv, environment variables, logs, generated host files or Git. Command output and LUKS metadata are captured internally and never logged. The script does not format devices, open arbitrary mappings, delete slots, remove snapshots or change volume sizes.

The image is the existing pinned multiarchitecture Python 3.13 Bookworm image used by the credential provisioners; `nsenter` availability was checked in that exact image. Host cryptsetup is already a Bootstrap prerequisite. The DaemonSet uses host PID plus a **privileged root container** to enter host mount/IPC namespaces and perform device ioctls. No hostPath volume or host network namespace is mounted/requested, but privileged host-PID access is still effectively host-administrator authority. A compromised pod or someone able to modify this ConfigMap can compromise a node: the NetworkPolicy is not a sandbox against privileged namespace entry. Protect namespace/Argo write access accordingly.

Memory is limited to 2GiB, with serial key operations; LUKS keyslots requiring more than 1536MiB of KDF memory are rejected instead of attempted. Immediately before authentication, host `MemAvailable` must exceed the keyslot's KDF requirement by at least 256MiB; otherwise the scan fails readiness and retries later. Existing inspected volumes use up to 1GiB of Argon2 memory, so a 1GiB pod limit would not safely cover all of them. Each host operation owns a new session/process group. The command deadline is 120 seconds; cancellation is checked at most every 250ms. Timeout or helper shutdown sends SIGTERM to that private group, waits 1 second, then sends SIGKILL and waits up to 5 seconds for completion before retrying. The group leader is retained until both signals are sent, preventing PID/PGID reuse from targeting a different group. If signalling or reaping cannot be confirmed, the helper exits instead of processing another device. The scan interval is 60 seconds and the pod graceful-stop budget is 150 seconds. Readiness runs the same program in key-free audit-only mode against the actual current mappings; liveness checks that the main process exists. An operation is retried only after its owned process group has been stopped and the direct child reaped; no separate heartbeat/state-file subsystem is needed. A script SHA-256 in the pod template forces a rollout on code changes and is checked by tests. Persistent flags survive remapping; polling also handles later attached volumes and repairs missing live flags.

## Planned live rollout — exact operations, not executed for this PR

### 1. Review and preflight

- Review the new privileged workload and the projected-key use. Do not copy the encryption key out of Kubernetes or print the Secret.
- Confirm each target node has `cryptsetup`, `flock`, `stat`, `readlink`, `cat` and `ls` at the paths used by the helper.
- Confirm volumes are healthy and PostgreSQL's standbys are really streaming, not merely Ready. Check backup/recovery posture separately; the previous operator-deleted backups are not silently recreated by this change.
- Confirm the node KDF requirement fits the pod budget, using only a filtered metadata diagnostic (never dump Secret values or device-mapper tables).

From a reviewed checkout of this PR:

```sh
kubectl diff -f 06-platform/longhorn-config/resources/encrypted-discard.yaml
kubectl apply --dry-run=server -f 06-platform/longhorn-config/resources/encrypted-discard.yaml
```

`kubectl diff` normally exits 1 for expected differences. Neither command above changes live state.

### 2. Normal path: GitOps

Merge the approved PR into Infra `main`. The existing automated Application applies **only the five resources listed above** for this feature, through its existing source directory. It will also reconcile its other pre-existing resources as usual. Do not bypass a failing existing sync hook without diagnosing it.

If Git discovery needs a refresh, the optional metadata-only command is:

```sh
kubectl annotate application -n argocd longhorn-config \
  argocd.argoproj.io/refresh=hard --overwrite
```

For a **separately approved manual fallback**, the equivalent apply would be:

```sh
kubectl apply --server-side \
  -f 06-platform/longhorn-config/resources/encrypted-discard.yaml
```

Do not run a fallback from an unreviewed local revision. Do not introduce an unrelated device loop or a key-bearing `cryptsetup` command on a shell command line. The `longhorn-config` Application must remain the desired-state owner; this is not a permanent out-of-Git patch.

### 3. Acceptance and initial trim

```sh
kubectl rollout status -n longhorn daemonset/longhorn-encrypted-discard --timeout=120s
kubectl get pods -n longhorn -l app.kubernetes.io/name=longhorn-encrypted-discard -o wide
kubectl logs -n longhorn -l app.kubernetes.io/name=longhorn-encrypted-discard \
  -c reconcile --tail=50 --prefix
kubectl get recurringjob,cronjob -n longhorn encrypted-volume-trim
```

A ready DaemonSet is necessary but insufficient. On **each node**, independently verify selected mappings' `cryptsetup status` shows `flags: discards`, filtered LUKS metadata contains `allow-discards`, and `lsblk -D` reports discard on the encrypted mapping. Verify host/system mappings are unchanged. Stop on identity, KDF, authentication or postcondition failures; do not suppress readiness checks.

After all node policies pass, the exact initial execution of the **native Longhorn** trim task would be:

```sh
kubectl create job -n longhorn \
  --from=cronjob/encrypted-volume-trim encrypted-volume-trim-acceptance
kubectl logs -n longhorn job/encrypted-volume-trim-acceptance --all-containers
kubectl get job -n longhorn encrypted-volume-trim-acceptance
```

Do not blindly repeat a create command after an ambiguous timeout; inspect the uniquely named Job first. This one-off Job is Kubernetes-managed and uses Longhorn's existing task implementation. Require terminal success, healthy volumes, ongoing database streaming, actual free/allocated-byte evidence and no host-disk mutation. Record Longhorn actual size and filesystem usage separately; a successful fstrim is not proof that snapshot-pinned blocks disappeared. The daily CronJob can then handle later free-space reclamation. Remove the terminal one-off Job only after its logs/evidence are recorded, using an explicitly approved cleanup.

The exact pinned container and actual ConfigMap entrypoint were exercised on OMV in **audit-only mode**, with no key mount. Host namespace entry and real device/iSCSI discovery worked, and the audit reported missing discard without changing flags; the test container exited and was removed. The first audit also exposed two larger KDF parameters, prompting the memory-budget correction above. No real key/mapping mutation or TRIM was tested for this PR. Rollout, activation, reattach persistence and physical reclamation remain **open merge/deployment acceptance gates**, not inferred from this read-only audit or unit tests.

## Rollback

**Stopping the helper does not revert persistent LUKS flags.** First remove/disable the policy's GitOps resources in a reviewed follow-up. Because this Application has `prune: false`, deleting YAML from Git alone does not delete its live resources. With explicit approval, delete exactly the named DaemonSet and RecurringJob, suspend/inspect its generated CronJob if still present, and wait for helper pods and any trim Jobs to stop. Do not remove `longhorn-crypto` or unrelated Longhorn resources. Remove the dedicated ConfigMap, ServiceAccount and NetworkPolicy only after the workload is stopped.

If the policy itself must be reversed, use a separately reviewed, authenticated operation on the same independently validated devices to refresh persistent activation flags **without** `--allow-discards`, preserving any other intentionally configured flags. Feed the key through a projected file/stdin, not argv or copied files; verify both persistent and live flags afterward. Do not run a blanket refresh against all host LUKS mappings. Blocks already discarded and old snapshot rollback points already removed cannot be recovered by disabling discard. Shrinking PVCs is unrelated and is not part of rollback.

## Tests and scope limitations

`tests/test_longhorn_discard.py` exercises the **actual ConfigMap source**, including idempotence, active versus persistent state, audit-only mode, unsafe names, kernel iSCSI identity, backing-device mismatches, key failure, KDF limits, timeouts, output redaction, shutdown/probe behavior and resource constraints. `tests/test_longhorn_discard_processes.py` additionally runs real `flock` and descendant processes through the actual ConfigMap host wrapper and sends a real SIGTERM through its main entrypoint. It verifies that a SIGTERM-ignoring descendant cannot perform a delayed action after cancellation, that the lock is released and that an unrelated process survives. The same tests pass in the exact pinned image without privileges, host PID access, network, keys or devices. A Linux subreaper fixture reaps all synthetic descendants. These checks do not authenticate a real production key or prove kernel/CSI behavior. The exact pinned image's entrypoint/tool availability, Python type/lint checks and manifest server dry-run are checked separately. The storage-admin privilege exception in the existing Ruby contract is restricted to this exact DaemonSet/namespace/container identity; other application containers retain the prior hardening requirements.

The existing repository-wide baseline failures are reported with the PR rather than weakened or hidden. See the PR review artifact for exact results. Upstream context: [Longhorn #5599](https://github.com/longhorn/longhorn/issues/5599) explains why no built-in global encrypted-discard toggle exists. [PostgreSQL WAL recovery](postgresql-wal-recovery.md) records the incident that motivated this policy; enabling discard is not a fix for archive-only replication or slot-retention bugs.
