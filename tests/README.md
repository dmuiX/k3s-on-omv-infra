# Offline checks

Run from the infra repository:

```sh
ruby tests/verify-bootstrap.rb   # public base chart pins, source paths, waves, values, storage constraints
ruby tests/verify-ingress.rb     # no live-specific resources selected by the public root
ruby tests/verify-charts.rb      # six pinned charts + complete public resource templates
```

The first two need only Ruby and this repository; they do not require the
private overlay, a kubeconfig or a sibling bootstrap checkout. The chart render
needs Helm and network access to the pinned chart repositories. It renders the
public `values.yml` files plus `charts/cluster-config/` using safe defaults, in
an isolated temporary Helm cache. Chart-generated
Secrets and diagnostics remain in memory and are not printed. Rendering does
not prove installation, ingress TLS, storage health or backup recovery.

The **same public root** owns the complete rendered deployment; only the value
set is private. With the private values checkout, run
`ruby tests/verify-private-values.rb` here (also requires Helm and the host
Traefik configuration). It verifies routes/certificate/backups against the
real values without printing private identifiers or Secret data.

Before any live sync, separately review: initial Argo CD read-only Git access
(the private repo cannot bootstrap its own credential through OpenBao), Argo CD
child-Application health customization, host storage prerequisites, disposable
Longhorn PVC provisioning/write/read/reattach, OpenBao unseal and recovery,
Certificate Ready, Gateway/route status, independent backup/restore and the
Argo automated prune/self-heal settings. Cluster-mutating tests need separate
approval and a cleanup plan.
