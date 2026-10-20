# Istio chart evidence

Static upstream review and local renders: 2026-10-18. No hosts, Kubernetes API, deployment, or runtime tests were used. This resolves chart inputs but does not replace automatic runtime validation. Host and workload logging are independent infrastructure and are outside this repository.

## Official mechanisms and pins

- [Istio 1.31 ambient Helm installation](https://istio.io/v1.31/docs/ambient/install/helm/) requires separate `base`, `istiod` (`profile: ambient`), `cni` (`profile: ambient`), and `ztunnel` charts. All four are pinned to **1.31.2** from `https://blob.istio.io/istio-release/charts`; the official index metadata agrees. There is no aggregate ambient chart.
- One `istio-core` child Application groups the four official chart sources as independent Helm render sources, preserving their previous release names; the `istio-ambient` child Application (wave 2) owns Gateway configuration and Live's Gateway. This reduces the Istio footprint to two Applications and keeps only `istio-system` and `istio-gateway`. Argo CD does **not** wait for each chart to become healthy before applying the next chart within one multi-source Application; all components must converge, and first-install/upgrade sequencing requires runtime validation. `istio-system` remains privileged for CNI/ztunnel; `istio-gateway` remains restricted with an explicit `RuntimeDefault` seccomp profile in the generated Gateway pod.
- [K3s prerequisites](https://istio.io/v1.31/docs/ambient/install/platform-prerequisites/#k3s) support bundled CNIs via `global.platform: k3s`. The selected chart renders `/var/lib/rancher/k3s/data/cni` for binaries and `/var/lib/rancher/k3s/agent/etc/cni/net.d` for configuration. CNI and ztunnel share `/var/run/ztunnel`.
- [Gateway API automated deployment](https://istio.io/v1.31/docs/tasks/traffic-management/ingress/gateway-api/#automated-deployment) makes a separate `gateway` Helm release unnecessary. Live's Gateway references Infra's same-namespace ConfigMap through `spec.infrastructure.parametersRef`. Its overlays request three replicas, preferred hostname anti-affinity, best-effort topology spread, Pi toleration, a `RuntimeDefault` seccomp profile, resource bounds, and minimum two available. This avoids hard scheduling constraints and missing seccomp being barriers to ingress pod readiness. Admission, probes, Gateway status, TLS, and endpoints still require runtime evidence.
- `base.validationFailurePolicy: Fail` is set for both base and istiod to avoid the upstream-documented server-side-apply ownership conflict. Gateway API CRDs remain externally owned.

## Artifact checks

Official chart index SHA-256 values:

| Chart | Version | SHA-256 |
| --- | --- | --- |
| base | 1.31.2 | `bf1e4bfdc1604603eec48ddd83877eb02aa2b929f90b12105f3f326203661abd` |
| istiod | 1.31.2 | `478af0013f3bf45e0e3072d122413276f8c387cae9ef2c130c45df7e781a613d` |
| cni | 1.31.2 | `61ebab3041d12fb127284da82cbf86dcc7e5b6246c3f1f64a9a1d520a825dc2c` |
| ztunnel | 1.31.2 | `683c7623f0ce650687e16d1ab481dee605a1158ac86e98732bd8cd21ec0bd2e9` |

The chart checksums come from the official 1.31.2 index. Architecture availability remains a runtime check; index and render evidence alone do not prove mixed-node compatibility.

## Offline reproduction

Place extracted official `base`, `istiod`, `cni`, and `ztunnel` chart directories in a local cache:

```sh
CHART_CACHE_DIR=/absolute/local/chart-cache ruby tests/verify-mesh-renders.rb
ruby tests/verify-contracts.rb
```

The test never downloads dependencies or contacts Kubernetes. Without a cache it explicitly skips.

## Transition from the previous five Applications

Argo CD automatic pruning is disabled. After merging this change, the old `istio-base`, `istio-istiod`, `istio-cni`, and `istio-ztunnel` Application objects remain even though Git stops declaring them. They must be deleted **without cascading Kubernetes resource deletion**, allowing `istio-core` to adopt their rendered resources. Do not use foreground/cascading deletion, which could remove CRDs, control plane and networking components. Verify that `istio-core` is Synced/Healthy and the Gateway Deployment has ready endpoints; chart grouping and seccomp changes cannot be verified from Git alone. The private Live Gateway must remain present.

## Remaining deployment blockers

1. Bootstrap selects `v1.37.1+k3s1`. The remaining upstream support-matrix gap for the pinned controller set is the explicitly accepted Homelab exception, but it still requires successful local API, admission, networking, restart, and upgrade evidence before the gate may open.
2. Verify real K3s/Flannel paths, kernel/netfilter, amd64/arm64 behavior, ambient interception, DNS, mTLS, NetworkPolicies and restart/upgrade behavior.
3. Verify Gateway API CRDs, gateway customization/admission, three eligible nodes, listener/TLS routing and LAN/VPN-only exposure. Verify the preinstalled Argo child-Application health customization during first installation.
4. Reviewed repository URLs/revisions and the deployment evidence gate remain unresolved.
