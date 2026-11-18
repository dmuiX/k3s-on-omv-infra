# Resource Metrics API ownership

## Desired state

`01-bootstrap/metrics-server/app.yml` owns the official Metrics Server chart
3.14.0 / application 0.9.0 in `kube-system`. Its native `nodeSelector` keeps both
replicas on `compute-class=standard` (OMV/Wyse), with the Linux selector. Required
Pod anti-affinity on `kubernetes.io/hostname` places one replica on each host.
Each replica collects metrics from all three nodes. Per-Pod CPU/RAM requests
remain 100m/70Mi, totaling 200m/140Mi; the existing unbounded CPU/RAM limits are
unchanged. The second replica adds 100m/70Mi of requests on a standard node and
another set of kubelet scrapes, not load on the Pi scheduler budget. Removing the
packaged Pi Pod frees 70Mi of Pi requests, not the entire future Barman/database deficit.

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

## Two-replica availability

The pinned chart natively supports `replicas`, `affinity`, `updateStrategy` and
`podDisruptionBudget`; no custom workload or budget manifest is needed. The
anti-affinity selector matches the chart's actual Pod labels across revisions.
One chart-owned `policy/v1` PodDisruptionBudget with `minAvailable: 1` protects
against evicting the last healthy replica during voluntary maintenance.

With hard host separation and only two eligible hosts, the Deployment uses
`maxSurge: 0` and `maxUnavailable: 1`. Default surge-only rolling updates would
wait for a third eligible host; the explicit strategy lets one Pod update while
the other remains available. A PDB does not govern Deployment rolling updates,
so the strategy is necessary in addition to the budget.

If OMV or Wyse is unavailable, one replica remains and its replacement stays
Pending until another eligible host is available. Do not relax placement onto
the Pi merely to make two replicas Ready. The budget cannot prevent an
involuntary node failure and may block draining the last remaining healthy Pod.

Upstream [HA guidance](https://github.com/kubernetes-sigs/metrics-server#high-availability)
recommends `--enable-aggregator-routing=true` on API servers for request load
balancing. This PR does not change K3s flags or restart hosts; host-side routing
is a separate inspection/approval, not a prerequisite silently added to this
chart change.

After approved Argo activation, verify two Ready Pods on distinct OMV/Wyse
hostnames, a matching PDB with one allowed disruption when both Pods are healthy,
one Available APIService with two ready backend endpoints, fresh node/Pod metrics
and Istiod HPA `ScalingActive`. Runtime failover and maintenance behavior remain
separate acceptance; rendered anti-affinity is not proof of an actual failover.

For a one-replica rollback, review the budget as well as the replica count.
Pruning is disabled: reverting the source does not remove the new PDB, and a
retained `minAvailable: 1` budget would block voluntary eviction of the sole Pod.
Do not re-enable the packaged K3s owner for a chart-only replica rollback.

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
render the real chart, check two replicas, required host separation, the native
PDB and bounded rollout strategy, per-Pod resource preservation, RBAC/Service/API
wiring, probes, security and intentionally misspelled placement/HA-value negative controls.
Bootstrap tests render every server config and retain the old-config refusal.
These are source/render tests, not proof of actual K3s retirement, Argo adoption,
three-node metric freshness, HPA behavior or production rollback.
