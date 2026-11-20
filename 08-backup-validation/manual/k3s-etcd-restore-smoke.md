# Synthetic K3s/etcd restore smoke test

This one-off [Job](k3s-etcd-restore-smoke.yaml) starts an agentless K3s server,
saves a compressed etcd snapshot of a synthetic ConfigMap, and restores it
into fresh RAM-backed storage. The restored API must be ready, preserve the
original UID and value, and contain neither a later write nor any registered
nodes. The Job has no production snapshot, token, host mount or Service.
Its CLI targets the nested loopback API, not the hosting Kubernetes cluster.

The manifest is outside Argo CD's source paths. The synthetic restore passed
in an unprivileged, networkless arm64 container. Running it as a non-root
Kubernetes Pod on amd64 is the next acceptance step, not a validated result.
Do not relax Pod Security or add host access if it fails.

Set `RESTORE_TEST_CONTEXT` to the explicitly approved hosting cluster. Verify
that its CNI enforces NetworkPolicies, the namespace does not already exist,
and an amd64 node fits the requested resources. Create the suspended Job, then
confirm `deny-all` is the only NetworkPolicy in the dedicated namespace and
denies all ingress and egress. Before the first run, record deny-all enforcement
for this namespace on an approved non-production amd64 host cluster. Do not
release the Job based on the YAML alone. Runtime isolation does not protect against privileged
node or control-plane access, so never stage real backups or server tokens here.

```sh
kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" create -f 08-backup-validation/manual/k3s-etcd-restore-smoke.yaml
kubectl --context "$RESTORE_TEST_CONTEXT" -n k3s-etcd-restore-smoke get networkpolicy -o yaml
```

After the isolation check, release the Job and record both `Complete=True`
and `PASS` before TTL cleanup. If the Job fails, inspect only this synthetic
Pod and keep the original isolation in place. After the run, retire only the
verified test namespace under separate approval.

```sh
kubectl --context "${RESTORE_TEST_CONTEXT:?set the approved context}" -n k3s-etcd-restore-smoke patch job k3s-etcd-restore-smoke --type=merge -p '{"spec":{"suspend":false}}'
kubectl --context "$RESTORE_TEST_CONTEXT" -n k3s-etcd-restore-smoke wait --for=condition=complete --timeout=6m job/k3s-etcd-restore-smoke
kubectl --context "$RESTORE_TEST_CONTEXT" -n k3s-etcd-restore-smoke logs job/k3s-etcd-restore-smoke
```

This tests restore mechanics, not recoverability of the production backup or
post-backup automation. Those require separate proof of artifact custody and
an isolated production-data recovery procedure.
