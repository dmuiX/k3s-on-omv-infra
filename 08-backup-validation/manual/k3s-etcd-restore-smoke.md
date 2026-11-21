# Synthetic K3s/etcd restore smoke test

This one-off [Job](k3s-etcd-restore-smoke.yaml) starts an agentless K3s server,
saves a compressed etcd snapshot of a synthetic ConfigMap, and restores it
into fresh RAM-backed storage. The restored API must be ready, preserve the
original UID and value, and contain neither a later write nor any registered
nodes. The Job has no production snapshot, token, host mount or Service.
Its CLI targets the nested loopback API, not the hosting Kubernetes cluster.

The manifest is outside Argo CD's source paths. The synthetic restore passed
in an unprivileged, networkless arm64 container. **Keep this draft pending a
successful rehearsal of this pinned Job on an approved non-production amd64
cluster**. Only rehearsal placement may be narrowed; keep the non-root UID,
read-only root filesystem, RAM-backed scratch, and all other settings
unchanged. Do not relax Pod Security, add permissions, or add host access if
it fails.

Set `RESTORE_TEST_CONTEXT` to that explicitly approved hosting cluster. Verify
that its CNI enforces NetworkPolicies and the namespace does not already exist.
The checked-in Job selects only `kubernetes.io/arch: amd64`, so it can land on
any eligible amd64 node, not just an intended test node. Choose one approach:

- Use the manifest as written only if you can test effective isolation,
  including the node-local checks below, on every node eligible to run the
  admitted Job Pod. Ensure no untested node can become eligible during the run.
- Otherwise, in a rehearsal copy, keep the amd64 selector and add required
  node affinity (`matchFields` on `metadata.name`) for one explicitly named
  amd64 node. Use that copy for `create` below and verify isolation on that
  node before releasing the Job.

Check that the chosen placement can satisfy the Job's resource requests. Create
the Job suspended, then confirm `deny-all` is the only NetworkPolicy in the
dedicated namespace and has no allow rules. Check that admission will not inject
a sidecar or otherwise change the Job's placement, networking, or security
settings.

A Kubernetes NetworkPolicy is not a node firewall: traffic to or from the
Pod's own node is exempt. The policy also does not protect against privileged
node or control-plane access. Never stage real backups or server tokens here.

```sh
kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" create -f 08-backup-validation/manual/k3s-etcd-restore-smoke.yaml
kubectl --context "$RESTORE_TEST_CONTEXT" -n k3s-etcd-restore-smoke get networkpolicy -o yaml
```

Before releasing the Job, test **effective** isolation in this namespace on
each eligible node. Use separately approved, disposable, tokenless synthetic
probes matching the Job's admitted networking and security settings, without
granting RBAC or changing the policy:

- From ordinary Pods in and outside the test namespace, prove ingress to a
  probe listener is blocked. From inside the test namespace, prove egress to
  representative Pod and Service endpoints is blocked, including DNS and the
  hosting API path. Use harmless test endpoints for external paths. Verify
  listeners are otherwise reachable from an allowed control so a dead endpoint
  is not mistaken for policy enforcement.
- Check existing host/network controls for the node-local exception separately.
  Verify that the probe cannot reach sensitive node-local listeners or routes
  to management, production, or backup networks. Test applicable paths with
  bounded, credential-free reachability checks; do not rely on the policy to
  block them.

Record each probe's assigned node (`.spec.nodeName`) and bounded pass/fail
results. If any eligible node is untested or any path is unexpectedly reachable
or cannot be verified, leave the Job suspended. After isolation is proven and
probes are removed, recheck that only verified nodes are eligible, then release
the Job.

Before TTL cleanup, record the actual assigned node of every Job Pod, along
with `Complete=True` and `PASS`. A missing assignment or placement outside
the verified set invalidates the rehearsal even if the restore reports `PASS`.
A script failure emits only `FAIL: stage=<stage> exit=<status>`. If the
container never starts or is killed before reporting, record only the bounded
Pod/Job reason and exit status, if available, in addition to any assignment.
Do not dump raw logs, snapshots, tokens, or kubeconfig. Keep isolation in place
and retire only the verified test namespace under separate approval.

```sh
kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" -n k3s-etcd-restore-smoke patch job k3s-etcd-restore-smoke --type=merge -p '{"spec":{"suspend":false}}'
kubectl --context "$RESTORE_TEST_CONTEXT" -n k3s-etcd-restore-smoke wait --for=condition=complete --timeout=6m job/k3s-etcd-restore-smoke
kubectl --context "$RESTORE_TEST_CONTEXT" -n k3s-etcd-restore-smoke get pods \
  -l batch.kubernetes.io/job-name=k3s-etcd-restore-smoke \
  -o 'custom-columns=POD:.metadata.name,NODE:.spec.nodeName'
kubectl --context "$RESTORE_TEST_CONTEXT" -n k3s-etcd-restore-smoke logs --tail=5 --limit-bytes=1024 -c restore-test job/k3s-etcd-restore-smoke
```

This tests restore mechanics, not recoverability of the production backup or
post-backup automation. Those require separate proof of artifact custody and
an isolated production-data recovery procedure.
