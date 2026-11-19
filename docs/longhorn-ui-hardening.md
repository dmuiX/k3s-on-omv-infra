# Longhorn UI hardening (Infra #107)

The `longhorn` Application updates the existing `apps/v1` Deployment `longhorn/longhorn-ui` in place. Its `longhorn-ui` container sets `allowPrivilegeEscalation: false` (Linux no-new-privileges), requests `10m` CPU and `16Mi` memory, and uses HTTP `GET /` on the named `http` port (8000) for readiness. The probe checks the frontend, not manager health. The requests are based on seven days of observations (CPU below 1m on five-minute averages, peak observed working set ~8.2 MiB per pod); they are not startup-peak measurements or limits. There is no liveness-triggered restart.

## Why a single Deployment override

The pinned [1.13.0 UI template](https://github.com/longhorn/longhorn/blob/v1.13.0/chart/templates/deployment-ui.yaml) renders the Deployment unconditionally. It has neither these container settings nor a UI pod-annotation hook. Argo CD's Helm source cannot exclude that one object. The existing [Application](../02-controllers/longhorn/app.yml) already renders Helm first and the Infra resource directory second; `$live` has only a `ref`, not a manifest path. The directory supplies [one complete, hardened UI Deployment](../02-controllers/longhorn/resources/longhorn-ui.yaml), **not** a partial patch. The frontend Service and UI PDB still come from Helm.

[Argo CD v3.5.5 multi-source precedence](https://github.com/argoproj/argo-cd/blob/v3.5.5/docs/user-guide/multiple_sources.md) keeps only the last object for a group/kind/namespace/name before sync. Expect **one `RepeatedResourceWarning` for the UI Deployment**, not a second Deployment or controller. Do not suppress duplicate warnings globally. The changed pod template triggers one rolling update; subsequent syncs have the same desired template. No admission policy, rollout annotation, imperative patch or namespace change is needed.

## Release gates

1. **Before merging** (auto-sync watches `main`), compare the override to the pinned chart with the intended Live values and the Application's inline values. Public CI uses a synthetic values file: it cannot establish site parity. In the authorized integration workspace run the same offline parity test with `PLATFORM_CHART_AUDIT` set to the verified chart cache and `PLATFORM_LIVE_REPO` to the intended Live checkout:

   ```sh
   KUBECONFIG=/dev/null PYTHONPATH=tests python -m unittest -v \
     test_native_chart_migrations.LonghornUIOverride
   ```

   It checks the source order, the one overlapping identity and equality of the entire UI Deployment after removing exactly the three additions. **Stop on a mismatch.** Site changes to e.g. registry, timezone, affinity or tolerations do not flow automatically into this static override. On any UI-affecting values change or chart upgrade, regenerate from `helm template` with the same release, namespace and effective values (`--show-only templates/deployment-ui.yaml`), select only the Deployment and reapply the three fields. Keep private material out of Infra. Prefer retiring the override when native settings ship.
2. Rehearse in staging. In the Argo diff require only the three UI changes: no manager, CSI, storage classes, engine, route or proxy change. Verify that the existing Deployment is tracked by `longhorn`, its pod template is SSA-managed by Argo and no other manager owns the target fields. Do not use replace, delete/recreate or take ownership from another controller. Check target-node request headroom including the rolling-update surge.
3. On an approved rollout, require the same Deployment UID, the new generation observed and **2 updated, ready and available replicas**. Require two distinct ready Pod targets in the `longhorn-frontend` EndpointSlices, both from the new ReplicaSet, not old or terminating endpoints. Confirm an authenticated read-only UI view through the OIDC proxy, with no increased restarts/OOMs. Verify the named container's three fields in the admitted template and Argo's `Apply` managed-fields entry. A subsequent refresh must be Synced/Healthy without another ReplicaSet or storage-component rollout. Static renders do not establish these runtime checks. Argo's Deployment health alone does not count Service endpoints. Read-only checks for the operator, in the approved context:

   ```bash
   set -eo pipefail
   kubectl -n longhorn rollout status deployment/longhorn-ui --timeout=5m
   kubectl -n longhorn get deployment longhorn-ui -o json | jq -e '
     .metadata.generation == .status.observedGeneration and .spec.replicas == 2 and
     .status.updatedReplicas == 2 and .status.readyReplicas == 2 and .status.availableReplicas == 2'
   kubectl -n longhorn get endpointslices -l kubernetes.io/service-name=longhorn-frontend -o json | jq -e '
     [.items[].endpoints[] | select(.conditions.ready == true and .conditions.terminating != true
       and .targetRef.kind == "Pod") | .targetRef.uid | select(. != null)] | unique | length == 2'
   ```

## Rollback / retirement

Revert the UI override through Git and reconcile the **same** Application, leaving the chart and resource directory in place. Helm still emits the exact same Deployment identity, so even with `prune: false` this is an SSA update back to the chart template, not a deletion or an orphan. Argo drops fields it solely owns when omitted; verify they are absent (unless now provided natively), the duplicate warning clears, and repeat the endpoint and UI gates. If another manager retains a field, stop and review ownership; do not force a patch, delete the Deployment or restart storage engines.
