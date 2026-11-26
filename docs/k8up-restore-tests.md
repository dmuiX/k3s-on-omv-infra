# K8up repository restore tests

| Namespace | Source in the OMV repository | Pass condition | Weekly schedule (Europe/Berlin) |
|---|---|---|---|
| `backup-validation` | Backed-up `k8up-validation-canary` PVC, snapshot no older than 48h | Exact restored bytes of `marker.txt` | Tuesday 09:15 |
| `kube-prometheus-stack` | Native replica-0 TSDB snapshot streamed by the K8up PreBackupPod, no older than 8 days | Verified Restic restore, validated tar, then a historical node-info sample returned by the isolated Prometheus query API | Tuesday 12:15 |

The `backup-validation/k8up-backup-validation` and `kube-prometheus-stack/prometheus-restore-test` CronJobs ship **suspended**. Pinned [Argo CD v3.5.5 CronJob health](https://github.com/argoproj/argo-cd/blob/v3.5.5/resource_customizations/batch/CronJob/health.lua) reports a suspended CronJob as `Healthy`; that is not evidence of a successful restore test. No health override is needed. Wave 8 reconciles the seed tool, **not** a seed Job. The manual manifest is outside both Applications' source paths. These are the two configured K8up sources, not a generic restore of every namespace. The backup validation CronJob restores a synthetic fixture; only the TSDB source exercises production data. The restore test does not create a Prometheus backup. Follow the [native backup runbook](prometheus-tsdb-backup-runbook.md) before the first TSDB restore test. New sources must declare a source/path, freshness and meaningful verifier. PostgreSQL, OpenBao, etcd and Audiobookshelf use other backup paths.

**Why not just a K8up Check or Schedule.restore?** A repository Check does not open the application data. The pinned [K8up 2.16 Schedule API](https://github.com/k8up-io/k8up/blob/v2.16.0/api/v1/schedule_types.go) can schedule a Restore, but has no condition-gated application verifier sharing its disposable target. Instead, one Kubernetes Job uses the Restic client shipped in the pinned K8up image, with a read-only repository mount, `--no-lock --no-cache`, an exact snapshot ID and `restore --verify`. Only the restore init container receives the repository password reference. The next container checks the restored files; the last container in the Prometheus template starts the pinned server on the restored scratch TSDB and queries it over loopback with promtool. It uses no live configuration, discovery, or scraping. No live database mount, Ingress, Service, service-account token, or cleanup controller is needed.

**Fixture provenance.** On 2026-11-20 the live `backup-validation/validation` Schedule had no `spec.backup.tags`, and its 03:17 UTC Backup specified no tags. [K8up v2.16.0](https://github.com/k8up-io/k8up/blob/v2.16.0/restic/cli/backup.go) adds no default Restic tag. The newest snapshot for host `backup-validation` and path `/data/k8up-validation-canary` must have exactly that one path and no tags. A newer tagged snapshot fails the test; the selector does not fall back. A manual backup with the same host, path and empty tag set is indistinguishable from a scheduled one in Restic. For supporting evidence, compare the logged snapshot time and ID with the Snapshot CR and the Schedule-owned Backup and Job times; the Snapshot CR itself does not record tags. Keep manual backups distinctly tagged.

## Rollout and acceptance

With `prune: false`, syncing the renamed CronJobs leaves the previously synced `k8up-restore-test` CronJob in each namespace. Keep both replacements suspended. Under operator approval, verify the namespace, Argo owner and UID of each legacy CronJob, confirm both are suspended and wait for their Jobs and Pods to finish. Once they are absent from desired state, retire only those two verified legacy CronJobs. Do not select resources by the shared Pod label.

This is a test name change, not a data migration. The fixture PVC, Restic path, marker bytes and source repository stay unchanged so existing backups remain verifiable without reseeding. The manual seed Job and immutable seed ConfigMap retain their versioned identities to avoid a second launch or adoption of an existing object. Restore and seed Pod labels remain unchanged; neither Infra nor private Live policy selectors need edits.

1. On first installation, **sync the private Live policy companion first**, then both Infra Applications. For the rename, verify the existing policies remain in place. Confirm the general validation namespace policies exclude Pods labeled `app.kubernetes.io/name: k8up-restore-test` (both restore test and seed Pods), that the isolation policy selects the Pods, and that no injected sidecar or other additive policy changes their access. Check source PVC bindings, UID 1000 read access to the retained SMB repository, and free schedulable Longhorn disk capacity on the Pod's eligible node(s), after reservations and headroom. Verify `longhorn-single-node-delete` exists in the cluster before starting either restore test. It is encrypted and strict-local: one disk on the selected node must fit the full request (256 MiB fixture / 48 GiB TSDB). The TSDB Pod uses the same `monitoring: 'true'` node selector as Prometheus. The label and aggregate cluster capacity do not establish 48 GiB free; do not launch or enable the schedule if local capacity is unverified. The restore tests must not overlap repository prune/maintenance: `--no-lock` is used because the repository is read-only. Coordinate manual runs too.
   If a previous revision already synced the declarative seed Job, `prune: false` leaves it behind. Confirm it is no longer desired, verify its Argo owner and UID, and retire only that old Job under supervision before accepting wave 8. If it ran, check completion, Pod release and fixture state first; do not reseed a verified fixture.
2. Only if the fixture has not already been seeded, confirm the RWO PVC is idle and the immutable `backup-validation/k8up-canary-seed-v1` ConfigMap is synced from the reviewed revision. In an approved maintenance window, from that Infra checkout create **only** the manual Job:

   ```sh
   kubectl --context "${REVIEWED_CONTEXT:?set the approved context}" -n backup-validation create -f 08-backup-validation/manual/seed-job.yaml -o jsonpath='{.metadata.uid}{"\n"}'
   ```

   Record the returned Job UID. `create` starts it once; if the name already exists, stop and inspect ownership/status rather than applying, replacing or unsuspending it. The seed writes the marker without replacing conflicting data. Require `Complete=True` on that UID, record the result before its one-hour TTL, and verify its owned Pods release the RWO mount. Job TTL cleans up just the Job and its Pods, not the fixture PVC or Argo-owned tools. Never clean up by the shared restore test label. Require a fresh backup of the fixture PVC and a successful repository check. For Prometheus obtain a fresh tagged backup and successful check as above. A missing/stale or mismatched snapshot is a **failure**, not a skipped test. The observed monitoring namespace had no Snapshot or Backup CR when this change was written; this is not evidence its OMV repository is empty.
3. With operator approval, clone **one** suspended CronJob at a time as a uniquely named Job (e.g. `kubectl --context "${REVIEWED_CONTEXT:?}" -n backup-validation create job "k8up-backup-validation-${RUN_ID:?}" --from=cronjob/k8up-backup-validation`). Observe the selected snapshot ID/time and terminal verification result in the respective container logs and Job condition. Record this bounded evidence before TTL, not raw application contents. For the fixture, require `stage=backup-validation result=PASS` in the `verify` log. Require `Complete=True`, not merely a successful restore init container.
4. Record the restore test Pod's **own** ephemeral PVC UID, bound PV and CSI handle; check `longhorn-single-node-delete`, `Delete` and `k8up.io/backup: 'false'`. After the Job TTL (one hour), verify that this exact Pod, PVC, PV and Longhorn volume disappear on success and failure. Never force a finalizer or delete another run's resources. Only then enable each schedule by reviewed change. The fixture and both retained repository PVCs are deliberately not cleaned up.

## Recover a terminating canary claim

The canary is a synthetic fixture, not application data. A terminating canary
PVC can keep `backup-validation` progressing and block later waves of the root
Application, even when the child reports `Synced`. A scheduled Pod can still
[hold a claim reference after completion](https://github.com/kubernetes/kubernetes/blob/v1.37.1/pkg/controller/volume/pvcprotection/pvc_protection_controller.go#L457-L470). Check the PV's CSI volume handle against
Longhorn: a `Bound` PV with `Retain` does not prove the disk still exists.

1. Keep the restore test suspended and stop manual seed/restore launches. Pause
   the K8up Backup producer for the canary: temporarily omit `spec.backup` from
   the Live `backup-validation/validation` Schedule through a reviewed change
   and verify it reconciled. [K8up v2.16.0 removes the Backup schedule entry
   when that section is absent](https://github.com/k8up-io/k8up/blob/v2.16.0/operator/schedulecontroller/handler.go#L119-L129);
   suspending the restore CronJob alone does not pause backups. Verify no other
   schedule or manual Backup can select the canary during the pause. Wait for
   already-created Backups and their Jobs to finish; a schedule change does not
   cancel them.
   Before releasing the claim, verify there are no pending or active consumers,
   no consumers besides validation, and no repository maintenance in progress.
2. Record the terminating PVC's UID and bound PV name, UID, reclaim policy and
   CSI volume handle. Check that exact handle in Longhorn. Record the completed
   Backup and Job identities (namespace, name and UID), conditions, and the
   latest Snapshot object's identity and full ID for
   `/data/k8up-validation-canary`. If the backing volume exists, its status is
   uncertain, or the claim may contain anything besides the fixture, stop and
   establish data custody before releasing the claim.
3. If the backing volume is already gone and the only remaining claim
   references are Pods in `Succeeded` state owned by Jobs with `Complete=True`,
   retire only those Pods after approval. Verify each Pod's namespace, name,
   UID and owner Job UID. Do not use a label-wide delete or force any finalizer.
   Wait for Kubernetes to remove the **old PVC UID** naturally; the name may
   already have been reused by Argo self-heal. Stop if the old PVC remains
   terminating after its consumers are gone and investigate the blocker.

   **Retain the old objects separately.** Do not delete the completed Jobs,
   Backups, Snapshot objects or OMV repository as part of claim recovery;
   preserve the recorded evidence in case normal retention removes objects
   later. A PV with [reclaim policy `Retain`](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#retain)
   can remain `Released` with its `claimRef` pointing to the old PVC UID. Its
   disappearance is not a recovery gate. Leave that exact PV untouched; do not
   clear the claim reference or reuse the PV. Any later disposition requires
   separate review of that PV's recorded UID and CSI handle.
4. Reconcile the existing Argo-managed claim if self-heal has not already done
   so. Require `Bound` on a **different PVC UID** to a freshly provisioned PV
   with a different name and UID and a different CSI handle. Verify the PV's
   claim reference matches the replacement PVC UID and the new Longhorn volume
   exists. With `WaitForFirstConsumer`, if used in a later migration, creating
   the approved seed Job may trigger provisioning; still verify this binding
   before accepting the seed. Stop if any identity is reused or cannot be
   verified.
5. Keep the Backup producer paused through reseeding. Use the seed procedure
   in rollout step 2 and require the seed Job to complete on the replacement
   claim and its Pods to release the mount. Only then restore the reviewed Backup
   schedule, require a fresh successful Backup of the reseeded fixture and a
   successful repository Check, and run the normal supervised restore test.
   Bind the exact post-reseed Backup and its owned Job to one new Snapshot for
   `/data/k8up-validation-canary` in the same repository. Record the full
   snapshot ID from that evidence and require the restore Job's logged full ID
   to match it. If the Backup cannot be tied unambiguously to one new snapshot,
   or the restore picked a different ID, do not accept recovery even if the
   unchanged canary marker matches. Verify the parent Application resumes; a
   new binding alone is not a restore test result.

## Rollback order (both repositories)

1. Stop manual seed/clone approvals and suspend both schedules in Git. Suspension does not stop existing Jobs or prevent manual clones. **Keep both repos' policy exclusions and the isolation policies in force.**
2. Retire the seed launch manifest/instructions and remove both CronJobs from desired state without reverting the selector patches. With `prune: false`, verify ownership and explicitly retire the two matching live CronJobs after their runs finish. Remove the seed tool from desired state; once unused, verify and retire its live ConfigMap so an old checkout cannot mount it. Check for any old declarative seed Job left by a prior sync: once no longer desired, retire only the verified namespace/name/UID; do not release it. Let outstanding manual and scheduled Jobs finish and their owned Pods and scratch claims disappear; verify there is no runnable template, seed tool or surviving restore test or seed Pod before proceeding.
3. Only then may a separate reviewed change remove unused restore test configuration or isolation policies or widen **either** Infra's egress/metrics selectors **or** Live's API/HTTPS selectors. Do not revert either branch wholesale as the first step. Preserve source PVCs, repositories, backup schedules and credentials.
