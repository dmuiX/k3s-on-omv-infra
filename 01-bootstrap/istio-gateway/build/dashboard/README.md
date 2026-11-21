# Start the Envoy build from Tekton Dashboard

The Dashboard uses `tekton-pipelines/tekton-dashboard` as its Kubernetes
identity and reads resources in `ci-builds`. The GitOps RoleBinding allows
creating, updating, cancelling and deleting `pipelineruns.tekton.dev` in
`ci-builds`; admission restricts creation to the reviewed Envoy run. All authenticated Dashboard
operators share that identity. Tekton executes the run with the separate
`envoy-builder` ServiceAccount, which has no Kubernetes API grants; the run
also disables token mounting.

## Argo CD verification

Before merging, confirm no Envoy run is active and no legacy manual writer grant
exists. After Argo syncs the Pipeline, Task, locked inputs, Deny admission
binding and RoleBinding, require the live admission policy's
`status.typeChecking` to be present with no expression warnings. The native
admission CI test covers RBAC and policy evaluation, not Tekton webhook
defaulting.

Use server-side dry-runs with
`--as=system:serviceaccount:tekton-pipelines:tekton-dashboard` to verify the
exact run below is accepted and a different name or Pipeline is denied by the
admission policy on every serving API endpoint. Check that cancellation and
deletion work, and that the account cannot write other resources or read
Secrets. Revoke the grant before using the Dashboard if any check fails. Do not rely on the UI flag for
access control or install the upstream cluster-wide read-write grant.

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
The Dashboard can cancel or delete it afterwards. The build still does not publish an
image or deploy a gateway.

To revoke access, block user traffic and commit `subjects: []` on the
Git-managed `ci-builds/tekton-dashboard-envoy-run` RoleBinding. Keep the
admission policy and binding in place until the identity is confirmed unable
to create PipelineRuns on every serving API endpoint. With automatic pruning
disabled, merely reverting the addition of the RoleBinding does not revoke it.
Do not remove the guard first or rely on changing UI mode.
