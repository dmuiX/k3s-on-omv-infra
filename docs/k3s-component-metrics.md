# K3s component metrics

K3s [shares a metrics registry](https://docs.k3s.io/reference/metrics) across embedded components. The [stack Application](../09-kube-prometheus-stack/app.yml) (chart 92.10.1) uses existing scrapes instead of dedicated controller-manager, scheduler and kube-proxy Services and ServiceMonitors. There are no new listeners or global `job` rewrites. Do not sum the apiserver and kubelet copies. Native etcd member metrics are out of scope.

- **Scheduler:** the chart's `kube-scheduler.rules` group stays in place. `jobNameOverride: apiserver` points its nine native recording rules (three latency histograms × p50/p90/p99) at one scrape job. Scrape health alone does not establish scheduler health.
- **Component alerts:** [`k3s-component-rules`](../09-kube-prometheus-stack/resources/k3s-component-rules.yaml) replaces dedicated-job alerts. It checks the `kube-system` leader leases for scheduler and controller-manager independently (missing or older than 120s), and warns if no `running_managed_controllers{job="apiserver",manager="kube-controller-manager"}` is positive. For proxy it warns on empty node inventory or any inventoried node missing `kubeproxy_sync_proxy_rules_last_timestamp_seconds{job="kubelet",metrics_path="/metrics"}`. This tests presence, not timestamp age. All five alerts use `for: 5m`; Alertmanager remains at zero replicas.
- **Grafana:** the scheduler, controller-manager and proxy dashboard ConfigMaps still render under their existing names. Helm `tpl` sets `grafana_dashboard="0"` on those three **in place**; the sidecar still selects `"1"`. Do not delete the ConfigMaps. In particular, scheduler process panels would describe the shared K3s process, not a standalone scheduler.

## Verify before retiring anything

After Argo sync, check **each Prometheus replica directly**, not through a load-balanced endpoint. Stop if either fails:

1. In Targets and Rules, verify healthy apiserver, kubelet and kube-state-metrics scrapes, one healthy `k3s.components` group and one `kube-scheduler.rules` group. Confirm the nine recordings produce samples where their input histograms have traffic.
2. Confirm both `kube_lease_renew_time` series advance and stay within 120s, and the controller-manager activity query above returns a positive sample.
3. Compare `kube_node_info{node!=""}` with the expected nodes (currently three); every node must have the proxy metric on kubelet `/metrics`, with the same `node` label. An empty missing-node result is not sufficient: complete inventory loss has its own alert, and a node removed from a partial inventory cannot be checked by this join.

**Scoped retirement (`prune: false`):** only after both replicas pass, compare retained objects to the desired render. Remove only the following old stack-owned objects if still present; match kind, namespace and exact name, never a label or wildcard. Inspect each rule object's live groups before removal; stop if it also contains unrelated rules.

| Service in `kube-system` **and** ServiceMonitor of the same name in `kube-prometheus-stack` | Dedicated PrometheusRule in `kube-prometheus-stack` |
| --- | --- |
| `kube-prometheus-stack-kube-controller-manager` | `kube-prometheus-stack-kubernetes-system-controller-manager` |
| `kube-prometheus-stack-kube-scheduler` | `kube-prometheus-stack-kubernetes-system-scheduler` |
| `kube-prometheus-stack-kube-proxy` | `kube-prometheus-stack-kubernetes-system-kube-proxy` |

Leave mixed rule objects such as `kube-prometheus-stack-kubernetes-system` under chart management; do not delete them. Keep `kube-prometheus-stack-kube-scheduler.rules`. Recheck both replicas for absence of the retired dedicated-job alerts, duplicate rules or lost unrelated targets.

**Rollback:** revert the component changes in Git and sync. Confirm the previous Services, monitors, alert groups, scheduler expressions and dashboard labels are restored. Once both replicas load the prior rules, explicitly retire only the now-undesired `PrometheusRule/k3s-component-rules` in `kube-prometheus-stack`; pruning is still off.

## Public test

From the repository root, use the public chart cache and synthetic values (no cluster or private values):

```sh
PATH="/path/to/promtool-3.15.0/bin:$PATH" \
PLATFORM_CHART_AUDIT=/path/to/pinned-chart-cache \
python -m unittest discover -s tests -p test_k3s_component_metrics.py
```
