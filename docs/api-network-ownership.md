# Deployment-specific API network ownership candidate

This is an offline publication candidate, not a deployed or runtime-verified restriction. No cluster or secret reads were performed. Exact addresses are copied from the existing local deployment policies, not rediscovered or verified against current endpoints.

## Ownership and discovery

Live owns Service `10.43.0.1/32` TCP 443 separately from K3s endpoint `192.168.1.2/32`, `192.168.1.5/32`, `192.168.1.7/32` TCP 6443. Both tuples cover policy enforcement before/after Service DNAT; neither grants the other port to the other addresses. Update these lists with Service/EndpointSlice changes after separately authorized verification.

- `infra/openbao/api-network-policy.yaml`: injector API egress and endpoint webhook ingress on TCP 8080. Infra keeps DNS and Ambient probe source `169.254.7.127/32`.
- `infra/openbao/server-api-network-policy.yaml`: server-only API access. The native server policy retains its published selector and all rules except the ports-only TCP 6443 rule. The additive policy selects the actual server labels, including `component: server`.
- `infra/k3s-postgres/api-network-policy.yaml`: only `cnpg.io/cluster: k3s-postgres` instances. Existing same-namespace/database-client ingress, Barman, DNS, namespace ownership and workload identities are unchanged. The credential provisioning helper is not a database instance and does not receive this grant; its existing OpenBao/DNS-only policy is preserved.
- `infra/backup-validation/api-network-policy.yaml`: namespace-wide helper API access, preserving the prior namespace-wide scope. Internal OpenBao/PostgreSQL/DNS and metrics ingress remain unchanged.
- Radar uses its existing `charts/workload-network` source and Live `apps/radar/network-values.yml`, not a new raw Live file. The chart adds only optional policy annotations so `allow-radar-credentials-api` can precede the helper Sync hook (wave -2 before -1). The workload keeps its published selector and non-API rules.

PostgreSQL and backup-validation already have raw Live directory sources. OpenBao's previously values-only `ref: live` now also has `path: infra/openbao` and `directory.exclude: '*values.y*ml'`: the three policy files are discovered, while both existing values files remain excluded. The route still uses `charts/service-route` and `route-values.yml`; the legacy `values.yml` remains untouched. There is no Kustomization in these directories hiding new files. Focused tests check this wiring; Argo runtime discovery remains unverified.

## Preserved broad HTTPS is not a restriction

Live `infra/openbao/external-https-network-policy.yaml` retains snapshot-job TCP 443 to **every destination**, using the existing snapshot selector. Live `infra/backup-validation/external-https-network-policy.yaml` does the same for the existing namespace-wide scope. These are baseline-preservation exceptions needed by the existing private external HTTPS dependencies, not new grants or an assertion that all such traffic is required forever. They are not R2-only controls. No endpoint migration, FQDN enforcement, proxy, egress gateway, or new networking infrastructure is introduced.

NetworkPolicies are additive: these HTTPS rules also allow API Service TCP 443. Radar's existing public Helm HTTPS rule likewise remains broad. Exact API policies do not override any of those grants.

**Paired Apps + Live rollout prerequisite:** the sibling Apps candidate removes only the ports-only TCP 443/6443 rule from `radar/resources/provision-credentials.yaml`. DNS, OpenBao, Argo namespace permissions and helper identity are unchanged. This removal requires Live `apps/radar/network-values.yml` and Infra chart annotation support to be published and rendered through Radar's actual `charts/workload-network` source first. The exact `allow-radar-credentials-api` policy must exist at wave -2 before the helper Sync hook at wave -1. Do not publish/activate the Apps removal alone: an older Live revision would leave the helper without API access. Until the Apps removal reconciles, the additive exact policy does not restrict the old broad grant. Offline Apps contracts render that source and check the exact Service/endpoints tuples, hook ordering and preserved internal rules; they do not prove cluster connectivity.

## Publication and rollout

1. Publish the Infra chart annotation support before publishing Radar Live values that use it. Do not activate the Infra policy removals yet.
2. Publish Live additive policies and coordinated Radar values. PostgreSQL/backup-validation can discover policies through existing sources. OpenBao cannot discover its new policies until the Infra source-path change is activated; do not claim Live publication alone makes them effective.
3. In an attended, separately approved rollout, ensure the additive policies exist before removing old permissions. Stage OpenBao source discovery before native server/injector/snapshot removals if necessary; policy resources in one Application are not a connectivity-safe transaction. Review Argo diffs, target labels, DNS, API/TokenReview, webhook/probes, snapshots, backups/retention and restore helpers. No live operations are included here.
4. Coordinate the paired Apps helper cutover above before claiming helper API destination restriction. Radar uses `prune: false`: the existing `radar-credentials` policy keeps its identity and is updated in place, so removing its egress rule does not require pruning. However, renamed/removed policy resources can remain stale and additive; inspect them before claiming restriction. Review any retained old OpenBao server policy: `prune: false` leaves `allow-openbao-raft-and-clients` until explicit approved retirement, and it can retain broad TCP 6443. Do not auto-prune or delete policies in this topic.

Block deployment on missing published revisions/source discovery, unverified endpoint inventory, or failed connectivity/admission checks. Other existing `REQUIRED_*` and compatibility gates remain. Rollback restores prior Infra rules and the Apps helper API allowance before removing additive Live policies or discovery; preserve broad HTTPS during rollback too. Namespace owners, route default names, metrics/listener/connection configuration, upstream fixes and R2 endpoints are outside this topic.

## Offline checks

Inspect `tests/run-offline-tests.sh` before running; the full runner includes unrelated Bootstrap and Ansible syntax checks. Focused checks use the installed Ansible Python (PyYAML), cached exact OpenBao chart `0.30.4` under `/tmp/platform-chart-audit`, Helm and Ruby. Run Python unittest discovery for `*api_network.py`, `test_openbao_network.py`, and `test_injector_network_policy.py`; run `ruby -EUTF-8 charts/workload-network/tests/test_render.rb` and Helm lint for the actual Radar Live values. Historical OpenBao and Radar fixtures stay immutable; tests model only explicit approved deltas. From Apps, run `PYTHONPATH=tests python -B -m unittest test_radar_raw_source test_radar_api_network` with the existing PyYAML-capable Python; these checks need only the sibling Infra/Live source checkouts and Helm, not a real cluster or network.
