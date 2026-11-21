# K8up repository restore tests

| Namespace | Source in the OMV repository | Pass condition | Weekly schedule (Europe/Berlin) |
|---|---|---|---|
| `backup-validation` | Backed-up `k8up-validation-canary` PVC, snapshot no older than 48h | Exact restored bytes of `marker.txt` | Tuesday 09:15 |
| `kube-prometheus-stack` | Native replica-0 TSDB snapshot streamed by the K8up PreBackupPod, no older than 8 days | Verified Restic restore, validated tar, then a real historical node-info sample read by promtool | Tuesday 12:15 |

Both `k8up-restore-test` CronJobs ship **suspended**. Pinned [Argo CD v3.5.5 CronJob health](https://github.com/argoproj/argo-cd/blob/v3.5.5/resource_customizations/batch/CronJob/health.lua) reports a suspended CronJob as `Healthy`; that is not evidence of a successful restore test. No health override is needed. Wave 8 reconciles the seed tool, **not** a seed Job. The manual manifest is outside both Applications' source paths. These are the two configured K8up sources, not a generic restore of every namespace. The canary is a fixture; only the TSDB source exercises production data. The restore test does not create a Prometheus backup. Follow the [native backup runbook](prometheus-tsdb-backup-runbook.md) before the first TSDB restore test. New sources must declare a source/path, freshness and meaningful verifier. PostgreSQL, OpenBao, etcd and Audiobookshelf use other backup paths.

**Why not just a K8up Check or Schedule.restore?** A repository Check does not open the application data. The pinned [K8up 2.16 Schedule API](https://github.com/k8up-io/k8up/blob/v2.16.0/api/v1/schedule_types.go) can schedule a Restore, but has no condition-gated application verifier sharing its disposable target. Instead, one Kubernetes Job uses the Restic client shipped in the pinned K8up image, with a read-only repository mount, `--no-lock --no-cache`, an exact snapshot ID and `restore --verify`. Only the restore init container receives the repository password reference. The next container checks the restored files; the Prometheus template additionally runs the version-matched TSDB reader. No live database mount, Ingress, Service, service-account token, or cleanup controller is needed.

**Canary provenance.** On 2026-11-20 the live `backup-validation/validation` Schedule had no `spec.backup.tags`, and its 03:17 UTC Backup specified no tags. [K8up v2.16.0](https://github.com/k8up-io/k8up/blob/v2.16.0/restic/cli/backup.go) adds no default Restic tag. The newest snapshot for host `backup-validation` and path `/data/k8up-validation-canary` must have exactly that one path and no tags. A newer tagged snapshot fails the test; the selector does not fall back. A manual backup with the same host, path and empty tag set is indistinguishable from a scheduled one in Restic. For supporting evidence, compare the logged snapshot time and ID with the Snapshot CR and the Schedule-owned Backup and Job times; the Snapshot CR itself does not record tags. Keep manual backups distinctly tagged.

## Rollout and acceptance

If either CronJob was already synced under its previous name, keep the existing policy exclusions in both repositories until the old CronJobs are retired and their Jobs and any seed Pods have finished. With `prune: false`, renaming a manifest does not remove the old CronJob. Follow the rollback order below before switching the selectors to the new label.

In a dedicated shell, set `RESTORE_TEST_CONTEXT` to the reviewed kubeconfig
context after confirming its API server is the approved target. For each restore
run, set `RESTORE_TEST_NAMESPACE` to the reviewed source namespace from the
table (`backup-validation` or `kube-prometheus-stack`). Keep this pair unchanged
through verification, launch, and evidence collection. Pass both flags on every
namespaced API call; do not use the current context or default namespace.

1. **Sync the private Live policy companion first**, then both Infra Applications. Confirm the general validation namespace policies exclude Pods labeled `app.kubernetes.io/name: k8up-restore-test` (both restore test and seed Pods), that the isolation policy selects the Pods, and that no injected sidecar or other additive policy changes their access. Check source PVC bindings, UID 1000 read access to the retained SMB repository, and free schedulable Longhorn disk capacity on the Pod's eligible node(s), after reservations and headroom. Verify `longhorn-single-node-delete` exists in the cluster before starting either restore test. It is encrypted and strict-local: one disk on the selected node must fit the full request (256 MiB canary / 48 GiB TSDB). The TSDB Pod uses the same `monitoring: 'true'` node selector as Prometheus. The label and aggregate cluster capacity do not establish 48 GiB free; do not launch or enable the schedule if local capacity is unverified. The restore tests must not overlap repository prune/maintenance: `--no-lock` is used because the repository is read-only. Coordinate manual runs too.
   If a previous revision already synced the declarative seed Job, `prune: false` leaves it behind. Confirm it is no longer desired, verify its Argo owner and UID, and retire only that old Job under supervision before accepting wave 8. If it ran, check completion, Pod release and fixture state first; do not reseed a verified fixture.
2. For the fixture only, confirm the RWO PVC is idle and the immutable `backup-validation/k8up-canary-seed-v1` ConfigMap is synced from the reviewed revision. In an approved maintenance window, from that Infra checkout create **only** the manual Job:

   ```sh
   kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" -n backup-validation create -f 08-backup-validation/manual/seed-job.yaml -o jsonpath='{.metadata.uid}{"\n"}'
   ```

   Record the returned Job UID. `create` starts it once; if the name already exists, stop and inspect ownership/status rather than applying, replacing or unsuspending it. The seed writes the marker without replacing conflicting data. Require `Complete=True` on that UID, record the result before its one-hour TTL, and verify its owned Pods release the RWO mount. Job TTL cleans up just the Job and its Pods, not the fixture PVC or Argo-owned tools. Never clean up by the shared restore test label. Require a fresh backup of the fixture PVC and a successful repository check. For Prometheus obtain a fresh tagged backup and successful check as above. A missing/stale or mismatched snapshot is a **failure**, not a skipped test. The observed monitoring namespace had no Snapshot or Backup CR when this change was written; this is not evidence its OMV repository is empty.
3. With operator approval, clone **one** suspended CronJob at a time. Set
   `RESTORE_TEST_JOB` to a unique name for this run. To test the newest
   host/path candidate without a pin, create the clone directly and record the
   returned Job UID:

   ```sh
   kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" \
     --namespace "${RESTORE_TEST_NAMESPACE:?set the reviewed namespace}" \
     create job "${RESTORE_TEST_JOB:?set a unique Job name}" --from=cronjob/k8up-restore-test \
     -o jsonpath='{.metadata.uid}{"\n"}'
   ```

   To verify a specific Backup, **skip the direct clone** and use these gates:

   - **Verify the installed revision before cloning.** From the reviewed Infra
     commit that supports snapshot pins, render the matching Argo source
     (`08-backup-validation/resources` for `backup-validation` or
     `09-kube-prometheus-stack/restore-test` for `kube-prometheus-stack`)
     with Kustomize. Verify the installed `k8up-restore-test` CronJob is
     suspended and its Job template matches the reviewed render, accounting
     only for Kubernetes API defaults. Its `restore` init container must
     run `/bin/sh /tools/restore.sh` from the `tools`
     volume, which must reference the rendered, hash-suffixed
     `k8up-restore-tools` ConfigMap in the same namespace. Set
     `RESTORE_TEST_TOOLS` to that rendered ConfigMap name and compare its
     installed `data["restore.sh"]` byte-for-byte with the reviewed script:

     ```sh
     kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" \
       --namespace "${RESTORE_TEST_NAMESPACE:?set the reviewed namespace}" \
       get cronjob k8up-restore-test -o yaml
     kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" \
       --namespace "${RESTORE_TEST_NAMESPACE:?set the reviewed namespace}" \
       get configmap "${RESTORE_TEST_TOOLS:?set the reviewed ConfigMap name}" \
       -o jsonpath='{.data.restore\.sh}' > installed-restore-script.txt &&
       cmp adapters/k8up-restore/restore.sh installed-restore-script.txt
     ```

     Stop on missing or mismatched evidence: an older mounted script can
     ignore the pin even if the Job has the variable and completes successfully.
   - **Render and review the pinned Job.** `--dry-run=client` does not create a
     Job, but `--from` still reads the CronJob from the Kubernetes API. Render
     to a fresh local file and proceed only if the command succeeds:

     ```sh
     kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" \
       --namespace "${RESTORE_TEST_NAMESPACE:?set the reviewed namespace}" \
       create job "${RESTORE_TEST_JOB:?set a unique Job name}" --from=cronjob/k8up-restore-test \
       --dry-run=client -o yaml > restore-test-job.yaml
     ```

     With the same explicit context and namespace flags, identify the K8up
     `Snapshot` that corresponds to the completed Backup and record its full
     `.spec.id`. Confirm the manifest's name and namespace are the reviewed
     values. Set one literal `EXPECTED_RESTIC_SNAPSHOT_ID` on the `restore`
     init container to the recorded 64-character lowercase hex ID as a quoted
     string. Verify the Job spec matches the verified CronJob's Job template
     spec except for that pin, including the same command and `tools`
     reference. The pinned snapshot must still match the source, tags, and
     freshness checks; there is no fallback, even if a newer backup exists.
   - **Create only the reviewed manifest.** After operator approval, create
     it in the same context and namespace and record the returned Job UID:

     ```sh
     kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" \
       --namespace "${RESTORE_TEST_NAMESPACE:?set the reviewed namespace}" \
       create -f restore-test-job.yaml -o jsonpath='{.metadata.uid}{"\n"}'
     ```

   **Accept the run only in the same context and namespace.** Fetch the exact
   Job and confirm its UID matches the one returned by `create` and its status
   has `Complete=True`. Set `RESTORE_TEST_POD` to the Pod name after confirming
   its controller owner reference is to that Job UID. For a pinned run, check
   the Pod's `restore` init container still uses the reviewed command, `tools`
   reference, and literal pin. Read the logs by that exact Pod name:

   ```sh
   kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" \
     --namespace "${RESTORE_TEST_NAMESPACE:?set the reviewed namespace}" \
     logs "${RESTORE_TEST_POD:?set the verified Pod name}" -c restore
   kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" \
     --namespace "${RESTORE_TEST_NAMESPACE:?set the reviewed namespace}" \
     logs "${RESTORE_TEST_POD:?set the verified Pod name}" -c verify
   ```

   For a pinned run, require a single logged `snapshot_id` from `restore`: it
   must be a full 64-character lowercase hex ID exactly equal to both the pin
   and the ID recorded for the Backup. A missing, ambiguous, shortened, or
   different ID is a failure, even if the Job is `Complete=True`. In both
   modes, also require `stage=canary result=PASS` or
   `stage=read-samples result=PASS` from `verify`, as appropriate. Record the
   selected snapshot ID/time, Job condition, and verifier result before TTL,
   not raw application contents.
4. Record the restore test Pod's **own** ephemeral PVC UID, bound PV and CSI handle; check `longhorn-single-node-delete`, `Delete` and `k8up.io/backup: 'false'`. After the Job TTL (one hour), verify that this exact Pod, PVC, PV and Longhorn volume disappear on success and failure. Never force a finalizer or delete another run's resources. Only then enable each schedule by reviewed change. The fixture and both retained repository PVCs are deliberately not cleaned up.

## Rollback order (both repositories)

1. Stop manual seed/clone approvals and suspend both schedules in Git. Suspension does not stop existing Jobs or prevent manual clones. **Keep both repos' policy exclusions and the isolation policies in force.**
2. Retire the seed launch manifest/instructions and remove both CronJobs from desired state without reverting the selector patches. With `prune: false`, verify ownership and explicitly retire the two matching live CronJobs after their runs finish. Remove the seed tool from desired state; once unused, verify and retire its live ConfigMap so an old checkout cannot mount it. Check for any old declarative seed Job left by a prior sync: once no longer desired, retire only the verified namespace/name/UID; do not release it. Let outstanding manual and scheduled Jobs finish and their owned Pods and scratch claims disappear; verify there is no runnable template, seed tool or surviving restore test or seed Pod before proceeding.
3. Only then may a separate reviewed change remove unused restore test configuration or isolation policies or widen **either** Infra's egress/metrics selectors **or** Live's API/HTTPS selectors. Do not revert either branch wholesale as the first step. Preserve source PVCs, repositories, backup schedules and credentials.
