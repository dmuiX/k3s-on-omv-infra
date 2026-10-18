# K3s on OMV — Infra

This repository is the publishable Infra ownership boundary for the canonical SPEC v6.1 GitOps architecture. It contains the Infra root, 21 unconditional platform child Applications, public values/resources, and cluster-free structural tests. Radar, Audiobookshelf, consumer resources, private cluster values, host configuration, and secrets belong to other repositories.

## Canonical phases

| Wave | Path | Applications |
|---:|---|---|
| 1 | `01-bootstrap` | `argocd-config`, `prometheus-operator-crds` |
| 2 | `02-controllers` | `cert-manager`, `k8up`, `longhorn`, `crossplane`, `vault-secrets-operator`, `cloudnative-pg`, `barman-cloud`, `istio-ambient` |
| 3 | `03-controllers` | `provider-vault` |
| 4 | `04-core` | `openbao` |
| 5 | `05-platform` | `provider-vault-connection` |
| 6 | `06-platform` | `openbao-config`, `openbao-pki`, `openbao-backups`, `letsencrypt-dns01` |
| 7 | `07-data` | `postgresql` |
| 8 | `08-observability` | `fluent-bit`, `kube-prometheus-stack`, `backup-validation` |

`redis-operator` is intentionally absent because no documented consumer justifies it. Every platform Application self-heals and has no automated prune. The dedicated Prometheus CRD chart is the only single-source upstream exception.

## Deployment blockers

[`deployment-blockers.json`](deployment-blockers.json) is the authoritative machine-readable fail-closed registry. The repository is not deployable while `deploymentAllowed` is false or any record has `blocksDeployment: true` and a non-resolved status. This prevents unresolved chart schemas, versions, private inputs, or adapter contracts from being represented as supported.

Check the gate explicitly:

```sh
python3 tests/deployment_gate.py  # exits 2 while blockers remain
```

While blockers are open, `infra.yml` references the deliberately nonexistent `infra-deployment-blocked` AppProject, so direct application cannot start child reconciliation. Resolve all evidence, set `deploymentAllowed` to `true`, and change the root project to `default` in the same reviewed change before Bootstrap applies the root.

## Offline structural tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

The tests use only the Python standard library and make no network or cluster connection. See [`tests/README.md`](tests/README.md) for scope and limitations.
