# Grafana administrator credential custody

## Ownership and limitations

`kube-prometheus-stack` owns the create-once `grafana-admin-provisioner` Sync
hook and ServiceAccount. Its Policy, AuthBackendRole, ConfigMap, NetworkPolicy,
labels, script login, volume references and `vault://grafana-admin-provisioner`
audience all use the same producer name.
It uses the existing shared custody helper, audience-bound Kubernetes login,
CAS-0 writes and a read/create-only ACL for `kv/data/monitoring/grafana-admin`.
The fixed login is `admin`; the password is generated in memory. Repeated syncs
reuse the owned value. Foreign, malformed or tombstoned entries fail closed;
never delete history to force regeneration. The hook has no Kubernetes API RBAC,
PostgreSQL access or Grafana reset capability.

VSO reads that path through VaultAuth/OpenBao role `grafana-vso`, reusing the
chart-created ServiceAccount `kube-prometheus-stack-grafana`. There is no separate
`grafana-vso` ServiceAccount. The reader ACL remains limited to the existing
Grafana database/admin paths and token renewal; Grafana's workload identity now
also has that direct OpenBao read authority. The `grafana-admin-provisioner` Job keeps
its separate ServiceAccount, audience-bound create permission and Job-specific
NetworkPolicy; Grafana's native policy selects Grafana pods, not that Job.
VSO owns **only** Secret `grafana-admin` (keys `admin-user`, `admin-password`,
no raw payload). The chart's native
`grafana.admin.existingSecret` connects both Grafana and its reload sidecars to
this Secret. It does not adopt the old Argo/chart-owned
`kube-prometheus-stack-grafana` Secret. `grafana-db` remains independent: it is the
PostgreSQL service login, not the Grafana UI administrator.

**Grafana's admin environment variables initialize an empty database; they do
not change a password already stored in PostgreSQL.** Two replicas share that
persistent database. A rollout alone cannot repair the existing mismatch and
may leave sidecars returning 401 until the one-time alignment below. Future
password changes require an attended KV/DB alignment and pod rollout as well;
there is no continuous reset controller or automatic restart target here.

Reader resources (VaultAuth, Secret delivery, reader ACL and login role) are
colocated in `resources/grafana.yaml`. Producer resources stay together in
`resources/provision-credentials.yaml`; mixing their Kubernetes kinds is
intentional because they implement one bounded producer. Shared transport is
handled separately by Infra #70, not duplicated here.

If the previous Job/ServiceAccount name was deployed, stop or await any active
old producer before an approved renamed-hook sync. Pruning is disabled: old
Jobs/accounts can remain after rename and require separate retirement. Keep
existing policies, roles, KV history and Secrets; never recreate credentials to
clean up names. The new account remains distinct from the normal Grafana reader. Bootstrap #64
must authorize the canonical management paths before producer reconciliation.
If a partial earlier draft was deployed, inventory its old producer resources
and outstanding tokens, pause competing reconcilers, and retire old Crossplane
Policy/Role objects through their normal Orphan lifecycle while their existing
management grants still work. Do not force finalizers or delete KV history.
Changing management grants does not revoke old login tokens. After the canonical
producer is accepted, separately authorize retirement of obsolete external
policies/roles, Kubernetes helpers and old RBAC, preserving all credential data.

Native capabilities checked before implementation:

