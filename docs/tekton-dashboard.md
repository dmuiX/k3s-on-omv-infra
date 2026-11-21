# Tekton Dashboard

The LAN/VPN Istio HTTPS listener sends all requests for the Tekton host to a
dedicated oauth2-proxy. The [same chart](../charts/oidc-proxy/) already serves
Longhorn; the pods, OIDC client, cookie and credentials are separate. Only the
proxy Service has an HTTPRoute. Neither the dashboard nor its Kubernetes API
proxy is reachable through another route. NetworkPolicy limits the dashboard
pod ingress to its oauth2-proxy and egress to the Kubernetes API peers in Live.

The dashboard is pinned to [v0.74.0](https://github.com/tektoncd/dashboard/tree/dac0320946ed1905e97587d5bff683c1de170352),
which supports the installed Tekton Pipelines v1.18.x LTS. It shows pipelines,
runs and logs from `ci-builds`. The UI offers PipelineRun creation. The
Application syncs the build and its admission policy before a separate GitOps
change grants `create` on `pipelineruns.tekton.dev` only in `ci-builds`. The grant does not allow updates, cancellations, deletion,
other resource writes, secret reads or cross-namespace logs. The Dashboard uses
one Kubernetes identity for all authenticated users; it does not grant
impersonation or forward browser-supplied Kubernetes authorization. The Pipelines
Application owns `ci-builds`.

OpenBao assigns the confidential client only to the existing `radar-admin`
group (external name `radar:admin`). The proxy independently enforces the group
claim. The browser uses the public OpenBao issuer and the registered HTTPS
callback. Token redemption and JWKS use `openbao-active` over Istio Ambient
mTLS, without bypassing issuer verification. The bounded provisioner uses the
shared create-once custody helper to copy the native client into
`kv/tekton-dashboard/oidc-proxy` and initialize a separate cookie key. VSO
syncs only the three expected fields. A rerun validates, not overwrites, existing
custody. Mismatched credentials require an attended rotation; do not delete the
KV entry to recover a failed rollout.

## Rollout and verification

1. Merge the associated Live and Bootstrap changes before Infra. On an existing
   cluster, separately approve and apply the Tekton-specific ACL paths to the
   provider using Bootstrap's attended OpenBao provider-access maintenance
   procedure. A Bootstrap merge alone does not update the running ACL. Review
   the full ACL and role diff and stop if it includes unrelated unapproved
   changes. Verify the site DNS, wildcard TLS, Gateway listener, current API
   EndpointSlices and CoreDNS Service IP against Live, healthy Pipelines and
   OpenBao/VSO/provider-vault, and the existing OpenBao group. The Infra root
   auto-syncs after an approved merge.
2. Require the `tekton-dashboard` Application to be Synced/Healthy. Confirm the
   route is Accepted and ResolvedRefs, the proxy and dashboard are ready, and
   VSO reports a valid delivery without inspecting any Secret contents. The
   OIDC discovery document should advertise the same issuer and authorize path
   used by the proxy. Confirm the gateway still serves Longhorn unchanged.
3. From LAN/VPN verify an anonymous browser is directed to OpenBao for both
   the UI and an API path, a nonmember cannot sign in, and a `radar:admin` can see
   an existing run and stream its logs. Sign out and confirm the local session
   no longer grants access. Verify an unrelated workload cannot reach the
   internal dashboard Service or the proxy directly. Before installing the run
   grant, verify that the Dashboard ServiceAccount cannot create PipelineRuns,
   read Secrets or read Pod logs outside `ci-builds`. No new run is needed to
   validate login.

## Enable Envoy runs

Follow the [Envoy Dashboard runbook](../01-bootstrap/istio-gateway/build/dashboard/README.md).
Argo CD owns the build and guard; verify them before merging the separate
GitOps write grant. No further Bootstrap or OpenBao changes are needed. Use
Tekton API v1 and the reviewed payload in the Dashboard YAML editor, not the
simple form. The UI flag is not a security boundary.

## Rollback

Block Dashboard traffic and revoke the Envoy run RoleBinding through GitOps
first; verify that run creation is denied before removing the admission guard. To
retire the Dashboard, remove its route, verify the host no longer serves it,
then retire the Application and its owned resources. A Git revert alone does
not remove the route because automated pruning is disabled. Keep `ci-builds`,
Pipelines, Longhorn and any existing runs intact. Crossplane resources are
Orphan by design: review the Tekton-only OpenBao client/assignment, access roles
and KV retention separately. No rollback or live sync is performed by tests.
