# PostgreSQL application credential rotation

Crunchy PGO generates and owns the `grafana` and `radar` passwords. OpenBao is
not their source of truth. Never print, decode, log, or manually transcribe a
PGO user Secret.

Generated source Secrets in namespace `postgresql` are:

```text
platform-postgres-pguser-grafana
platform-postgres-pguser-radar
```

Deleting one causes PGO to generate a new password, update the role verifier in
PostgreSQL, and recreate its connection Secret. This is a live mutation and can
interrupt the application. It requires explicit approval and a maintenance
window.

## Preconditions

- `PostgresCluster/platform-postgres` is reconciled with three Ready instances.
- Backup and WAL-archive health have been independently accepted.
- The application-specific GitOps release already provides automatic,
  least-privilege Secret replication into its namespace.
- The workload consumes the replicated Secret without embedding the value in a
  ConfigMap, Helm values, or Git.
- The application has a reviewed restart/reload path and rollback decision.

Kubernetes Secrets cannot be mounted across namespaces. Do not solve this by
granting an application read access to all Secrets in `postgresql`.

## Rotation

After fresh approval, rotate one application at a time:

```sh
kubectl -n postgresql delete secret platform-postgres-pguser-APPLICATION
```

Replace `APPLICATION` only with `grafana` or `radar`. Do not use a wildcard.

Then, without reading Secret contents:

1. Wait for PGO to recreate the source Secret and reconcile
   `status.usersRevision`.
2. Wait for the application-owned Secret replication to report the same source
   resource version or approved digest.
3. Restart or reload only the affected workload if its integration does not
   watch Secret updates.
4. Verify TLS database connectivity and application behavior.
5. Confirm that the old password no longer authenticates using a secret-safe
   automated check; do not place it on a command line or in shell history.
6. Record timestamps and resource versions, not credential values.

If propagation fails, stop. Do not paste the generated password into Git,
OpenBao, a ticket, or another Secret manually. Restore service through the
approved application rollback mechanism or escalate to a separately reviewed
role recovery procedure.

## R2 credentials

The pgBackRest R2 key is externally issued and remains an OpenBao-managed
secret. Rotate it separately at `kv/postgresql/r2-credentials`, verify webhook
materialization, restart/reconcile pgBackRest consumers as required, and keep
old R2 credentials valid until a full backup, WAL archive, and restore probe
using the replacement have been independently verified.

## Legacy OpenBao passwords

The former `kv/postgresql/grafana` and `kv/postgresql/radar` entries are not
used by PGO. Retain them through PGO and application acceptance. Their deletion
is a separate custody-approved cleanup, not part of password rotation.