- The exact cached kube-prometheus-stack 92.10.1 archive's Grafana
  `values.yaml`, `templates/secret.yaml` and `templates/_pod.tpl` support
  `admin.existingSecret`, `userKey`, `passwordKey`; they suppress the generated
  admin Secret and use the same references for Grafana and reload sidecars.
  [Chart release](https://github.com/prometheus-community/helm-charts/releases/tag/kube-prometheus-stack-92.10.1).
- Bundled Grafana 14.0.1 `templates/serviceaccount.yaml` and `_pod.tpl` already
  create and use the workload ServiceAccount. Exact rendering with the Live
  overlay verifies its name against both VaultAuth and the OpenBao role binding.
  `templates/networkpolicy.yaml` fixes its selector to Grafana, so it cannot
  replace the provisioning Job's separate policy. No `extraObjects` wrapper is
  introduced.
- [VSO 1.6.0 VaultStaticSecret schema](https://github.com/hashicorp/vault-secrets-operator/blob/v1.6.0/api/v1beta1/vaultstaticsecret_types.go)
  provides KV-v2 delivery; native transformation filters retain only admin keys.
- [Grafana 13.2.4 reset implementation](https://github.com/grafana/grafana/blob/v13.2.4/pkg/cmd/grafana-cli/commands/reset_password_command.go)
  reads one line from stdin with `--password-from-stdin`, defaults to user ID 1,
  and refuses non-admin accounts. It may report a missing admin without a nonzero
  exit, so verify the success message and authenticated login, not just exit code.

## Attended rollout (not executed by this change)

Every live mutation requires separate approval. This draft is stacked on the
platform adoption draft; it does not authorize that adoption or bypass its gates.
Do not run the full bootstrap or change unrelated in-progress migrations for
this repair. Provider access maintenance is a separate admin/MFA playbook;
review and approve its exact versioned ACL changes before execution.

1. Review/publish only this scoped change through the normal process. Before the
   monitoring sync, an authorized OpenBao operator must update the existing
   Crossplane provider policy from Bootstrap's
   `ansible/roles/openbao_setup/files/crossplane-provider-vault.hcl`, preserving
   its other entries. The only additions are create/read/update on
   `sys/policies/acl/grafana-admin-provisioner` and
   `auth/kubernetes/role/grafana-admin-provisioner`; no KV value permission is added to
   the provider. Verify its current token can manage these two exact objects.
2. Confirm a current PostgreSQL backup and the intended cluster/context. Check
   that the existing admin login is `admin` and its numeric user ID is 1 through
   an authorized account or a narrowly reviewed, non-secret DB query. If not,
   stop and adapt the fixed identity/user ID; do not create a second admin.
3. Sync `kube-prometheus-stack` normally. Helper and producer prerequisites are
   wave -2, create-once hook -1, and native chart/VSO consumers wave 0. Require
   Ready/Synced producer policy/login role, successful hook, VSO Ready and a
   VSO ownerReference on `grafana-admin`. Inspect metadata/conditions only, never
   `kubectl get secret -o yaml`, decoded data, pod environment or debug logs.
   Confirm the chart render has no admin Secret and both replicas/sidecars refer
   to `grafana-admin`. Existing prune-disabled policy retains the old chart
   Secret; leave it unused, not adopted or overwritten. If the old `grafana-vso`
   ServiceAccount was deployed, pruning also retains it: require successful fresh
   VSO authentication as the chart account, not just Ready from a cached old
   token. Verify both Grafana Secret deliveries (`grafana-admin` and `grafana-db`)
   before separately approving its retirement. Do not delete the
   VaultAuth or OpenBao role still named `grafana-vso`. Existing reader tokens
   are not revoked by changing the role's allowed ServiceAccount; account for
   their expiry/revocation during that attended retirement.
4. An authorized human may use the authenticated OpenBao UI's masked/copy flow
   for `kv/monitoring/grafana-admin` and store the credential in an approved
   password manager. Use only exact-path read access if delegated; do not grant
   list/write or attach it to the provider. Never paste it into chat, terminal
   commands, logs or a ticket. Clear the clipboard after use; avoid screen
   sharing/recording. No agent needs to retrieve the value.
5. Align the existing DB admin **once** using the supported CLI below. It runs
   directly inside a Grafana container, using the running release's mounted
   `grafana.ini` and `/etc/grafana-db` credentials, so it targets the existing
   PostgreSQL database rather than a new local SQLite database. There is no
   `/bin/sh` in the distroless Grafana image. Do not use `sh -c`, a positional
   password, `kubectl set env`, or reset from every pod/init container.

   Human-reviewed command, run from an interactive terminal after approval:

   ```bash
   python3 - <<'PY'
   import getpass
   import subprocess
   import warnings
   warnings.simplefilter('error', getpass.GetPassWarning)
   password = getpass.getpass('Paste the OpenBao Grafana admin password (hidden): ')
   if not password or '\n' in password or '\r' in password:
       raise SystemExit('Invalid single-line password')
   command = [
       'kubectl', '--context', 'k3s-on-omv', '-n', 'kube-prometheus-stack', 'exec', '-i',
       'deployment/kube-prometheus-stack-grafana', '-c', 'grafana', '--',
       '/usr/share/grafana/bin/grafana', 'cli',
       '--homepath', '/usr/share/grafana', '--config', '/etc/grafana/grafana.ini',
       'admin', 'reset-admin-password', '--user-id', '1', '--password-from-stdin',
   ]
   subprocess.run(command, input=password + '\n', text=True, check=True)
   PY
   ```

   `getpass` reads the terminal, not the heredoc. The password travels only via
   process memory and stdin, never argv/environment/files. Do not run in a
   terminal/session recorder or with credential-bearing kubectl debug verbosity.
   Check that local/cluster exec auditing does not record stdin bodies before use.
   Serially run this against one replica only; it updates the shared DB once.
   Require `Admin password changed successfully`; a missing-user listing is not
   success. Do not print the password to diagnose failures.
6. Verify UI login as `admin` over the existing TLS route, successful sidecar
   reloads without 401, both replicas Ready and Argo/VSO healthy. Both pods must
   have been recreated with the new Secret references; if cutover left an old
   pod, authorize a normal Grafana-only rollout. Test a second sync reuses KV
   version 1 and does not reset the DB or change the credential. Observe no
   credential values during validation. Cleanup of the unused old Secret is a
   separate approved operation after acceptance, not required to restore login.

## Rollback

Before DB alignment, pause the affected reconciliation if needed and revert only
these Grafana admin source changes through GitOps; preserve both Secrets, KV
history, PostgreSQL and all unrelated work. This restores the old references,
**not** the previously unknown DB password; the pre-existing login mismatch may
remain. Keep the new provider paths until the producer is retired through normal
Orphan lifecycle handling.

After alignment, prefer rolling back application/image changes while retaining
`grafana.admin.existingSecret: grafana-admin`: the DB now intentionally uses the
OpenBao password. Reverting Secret references alone will recreate the 401
mismatch. A full credential rollback requires an explicitly approved second CLI
reset to a known securely held credential and matching Secret custody/rollout;
never assume the old chart Secret equals the former DB password. Never restore
an entire database, delete KV history, rotate service DB credentials or remove
PVCs to undo an admin password change.
