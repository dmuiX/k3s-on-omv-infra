# Start the Envoy build from Tekton Dashboard

The Dashboard uses `tekton-pipelines/tekton-dashboard` as its Kubernetes
identity and reads resources in `ci-builds`. The staged GitOps grant adds only
`create` on `pipelineruns.tekton.dev` in `ci-builds`. All authenticated Dashboard
operators share that identity. Tekton executes the run with the separate
`envoy-builder` ServiceAccount, which has no Kubernetes API grants; the run
also disables token mounting.

## Install the build and gate

1. Before merging the build-and-guard change, confirm no Envoy run is active
   and the Dashboard identity cannot create PipelineRuns. If a legacy manual
   grant is present, revoke it before continuing. After Argo syncs the
   `tekton-dashboard` Application, require the Pipeline, Task, locked inputs,
   admission policy and Deny binding to be present and Synced. Require the live
   policy's `status.typeChecking` to be present with no expression warnings.
2. Before merging the separate GitOps write-grant change, repeat the admission
   checks with the pinned Tekton webhook in isolation. The native admission CI
   test covers RBAC and policy evaluation but not Tekton webhook defaulting.
3. Block Dashboard user traffic before merging the write grant in
   `06-platform/tekton-dashboard/resources/build-access.yaml`. Keep it blocked
   during verification. Once Argo syncs the grant, use server-side dry-runs with
   `--as=system:serviceaccount:tekton-pipelines:tekton-dashboard`. Require the
   exact run below to be accepted and a different name or Pipeline to be denied
   by this admission policy on every serving API endpoint. Check that the
   account cannot update or delete runs, write other resources, or read Secrets.
   Revoke the grant before
   restoring traffic if any check fails. Do not rely on the UI flag for access
   control or install the upstream cluster-wide read-write grant.

## Run once

Approve an eligible builder after checking CPU, memory and disk headroom (see
the [manual build runbook](../README.md)). Do not enable a trigger or schedule.
Generate the approved Dashboard payload from the existing run template:

```sh
build=01-bootstrap/istio-gateway/build
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

To revoke access, block user traffic and commit `subjects: []` on the
Git-managed `ci-builds/tekton-dashboard-envoy-run` RoleBinding. Keep the
admission policy and binding in place until the identity is confirmed unable
to create PipelineRuns on every serving API endpoint. With automatic pruning
disabled, merely reverting the addition of the RoleBinding does not revoke it.
Do not remove the guard first or rely on changing UI mode.
