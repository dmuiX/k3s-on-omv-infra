# OpenBao metrics: attended policy-first cutover

This is a human-run migration plan, not an automated cleanup hook. Changes are
source-checked and tested offline, **not validated against a running cluster**.
Do not enable the new listener by an unattended first sync of an existing cluster.
Do not print credentials, Secret data, token lookup responses or debug auth logs.

Infra #65 and its Grafana follow-up #66 do not introduce OpenBao metrics or
metrics-token resources. This PR (#71) adds the complete protected listener and
monitor directly to that metrics-free baseline. Do not deploy the old token
approach first. The legacy retirement steps below apply only if an earlier
experimental setup was actually deployed and its exact objects still exist.

## Native behavior and prerequisites

- OpenBao 2.7.1 `telemetry.disallow_metrics` rejects metrics on API 8200 (403).
  Listener 9101 uses `metrics_only` (other paths return 404) and
  `unauthenticated_metrics_access`; only GET metrics is served. The
  [middleware](https://github.com/openbao/openbao/blob/v2.7.1/internal/http/metrics.go)
  and [handler](https://github.com/openbao/openbao/blob/v2.7.1/internal/http/sys_metrics.go)
  enforce these restrictions natively. The latter calls the local metrics helper,
  not request forwarding: a standby does not redirect metrics to the leader.
- Chart 0.30.4's named extra container/Service ports and native ServiceMonitor
  port override are sufficient. Its default HA monitor selector selects only
  `openbao-active: "true"`; preserve that selector, service registration and
  leader changes. No custom ServiceMonitor, exporter, proxy or issuer is needed.
- Rendering kube-prometheus-stack 92.10.1 with Infra values and the isolated Live
  overlay yields Prometheus ServiceAccount `kube-prometheus-stack-prometheus`
  in namespace `kube-prometheus-stack`. Prometheus Operator 0.94.1 supplies the
  standard `app.kubernetes.io/name: prometheus` pod label via
  [makeSelectorLabels](https://github.com/prometheus-operator/prometheus-operator/blob/v0.94.1/pkg/prometheus/server/operator.go)
  and [StatefulSet label construction](https://github.com/prometheus-operator/prometheus-operator/blob/v0.94.1/pkg/prometheus/server/statefulset.go). Verify the actual
  workload has both properties before rollout. Do not substitute the operator,
  Grafana, default SA or a namespace-wide identity. Verify trust domain
  `cluster.local`, Ambient enrollment on both workloads, supported ztunnel 1.31.2
  and no waypoint/interception bypass for this port. Future waypoint insertion
  changes the identity seen at destination and requires a separate policy review.
- [Istio L4 policy](https://istio.io/latest/docs/ambient/usage/l4-policy/)
  supports `source.notPrincipals` and destination `ports`. The DENY rule matches
  port 9101 and any identity other than the exact Prometheus principal. In
  [ztunnel 1.31.2 matching](https://github.com/istio/ztunnel/blob/1.31.2/src/rbac.rs),
  an absent identity becomes an empty string and negative exact matching still
  succeeds: direct plaintext is denied, even from a correctly labeled pod.
  Do not add a positive `principals: ["*"]` condition: that would exclude missing
  identities from this denial. No HTTP attributes, JWT conditions or waypoint
  are used. This DENY does not impose a new ALLOW default on API/Raft ports.
- NetworkPolicy permits direct 9101 only from the monitoring namespace AND
  Prometheus pod label in the same peer. It preserves API 8200, Raft 8201 and
  shared HBONE 15008. HBONE can carry metrics from other admitted namespaces;
  only workload identity authorization closes that path. Host/admin access and
  local process compromise are not prevented by these pod-network policies.

## Staged deployment gates

1. Pause automatic synchronization of the affected root/child Applications and
   retain the old revision/configuration for rollback. Inventory existing metrics
   resources and ownership by kind/namespace/name, without reading their Secret
   data. If a legacy authenticated monitor/VSO setup actually exists, keep it
   functioning while installing protection and disable competing token writers.
   Otherwise retain the metrics-free baseline; create no legacy token resources.
2. Perform a **policy-only** stage: sync only
   `AuthorizationPolicy/openbao/openbao-metrics` from the new resource manifest,
   leaving the existing server/monitor configuration unchanged. It has
   `argocd.argoproj.io/sync-wave: "-30"`, before normal chart resources at wave 0.
   It is an ordinary persistent Sync resource, NOT a PreSync hook with
   hook-succeeded deletion. Application wave 4 orders children, not dataplane
   propagation. A wave delay or Argo Healthy status is not an enforcement proof;
   confirm the policy is accepted and distributed to every destination ztunnel.
   If existing hooks or custom health checks block this selective sync, resolve
   those explicitly without opening the listener or deleting the policy.
3. In an approved test environment, verify identity denial on the protected port
   before production exposure. Then sync chart configuration/ports/NetworkPolicy
   with the policy still installed. Chart update strategy is `OnDelete`: a config
   sync alone does not restart OpenBao. Roll one standby at a time using the
   normal approved HA maintenance procedure, waiting for unseal, Ready and Raft
   peer recovery; transition the active member last. Do not restart all replicas
   together, delete PVCs, or treat the PDB as protection against manual deletion.
   If there is an existing monitor, keep it on its old config until the native
   new endpoint is available; a staged monitor-only sync may avoid scrape gaps.
   For a first metrics installation, enable the monitor only after the endpoint
   and enforcement gates pass.
4. Switch the native ServiceMonitor to named port `metrics`, without any
   authorization/Secret reference. Accept only after all of these are verified:
   - both Prometheus replicas scrape the active Service on 9101 over Ambient;
     force a controlled leader transition and observe recovery/expected metrics;
   - another SA in the monitoring namespace and an existing API-client namespace
     cannot scrape through HBONE, including a pod spoofing the Prometheus label;
   - direct plaintext to pod/Service 9101 fails closed, including a source with
     the allowed namespace/label, while the real Prometheus identity succeeds;
   - API 8200 metrics returns 403 even with an otherwise authorized token;
     API/health/login/UI and Raft on their existing ports still work;
   - from the permitted identity, non-metrics 9101 paths (health, login, KV, UI)
     return 404; metrics GET works and non-GET does not;
   - no Gateway/HTTPRoute, NodePort, LoadBalancer, external IP or ingress exposes
     9101, and no additional policy bypasses direct-port restrictions.
   A failed security check blocks acceptance: close/revert 9101 while preserving
   the DENY policy and investigate. Never broaden principal/namespace access to
   make a scrape green.

## Conditional legacy retirement after acceptance

Skip these steps when no legacy token setup was deployed. Do not create old
Policies, roles, VSO resources, Secrets or tokens just to retire them. Neither
#65 nor #66 introduces that setup after the PR split.

Pruning is disabled. Removing manifests from Git does **not** remove live objects.
Do not bulk-prune the OpenBao Application or force-remove Crossplane finalizers.
The new AuthorizationPolicy deliberately reuses only the old metrics name, not
its kinds: never delete it when retiring the old resources below.

1. After confirming Prometheus no longer references the token Secret, retire only
   `VaultDynamicSecret/openbao/openbao-metrics` first and allow normal VSO
   finalization. Then retire its `VaultAuth`, dedicated `VaultConnection` and
   `ServiceAccount` of that exact namespace/name, checking no other consumer uses
   them. Do not touch shared VSO connections or the VSO controller. Inspect
   destination Secret owner metadata: garbage collection may remove the Secret,
   but if retained, an administrator must explicitly remove only
   `Secret/openbao/openbao-metrics` after all writers/readers are gone.
2. Verify existing cluster-scoped Crossplane `Policy/openbao-metrics` and
   `AuthBackendRole/openbao-metrics` have `spec.deletionPolicy: Orphan`, the
   expected external identities and provider reference. If a legacy object lacks
   Orphan, arrange that lifecycle setting before its deletion. Retire those two
   managed resources normally. Orphan lets Crossplane finalize without deleting
   the external ACL/login role; it does not revoke tokens or delete Secrets.
   No broad provider delete privilege or finalizer removal is justified.
3. Through a separately authorized administrator ceremony, inventory only the old
   metrics role, policy and outstanding token accessors without exposing token
   values. Disable/delete the exact external role `auth/kubernetes/role/openbao-metrics`
   after its reconciler is gone to stop new logins. Revoke any remaining metrics
   login tokens by verified accessor (or verify expiry after the old 24-hour
   maximum TTL, including actual mount/system limits and last possible login).
   Removing a role alone does not revoke issued tokens. Remove only the obsolete
   external `sys/policies/acl/openbao-metrics` after that review; retain all other
   policies/roles. VSO finalization is not proof every issued token was revoked.
4. If the live provider ACL still contains the two obsolete metrics management
   grants, apply the reviewed canonical Bootstrap ACL through its attended
   maintenance plan. Current Bootstrap #64 introduces Grafana grants only, so
   no metrics ACL update is needed for a fresh listener installation. An ACL
   update deletes no runtime objects and grants no cleanup/delete powers.
   Preserve Grafana grants and their separate pending-grant approval.
   Resume normal sync only after verifying ownership, target health and no stale
   metrics reconcilers remain. Record deployment validation separately.

For a first metrics installation, roll back to the metrics-free #65/#66
configuration through the controlled HA procedure; keep the DENY policy while
any pod still exposes 9101. Do not introduce the abandoned token design.
Only when a legacy authenticated setup existed may its retained monitor/config
be restored before retirement. After retirement, any restoration of its external
policy/role, VSO resources or provider grants requires separate review. Never
roll back by allowing broad unauthenticated access to the API listener.
