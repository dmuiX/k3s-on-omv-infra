# Start the Envoy build from Tekton Dashboard

The OIDC Dashboard is a separate deployment. It starts read-only, uses
`tekton-pipelines/tekton-dashboard` as its Kubernetes identity, and provides
read access to `ci-builds`. This optional grant adds only `create` on
`pipelineruns.tekton.dev` in `ci-builds`. All authenticated Dashboard operators
share that Kubernetes identity. The `envoy-builder` ServiceAccount still has no
Kubernetes API grants; the run also disables token mounting.

## Install the build and gate

After the OIDC Dashboard is deployed and access verified, use the same reviewed
checkout for the Pipeline, Task and all inputs. This setup does not submit a run:

```sh
build=01-bootstrap/istio-gateway/build
kubectl --context k3s-on-omv apply --server-side -k "$build"
kubectl --context k3s-on-omv apply --server-side -f "$build/dashboard/admission.yaml"
```

Before enabling writes in the live Dashboard, repeat the admission checks in
an isolated environment with the pinned Tekton webhook running. The CI test
covers the native policy and RBAC, not Tekton's defaulting webhook. Verify the
live admission policy reports no expression warnings and its binding is
present. Keep user requests to the Dashboard blocked during the write-access
checks: `--read-only=true` is not a substitute for Kubernetes RBAC. Only then
install the grant:

```sh
kubectl --context k3s-on-omv apply --server-side -f "$build/dashboard/access.yaml"
```

Use server-side dry-runs with
`--as=system:serviceaccount:tekton-pipelines:tekton-dashboard`. Verify that the
exact run below is accepted, a different name or Pipeline is explicitly denied
by admission, and the account cannot write other resources or read Secrets.
If a check fails, remove the RoleBinding before restoring user access. Verify
OIDC still restricts access to the intended operator group. In a separate
reviewed activation change, update the Dashboard Deployment's `--read-only`
flag and its read-only CI assertion, then restore access. Do not use the
upstream cluster-wide read-write RBAC.

## Run once

Approve an eligible builder after checking CPU, memory and disk headroom (see
the [manual build runbook](../README.md)). Do not enable a trigger or schedule.
Generate the approved Dashboard payload from the existing run template:

```sh
kubectl --context k3s-on-omv create --dry-run=client -f "$build/run.yaml" -o json |
  jq 'del(.metadata.generateName, .metadata.creationTimestamp, .status)
      | .metadata.name = "envoy-backport-dashboard"
      | .spec.timeouts.pipeline = "5h50m0s"
      | .spec.taskRunTemplate.podTemplate.automountServiceAccountToken = false'
```

Enable **Use Tekton Pipelines API version v1** in Dashboard settings. The YAML
editor uses that setting for the request URL, even when the YAML says `v1`.
Paste the output into **Create PipelineRun → YAML editor** in `ci-builds`.
The simple form does not carry the full approved Pod template. Do
not add labels, annotations or other overrides. Update the admission spec in the
same review if the run template or Tekton defaults change. The fixed name allows
only one Dashboard run object at a time. Do not start a concurrent CLI run or change the
build inputs during a run. After the run is terminal and its logs and results
are saved, a trusted operator can remove that exact run before allowing another.
The Dashboard cannot modify or delete it. The build still does not publish an
image or deploy a gateway.

To revoke access or retire the Dashboard, block user access first, then remove
only the `ci-builds/tekton-dashboard-envoy-run` RoleBinding. Keep the admission
policy and binding in place until the Dashboard identity is confirmed unable to
create PipelineRuns on every serving API endpoint. Restore read-only UI mode
before reopening access. Removing the Dashboard Application or these files from
Git does not revoke a manually applied RoleBinding.
