# Draft: Longhorn UI hardening

No workload changes in this proposal. In the pinned [1.13.0 chart template](https://github.com/longhorn/longhorn/blob/v1.13.0/chart/templates/deployment-ui.yaml), the UI Deployment does not consume container security context, resources or readiness probe values. Adding those keys to `02-controllers/longhorn/app.yml` alone has no effect. Do not loosen namespace policy or patch all Longhorn workloads.

**Preferred implementation:** propose three optional UI-container settings upstream with the current defaults unchanged. When a released chart supports them, pin and render it with the actual Live values; verify that the changes affect only `Deployment/longhorn-ui`. Until then, leave the stock chart unchanged rather than introducing a second writer, separate controller or permanent fork for one Deployment. A maintained chart patch would require an explicit decision about ownership, upgrades and security backports.

| Setting on the UI container | Initial value | Evidence / limits |
|---|---|---|
| `securityContext.allowPrivilegeEscalation` | `false` | A running v1.13.0 UI process had UID 499. Confirm nginx starts and serves requests with no-new-privileges in a disposable environment. |
| CPU and memory requests | `10m`, `16Mi` | Seven-day observations: five-minute average CPU below 1m, maximum observed working set about 8.2 MiB per pod. Requests are a starting point, not limits or startup-peak measurements. |
| Readiness | HTTP `GET /` on named port `http` (8000) | The existing UI answered 200 on both pods. This checks the frontend, not manager health. Avoid liveness-triggered restarts as part of this change. |

**Gate:** compare the complete old/new chart render; reject incidental storage-controller changes from an upgrade unless reviewed separately. After the intended UI rollout require 2/2 Ready, both Service endpoints, an authenticated read-only view through the OIDC proxy, and no new restart/OOM trend. The manager, CSI, node components, volume replicas, routes and OIDC credentials are out of scope. Revert the UI settings if the UI fails readiness; do not restart storage engines.
