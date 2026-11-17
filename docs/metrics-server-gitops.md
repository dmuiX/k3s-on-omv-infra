# Resource Metrics API ownership

## Desired state

`01-bootstrap/metrics-server/app.yml` owns the official Metrics Server chart
3.14.0 / application 0.9.0 in `kube-system`. Its native `nodeSelector` keeps the
single replica on `compute-class=standard` (OMV/Wyse), with the Linux selector.
It still collects metrics from all three nodes. CPU/RAM requests remain
100m/70Mi; the existing unbounded CPU/RAM limits are not changed by this handoff.
This frees 70Mi of Pi requests, not the entire future Barman/database deficit.

The chart owns the Deployment, Service, ServiceAccount, RBAC and the single
`v1beta1.metrics.k8s.io` APIService. Wave -6 precedes Istiod's resource-metrics HPA;
otherwise a health-gated Istio Application can block the later Metrics API.
Runtime serving certificates preserve the packaged APIService's existing
`insecureSkipTLSVerify` behavior; kubelet certificate verification remains enabled.
There is no OpenBao, VSO, private values, certificate-manager or metrics-monitor
CRD dependency. The chart adds its native restricted container security settings.
CoreDNS, Flannel, ServiceLB and kube-system namespace metadata remain K3s-owned.

Paired Bootstrap adds only `metrics-server` to K3s's `disable` list on all servers.
No runtime Deployment patch, skip marker, generated-manifest edit, replacement
controller or migration script is used. Clean-install base acceptance requires
CoreDNS, not the Metrics API that Argo installs afterward.

## Existing-cluster transition: blocked until separately approved

**Neither PR is a deployment instruction. Keep both as drafts until the complete
handoff and its exact commands are reviewed. Do not activate this Application
while K3s still manages the packaged resources.**

K3s's [packaged-component documentation](https://docs.k3s.io/installation/packaged-components)
states that `disable` removes the packaged resources. Adding the flag is not a
placement override, and disabling it on only the Pi does not move the Pod.
K3s servers can independently reapply their packaged manifests.

The existing K3s Deployment selects `k8s-app=metrics-server`; the official chart
uses a different **immutable Deployment selector**. Normal SSA cannot adopt that
Deployment in place. Never use force/replace or a second competing reconciler.
The new default image is the upstream 0.9.0 image rather than K3s's mirrored 0.9.0
image; validate that artifact and transport during the attended rollout.

Review these operations together before running any of them:

1. Hold Infra root reconciliation before publishing the new Application to main.
   Record the K3s add-on owners, current APIService backend, Argo revision and
   server configurations for rollback. Require fresh API/etcd peer, worker,
   healthy remaining database-instance/replication and attached-volume evidence;
   inspect queued GitOps effects. Do not restart storage hosts merely because a
   Pending database needs memory.
2. Render the paired Bootstrap configuration for all servers. The classifier
   compares the complete config exactly: old files lacking the disable entry
   become `AMBIGUOUS`. Normal resume does not rewrite them or activate the flag.
   An attended config/serial-service handoff is required; do not weaken the
   classifier, reset acceptance or run the installer against an existing cluster.
3. With separate command-specific approval, transition only the reviewed server
   config and required services serially, verifying peer/storage recovery before
   proceeding. Confirm no server can recreate the packaged Metrics Server and
   K3s has completed retirement of its resources before enabling Argo ownership.
   A temporary resource-Metrics API outage is expected in this transition; HPA
   resource inputs and `kubectl top` can be unavailable. CoreDNS must stay ready.
4. Activate the chart through Argo, not a manual Helm release or kubectl patch.
   Verify exactly one owner, standard-node placement and the APIService backend.
   Wait for Deployment readiness and `APIService Available=True`, then check fresh
   node metrics for all three nodes, Pod metrics and Istiod HPA `ScalingActive`.
   Recalculate Pi requests, the third DB and Barman separately; no database
   switchover or storage operation is authorized by this metrics handoff.

Example read-only acceptance commands:

```sh
kubectl --context k3s-on-omv -n kube-system get pods -l app.kubernetes.io/name=metrics-server -o wide
kubectl --context k3s-on-omv wait apiservice/v1beta1.metrics.k8s.io --for=condition=Available --timeout=180s
kubectl --context k3s-on-omv get --raw /apis/metrics.k8s.io/v1beta1/nodes
kubectl --context k3s-on-omv top nodes
kubectl --context k3s-on-omv -n istio-system get hpa istiod
```

## Rollback

Rollback is the reverse attended ownership handoff, not an immediate source
revert. Hold Argo first; separately review retirement of only chart-owned metrics
resources before allowing K3s to recreate its add-on. Pruning is disabled, so
removing the Application from Git does not retire its resources. Restore the
reviewed server configuration serially and require the restored Metrics API and
fresh node/Pod metrics. Never run both owners, force selectors, disable CoreDNS or
change database/storage resources to make this migration appear successful.

## Evidence boundary

The chart archive is SHA-256 pinned in `tests/ci-dependencies.json`. Public tests
render the real chart, check placement, resource preservation, RBAC/Service/API
wiring, probes, security and an intentionally misspelled-value negative control.
Bootstrap tests render every server config and retain the old-config refusal.
These are source/render tests, not proof of actual K3s retirement, Argo adoption,
three-node metric freshness, HPA behavior or production rollback.
