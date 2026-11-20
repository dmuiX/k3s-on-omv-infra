# K3s component metrics

K3s [shares a metrics registry](https://docs.k3s.io/reference/metrics) across embedded components. The [stack Application](../09-kube-prometheus-stack/app.yml) (chart 92.10.1) uses existing scrapes instead of dedicated controller-manager, scheduler and kube-proxy Services and ServiceMonitors. There are no new listeners or global `job` rewrites. Do not sum the apiserver and kubelet copies. Native etcd member metrics are out of scope.

- **Scheduler:** the chart's `kube-scheduler.rules` group stays in place. `jobNameOverride: apiserver` points its nine native recording rules (three latency histograms × p50/p90/p99) at one scrape job. Scrape health alone does not establish scheduler health.
- **Component alerts:** [`k3s-component-rules`](../09-kube-prometheus-stack/resources/k3s-component-rules.yaml) replaces dedicated-job alerts. It checks the `kube-system` leader leases for scheduler and controller-manager independently (missing or older than 120s), and warns if no `running_managed_controllers{job="apiserver",manager="kube-controller-manager"}` is positive. For proxy it warns on empty node inventory or any inventoried node missing `kubeproxy_sync_proxy_rules_last_timestamp_seconds{job="kubelet",metrics_path="/metrics"}`. This tests presence, not timestamp age. All five alerts use `for: 5m`; Alertmanager remains at zero replicas.
- **Grafana:** the scheduler, controller-manager and proxy dashboard ConfigMaps still render under their existing names. Helm `tpl` sets `grafana_dashboard="0"` on those three **in place**; the sidecar still selects `"1"`. This de-selection is intentional: panels designed for dedicated jobs would either lack data or, if pointed at a shared scrape, present K3s process health as component health. Keep the ConfigMaps, but do not serve those dashboards.

## Verify before retiring anything

After Argo sync, check **each Prometheus replica directly**, not through a load-balanced endpoint. Stop if either fails:

1. In Targets and Rules, verify healthy apiserver, kubelet and kube-state-metrics scrapes, one healthy `k3s.components` group and one `kube-scheduler.rules` group. Confirm the nine recordings produce samples where their input histograms have traffic.
2. Confirm both `kube_lease_renew_time` series advance and stay within 120s, and the controller-manager activity query above returns a positive sample.
3. Compare `kube_node_info{node!=""}` with the expected nodes (currently three); every node must have the proxy metric on kubelet `/metrics`, with the same `node` label. The monitor copies `__metrics_path__` to `metrics_path`; `node` comes from discovered kubelet endpoint targets, not a `pod` label. The offline render cannot verify live target labels. Partial inventory loss is not alerted on: if an expected node disappears from `kube_node_info` while another remains, the join no longer checks the missing node and the empty-inventory alert stays clear.

**Scoped retirement (`prune: false`):** only after both replicas pass, compare retained objects to the desired render. Remove only the following old stack-owned objects if still present; match kind, namespace and exact name, never a label or wildcard. Inspect each rule object's live groups before removal; stop if it also contains unrelated rules.

| Service in `kube-system` **and** ServiceMonitor of the same name in `kube-prometheus-stack` | Dedicated PrometheusRule in `kube-prometheus-stack` |
| --- | --- |
| `kube-prometheus-stack-kube-controller-manager` | `kube-prometheus-stack-kubernetes-system-controller-manager` |
| `kube-prometheus-stack-kube-scheduler` | `kube-prometheus-stack-kubernetes-system-scheduler` |
| `kube-prometheus-stack-kube-proxy` | `kube-prometheus-stack-kubernetes-system-kube-proxy` |

Leave mixed rule objects such as `kube-prometheus-stack-kubernetes-system` under chart management; do not delete them. Keep `kube-prometheus-stack-kube-scheduler.rules`. Recheck both replicas for absence of the retired dedicated-job alerts, duplicate rules or lost unrelated targets.

**Rollback:** revert the component changes in Git and sync. Confirm the previous Services, monitors, alert groups, scheduler expressions and dashboard labels are restored. Once both replicas load the prior rules, explicitly retire only the now-undesired `PrometheusRule/k3s-component-rules` in `kube-prometheus-stack`; pruning is still off.

## Embedded etcd

The chart's dedicated etcd integration is disabled: its Service selects static
etcd pods that do not exist here, and the Service has no endpoints. The matching
ServiceMonitor, native etcd rules and dashboard depend on that scrape and are no
longer rendered. This does not add per-member etcd monitoring or change the
existing K3s scrape jobs. The former custom member collector was reverted in #112.

Before retiring anything, confirm the Service still has no endpoints and an
instant query for `etcd_server_has_leader` returns no series on either Prometheus
replica. With automatic pruning disabled, verify the new render and review the
exact stack-owned retirement candidates: `kube-prometheus-stack-kube-etcd` (Service in
`kube-system` and ServiceMonitor in `kube-prometheus-stack`) and
`kube-prometheus-stack-etcd` (PrometheusRule and dashboard ConfigMap in
`kube-prometheus-stack`). Do not enable blanket pruning. After the scoped retirement,
verify the dedicated monitor and rule group are gone and the existing apiserver
and kubelet scrapes and rules remain healthy on both Prometheus replicas. Revert the Helm value and sync to restore the previous objects.

## Public test

From the repository root, use the public chart cache and synthetic values (no cluster or private values):

```sh
PATH="/path/to/promtool-3.15.0/bin:$PATH" \
PLATFORM_CHART_AUDIT=/path/to/pinned-chart-cache \
python -m unittest discover -s tests -p test_k3s_component_metrics.py
```
