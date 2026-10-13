# Radar

Radar runs as one in-cluster replica from the pinned upstream Helm chart. Argo CD
owns the workload, route, restrictive network policy, read-only service-account
role, OpenBao OIDC resources, and namespace-local secret delivery.

## Authentication

External access is available only through the `websecure` Gateway listener.
The private route values supply its externally published port so the OpenBao
issuer metadata and Radar callback use the same browser-reachable authority.
OpenBao is the OIDC issuer. Crossplane writes the generated confidential-client
secret to `Secret/radar-oidc-client`; the generated client ID and private route
identifiers are kept in the Live values repository. The `radar-groups` scope
adds the `radar:view` group, which is bound to Kubernetes' built-in read-only
`view` ClusterRole.

`Secret/radar-auth` is generated during its first Helm render and retained.
Argo ignores only its session-signing data field so later renders do not replace
active sessions. Neither the session key nor OIDC client secret belongs in Git.

Radar's service account deliberately has no Secret, RBAC-object, webhook, exec,
port-forward, Helm-write, or workload-write access. User operations are checked
through Kubernetes impersonation and SubjectAccessReview. Expanding permissions
requires a separate reviewed RBAC change.

## PostgreSQL timeline

VSO reads OpenBao's `database/static-creds/radar` role and emits only a PostgreSQL
DSN in `Secret/radar-db-credentials`. Radar uses the central service:

```text
k3s-postgres-rw.cloudnative-pg.svc:5432
```

The timeline is configured fail-closed with PostgreSQL and 30-day retention. VSO
restarts the Deployment after credential rotation. The `radar` database is part
of the central CloudNativePG backup and restore boundary; this Application does
not operate a separate database backup.

Radar intentionally remains single-replica because its informer caches, streams,
and interactive sessions are process-local. Its PDB permits voluntary disruption
rather than claiming application HA. Timeline persistence does not make the
Radar workload highly available.
