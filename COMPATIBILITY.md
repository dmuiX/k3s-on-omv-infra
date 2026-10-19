# Compatibility gate

Pinned in the reviewed manifests: cert-manager `v1.21.2`, K8up `4.10.0`, csi-driver-smb `1.20.3`, Longhorn `1.13.0`, Crossplane chart `2.4.2`, provider-vault `4.0.5`, OpenBao chart `0.30.4`, VSO `1.6.0`, CNPG `0.29.1`, Barman plugin `0.8.1`, prometheus-operator-crds `32.0.1`, kube-prometheus-stack `92.10.1`, Istio `1.31.2`, and Radar `1.17.0`.

Unresolved and deployment-blocking:

- Istio is pinned to the official `base`, `istiod`, `cni`, and `ztunnel` charts `1.31.2`; Gateway API uses istiod's native automated deployment and `infrastructure.parametersRef` overlays (no separate gateway chart). Local renders and Linux amd64/arm64 image-manifest inspection are static evidence only. The K3s `v1.37.1+k3s1` support-matrix gap is the explicit Homelab exception recorded by the architecture contract; runtime validation remains mandatory. Verify actual K3s/Flannel CNI paths, kernel/netfilter, ambient traffic, DNS, NetworkPolicy, gateway admission/readiness, three-node scheduling, and Argo child-Application health sequencing. See `docs/mesh-chart-evidence.md`.
- provider-vault `4.0.5` package digest, exact 16-type v2 activation-policy APIs, OpenBao behavior, and drift reconciliation.
- OpenBao snapshot and PostgreSQL retention adapter multi-arch image digests.
- The protected bootstrap ceremony now creates exactly `kv/platform/r2` and `kv/platform/smb`; deployment stays blocked until API and runtime validation confirms CAS-zero behavior and both VSO transformations without exposing values.
- Official csi-driver-smb 1.20.3 runtime compatibility with K3s, SMB dialect/security negotiation, retained dynamic subdirectory behavior, node packages, permissions, failure handling, and the verified mounted OMV backup disk.
- K8up 4.10.0/v2.16.0 native local backend and PodConfig are upstream-supported, but SMB CSI behavior, repository locking/pruning, permissions, independent password recovery, and isolated canary restore remain deployment-blocking. The repository-defined PostgreSQL/OpenBao/etcd OMV `rclone copy` units still require mounted-disk, checksum, retention, alerting, and restore evidence; see `docs/backup-contract.md`.
- Prometheus snapshot tool image digest, rendered replica-0 PVC identity, native admin-API reachability, same-node RWO attachment, one-shot Backup/Check/Snapshot status verification, exact success-gated cleanup mechanism, isolated restore/checksum, and 7-daily/4-weekly Prune behavior.
- Alertmanager `configSecret` schema plus the OpenBao/VSO `alertmanager.yaml` key and all SMTP values.
- Upstream oauth2-proxy multi-arch digest, OpenBao OIDC compatibility, callback/client contract, and Longhorn-only route isolation.
- Radar image architecture and exact chart values.
- OpenBao/Crossplane/VSO/cert-manager API schema compatibility, including explicit VSO transformations, rotation restarts, provider child-token exchange, every declared policy/auth-role endpoint, and OIDC key/scope/client/provider/group resources for Argo CD, Grafana, Longhorn, and Radar.
- OpenBao first-install behavior with `server.podManagementPolicy: Parallel`, three encrypted PVCs, initialization, Raft joining, automatic unseal, and quorum after restart.
- Longhorn first-install and upgrade behavior with the chart pre-upgrade hook disabled, an explicit supported pre-upgrade check, manager memory headroom, encrypted-volume attach/detach/reattach, and storage cleanup evidence.
- Network-policy behavior after the actual K3s CNI dataplane is installed: namespace selectors, API Service DNAT to port 6443, DNS, operator webhooks, Prometheus scraping, backup jobs, and all stateful clients.
- Verify protected `main` branch rules, reviewed merge history, repository credentials, and Git-revert rollback for Infra, Live, and Apps; force-pushes remain prohibited.
- Barman catalog/delete CLI behavior required by the retention contract and actual PostgreSQL/K8up/OpenBao/Prometheus restore execution rather than manifest-only checks.

No compatibility is claimed until milestone-2 evidence checks these items. Bootstrap reads `DEPLOYMENT-GATE.yml` and refuses to create the Infra root while the reviewed static gate is closed. `REQUIRED_*` resources are intentional fail-closed draft skeletons, not deployable defaults.
