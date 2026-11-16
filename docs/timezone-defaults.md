# Kubernetes timezone defaults

`01-bootstrap/timezone-defaults` installs two native `admissionregistration.k8s.io/v1` MutatingAdmissionPolicies and their bindings through the canonical Infra root, at wave 1. It adds no webhook, controller, container, RBAC, host mount or bootstrap imperative step. The stable admission API is required; the policies were compiled and exercised against K3s `v1.37.1+k3s1`.

## Behavior

| Resource | Admission | Default |
| --- | --- | --- |
| CronJob, any namespace | CREATE / UPDATE | Missing or empty `spec.timeZone` becomes `Europe/Berlin`. Explicit nonempty choices such as `UTC` remain unchanged. |
| Linux/unspecified-OS Pod | CREATE only | Eligible regular and init containers, including native init sidecars, receive `TZ=Europe/Berlin`. Other container fields and environment entries are preserved. |

The Pod policy acts at admission, independently of Helm chart-specific environment interfaces. It does not mutate Deployment/Job templates, force rollouts or update immutable existing Pod environments. Explicit `env` entries named `TZ`, including empty values and `valueFrom` references, are preserved. Containers with nonempty `envFrom` are deliberately left alone: imported ConfigMaps/Secrets might already define TZ. No credential object is fetched to resolve that ambiguity. Windows Pods and node-authenticated mirror-Pod requests are excluded. Ephemeral-container subresource updates are outside scope.

Imported-env preservation uses the fields present when the policy runs. A later webhook that adds `envFrom` to an already-defaulted container must manage TZ explicitly; the policy does not remove existing env entries.

These are convenience defaults, **not enforcement or a security boundary**. Both policies use `failurePolicy: Ignore` so an internal defaulting error cannot block deployments or scheduled work. Reinvocation is idempotent and can handle later admission-injected containers.

Images/applications still need to support TZ and have suitable timezone data. Applications explicitly formatting UTC may continue to emit UTC logs. No image rebuilds, tzdata injection, host/Mac timezone changes, Compose changes or log-viewer changes are included. Existing Compose globals remain as they are.

## Scheduling and existing resources

Cron scheduling follows `spec.timeZone`, not container TZ. For the existing Longhorn `35 18 * * *` trim schedule, Berlin 18:35 corresponds to 17:35 UTC in winter and 16:35 UTC in summer. The IANA timezone supplies DST rules; no fixed UTC offset is substituted.

The three directly Git-owned CronJobs explicitly declare Berlin as well, so admission-only fields do not introduce Argo diff/self-heal churn. Their cron expressions and suspend settings are unchanged. This change does not manually run any backup, restore or trim task.

Longhorn-generated CronJobs are covered by admission, without taking ownership of their manifests or changing the Longhorn chart. Longhorn 1.13.0's recurring-job controller compares its desired spec with the last-applied-spec annotation rather than repeatedly replacing a defaulted live field solely because it is absent from the desired spec ([source](https://github.com/longhorn/longhorn-manager/blob/v1.13.0/controller/recurring_job_controller.go)). Actual controller reconciliation remains a post-merge acceptance check.

**Admission is not retroactive.** Existing generated CronJobs can keep a missing field until a main-resource update or recreation. Existing Pods keep their current environment until their normal replacement. Do not claim rollout/backfill or local-time log acceptance from policy installation alone.

## Review and rollout gates

Before merge, only isolated API tests and production server-side dry-runs are permitted:

```sh
kubectl create --dry-run=server -f 01-bootstrap/timezone-defaults/resources/defaults.yaml
```

A policy dry-run checks schema and CEL compilation; it does not activate bindings or prove live mutation. The opt-in native tests separately install the exact manifests into an isolated, agentless K3s test API and exercise CREATE/UPDATE admission. See [test instructions](../tests/README.md#native-timezone-admission-tests).

After an approved merge:

1. Verify the root and `timezone-defaults` Applications have synced the merged revision, and both policies/bindings exist.
2. Inspect only CronJob names, namespaces and timezone fields:
   ```sh
   kubectl get cronjobs -A -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,TIMEZONE:.spec.timeZone'
   ```
3. Review generated CronJobs still missing a timezone. Backfill is a **separately approved** operation, not an install hook in this PR. It changes scheduling metadata only, never explicit existing timezone choices. Use a reviewed resourceVersion precondition to avoid overwriting a concurrent choice. No backfill was executed for this PR.
4. Confirm Longhorn retains the admitted field through normal recurring-job reconciliation. Check actual next scheduled/executed wall times after deployment; do not create a trim or backup Job merely to check the timezone.
5. For newly replaced application Pods, inspect only the allowed TZ field or a safe date/time rendering if needed, never full environments. Existing Pods are not restarted by this change. Unsupported images/applications may remain on UTC.

## Rollback

Pruning is disabled, like the other canonical Applications. Removing YAML or reverting Git alone does not delete retained policy bindings. Coordinate a GitOps rollback with separately approved removal of the retained bindings/Application as appropriate. Disabling admission does not strip already written CronJob fields or Pod environment entries; reverting those is a separate, explicitly scoped decision. No mass restart is part of rollback.
