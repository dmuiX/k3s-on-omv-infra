# Mandatory Kubernetes timezone

`01-bootstrap/timezone-defaults/resources/defaults.yaml` centrally enforces **Europe/Berlin**, rather than merely filling missing defaults. It contains two native MutatingAdmissionPolicies, two ValidatingAdmissionPolicies and four bindings (`admissionregistration.k8s.io/v1`). The canonical Argo child installs them at wave 1: mutation policies/bindings first, then final validation policies/bindings. There is no additional Job, webhook, controller, container, RBAC, host mount or Ansible change.

## Behavior

| Resource | Admission | Required configuration |
| --- | --- | --- |
| CronJob, every namespace | CREATE / UPDATE | `spec.timeZone: Europe/Berlin`, including when the request specifies another timezone. |
| Pod, every namespace | CREATE | Every regular/init container, including native init sidecars, receives literal `TZ=Europe/Berlin`. No Windows or `envFrom` exception. |
| Pod debug containers | UPDATE of `pods/ephemeralcontainers` | Every newly appended ephemeral container receives the same literal TZ. Existing containers remain immutable. |

An explicit `TZ=UTC`, empty TZ or TZ `valueFrom` reference is replaced. The source reference is removed before applying the literal; its ConfigMap/Secret is **never read**. `envFrom` references and unrelated environment entries remain unchanged. The explicit TZ entry takes precedence over any imported TZ. Other container fields and container order are preserved. Ambiguous duplicate TZ entries are not accepted.

Final validation runs after mutation and denies missing, different, duplicate or indirect TZ declarations. It also protects against a later mutating webhook leaving an incorrect timezone. All four policies use **`failurePolicy: Fail`**: this is mandatory configuration, not best-effort defaulting. Internal policy errors can therefore block new Pods or CronJob writes. The native API test gate and a coordinated rollback path are required before activation.

The only Pod match exclusion is node-authenticated mirror requests. Static containers are started from node-local manifests, outside API admission; modifying their mirror object would not change the actual running environment. These policies cover API-managed workloads, not node-local static manifests, host processes or Compose.

The guarantee is the declared Kubernetes configuration. An application/image still needs to honor TZ and have suitable timezone data; an application explicitly formatting UTC may continue to do so. No image rebuilds, tzdata injection, host/Mac changes or log-viewer changes are included. **Compose remains untouched.**

## Scheduling and existing resources

Cron scheduling follows `spec.timeZone`, not container TZ. For Longhorn's existing `35 18 * * *` trim schedule, Berlin 18:35 means 17:35 UTC in winter and 16:35 UTC in summer. The IANA timezone supplies DST rules.

OpenBao snapshot, PostgreSQL size retention and PostgreSQL restore validation use this same central policy. They have **no duplicate per-application timezone settings**. Their schedules, suspend settings, images and credentials are unchanged. Native API tests submit the actual Git-owned CronJobs to prove central enforcement. No claim of an Argo diff loop is made; actual Argo reconciliation remains a rollout check.

Longhorn-generated CronJobs are covered without adopting their manifests. Longhorn 1.13.0 compares its desired spec against the last-applied-spec annotation rather than replacing a live injected field merely because it is absent from the desired spec ([source](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/controller/recurring_job_controller.go)). Actual reconciliation must still be verified after merge.

**Admission is not retroactive.** Existing CronJobs change on their next main-resource write; status updates do not suffice. Existing Pods and ephemeral containers keep their current environments until normal replacement. This PR does not force any restart, manually run a backup/restore/trim, or introduce a background migration Job.

## Review and rollout gates

Before merge, production access is read-only/dry-run only:

```sh
kubectl create --dry-run=server -f 01-bootstrap/timezone-defaults/resources/defaults.yaml
```

This checks schemas and CEL compilation without activating bindings. Eighteen focused tests (four offline and fourteen native admission tests) passed against isolated, agentless K3s `v1.37.1+k3s1`. The Pod JSONPatch uses CEL two-variable comprehensions, available from Kubernetes 1.33. Tests exercise actual CREATE/UPDATE/subresource admission, not a reimplementation of CEL. They also disable mutation bindings only in the isolated API to prove final denial, then restore and verify actual mutation before proceeding. See [test instructions](../tests/README.md#native-timezone-admission-tests).

After an approved merge:

1. Verify the root and `timezone-defaults` Applications have synced the merged revision and all eight native resources exist without expression warnings.
2. Verify new-creation admission with safe server dry-runs. Check both the literal TZ and the final rejection guard; never dump full environments or credential objects.
3. Inspect current CronJob timezone fields:
   ```sh
   kubectl get cronjobs -A -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,TIMEZONE:.spec.timeZone'
   ```
4. Any existing CronJob not on Berlin needs a **separately approved** one-time main-resource update through the same policy. Review exact targets and use resourceVersion protection. The new mandate deliberately replaces other timezone choices. No backfill was performed for this PR.
5. Verify Longhorn retains Berlin through normal reconciliation and Argo does not continuously fight admission. Observe normal scheduling; do not create a trim or backup merely to test the timezone.
6. Existing Pods are not restarted. Check safe TZ/date output only when they are normally replaced. Do not claim that installation alone changed existing processes or every application's log rendering.

## Rollback / admission recovery

An enforcement error can deny new Pods and CronJob writes, but these policies do not match their own policy/binding APIs, so an existing administrative client can recover without starting a new Pod. **Coordinate GitOps suspension/rollback first**, otherwise self-heal may restore manually removed bindings.

Pruning is disabled. Git revert or deleting YAML alone does not remove retained resources. After explicit approval, disable the two mutating and two validating bindings belonging to this Application; disabling only final validation does not bypass a failing mutator. Do not remove unrelated admission resources. Then reconcile the reviewed Git rollback. Already admitted fields remain Berlin, and existing process environments are not rewritten. No mass restart is part of rollback.
