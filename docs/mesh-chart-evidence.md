# Istio chart evidence

Chart pins and local render checks updated 2026-11-21. No hosts, Kubernetes API, deployment, or runtime tests were used. This resolves chart inputs but does not replace automatic runtime validation. Host and workload logging are independent infrastructure and are outside this repository.

## Official mechanisms and pins

- [Istio 1.32 ambient Helm installation](https://istio.io/v1.32/docs/ambient/install/helm/) requires separate `base`, `istiod` (`profile: ambient`), `cni` (`profile: ambient`), and `ztunnel` charts. All four are pinned to **1.32.0** from `https://blob.istio.io/istio-release/charts`; the official index metadata agrees. There is no aggregate ambient chart.
- One `istio-core` child Application groups the four official chart sources as independent Helm render sources, preserving their previous release names; the `istio-ambient` child Application (wave 2) owns Gateway configuration and Live's Gateway. This reduces the Istio footprint to two Applications and keeps only `istio-system` and `istio-gateway`. Argo CD does **not** wait for each chart to become healthy before applying the next chart within one multi-source Application; all components must converge, and first-install/upgrade sequencing requires runtime validation. `istio-system` remains privileged for CNI/ztunnel; `istio-gateway` remains restricted with an explicit `RuntimeDefault` seccomp profile in the generated Gateway pod.
- [K3s prerequisites](https://istio.io/v1.32/docs/ambient/install/platform-prerequisites/#k3s) support bundled CNIs via `global.platform: k3s`. The selected chart renders `/var/lib/rancher/k3s/data/cni` for binaries and `/var/lib/rancher/k3s/agent/etc/cni/net.d` for configuration. CNI and ztunnel share `/var/run/ztunnel`.
- [Gateway API automated deployment](https://istio.io/v1.32/docs/tasks/traffic-management/ingress/gateway-api/#automated-deployment) makes a separate `gateway` Helm release unnecessary. Live's Gateway references Infra's same-namespace ConfigMap through `spec.infrastructure.parametersRef`. Its overlays request three replicas, preferred hostname anti-affinity, hard hostname spread (`maxSkew: 2`, `minDomains: 2`), Pi toleration, a `RuntimeDefault` seccomp profile, resource bounds, and a PDB with `minAvailable: 2`. Three replicas may run 2+1 across two hosts; if only one host can fit them, the third stays Pending. Do not overlap the rollout with a node drain. The PDB limits voluntary evictions; losing the host carrying two pods can leave just one serving. Admission, probes, Gateway status, TLS, and endpoints still require runtime evidence.
- `base.validationFailurePolicy: Fail` is set for both base and istiod to avoid the upstream-documented server-side-apply ownership conflict. Gateway API CRDs remain externally owned.

## Artifact checks

Official chart index SHA-256 values:

| Chart | Version | SHA-256 |
| --- | --- | --- |
| base | 1.32.0 | `505ce0d9b6cf5d73a9998728fdf0821510a237470567e6c332164937c6850f05` |
| istiod | 1.32.0 | `17d275e3661e2e943a1ab630386bacab0cda76ea007978351a3a10fe9f4050d4` |
| cni | 1.32.0 | `8fa98238dd8dbf11ac1ebd00e27af9874f83ff40b54d2732c23941e251881c9a` |
| ztunnel | 1.32.0 | `1ee5cc123b8f13d668b79f1cb25ef052a30fad1c772e4d000607b931b5cc9d93` |

The chart checksums come from the official 1.32.0 index. Architecture availability remains a runtime check; index and render evidence alone do not prove mixed-node compatibility.

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
3. Verify Gateway API CRDs, gateway customization/admission, three Ready gateway pods across at least two hostnames, listener/TLS routing and LAN/VPN-only exposure. Verify the preinstalled Argo child-Application health customization during first installation.
4. Reviewed repository URLs/revisions and the deployment evidence gate remain unresolved.
