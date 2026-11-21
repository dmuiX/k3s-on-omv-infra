# Tekton: Envoy pilot

Argo CD installs and reconciles Tekton Pipelines through `02-controllers/tekton-pipelines/app.yml`. Merging installs the control plane, but does not install the Envoy pilot, label a node, or start a build. Git stays on GitHub and Argo CD remains the application deployment owner. There is no external webhook, schedule, registry credential, image push, or gateway change.

## Install and run after approval

1. Before merging, approve the controller rollout and verify capacity on the eligible workers. The new child occupies sync wave 2; an unhealthy first sync can hold later waves. Review cluster-wide CRDs, RBAC, and internal admission webhooks in the [official Tekton Pipelines v1.18.0 release](https://github.com/tektoncd/pipeline/releases/tag/v1.18.0). Check the upstream release checksum against the unchanged vendored file. The overlay schedules Tekton's own Deployments on Linux `amd64`, disables credential initialization and remote resolvers, and does not aggregate Tekton access into Kubernetes' default edit/admin/view roles.

   ```sh
   printf '%s  %s\n' 029c8400e42d0fcd4fc669140d382af1f1d8ae86202b710d6197ae22bd48be5f ci/tekton/controller/release.yaml | shasum -a 256 -c -
   kubectl kustomize ci/tekton/controller
   ```

2. After the reviewed merge, wait for the `tekton-pipelines` Application to report `Synced` and `Healthy`. Verify the Deployments before separately applying the opt-in pilot resources:

   ```sh
   kubectl --context k3s-on-omv -n tekton-pipelines rollout status deployment/tekton-pipelines-controller --timeout=180s
   kubectl --context k3s-on-omv -n tekton-pipelines rollout status deployment/tekton-pipelines-webhook --timeout=180s
   kubectl --context k3s-on-omv wait --for=condition=Established crd/tasks.tekton.dev crd/pipelines.tekton.dev crd/pipelineruns.tekton.dev --timeout=60s
   ```

   Confirm the internal Tekton admission webhooks have registered rules and CA bundles and that the API server can reach the webhook before separately applying `ci/tekton/pilot/pipeline.yaml` with server-side apply.

3. Separately select and approve a suitable Linux `amd64` worker with the label `ci.dmuix.com/envoy-builder=true`. The repository does not set this label and the example stays unschedulable without it. Never label the Pi. The existing GitHub backport workflow excludes production nodes: do not repurpose those as builders without a separate capacity and isolation decision. Check allocatable **and** actual CPU/RAM/disk headroom, kubelet eviction margins, and that the local build does not compete with backup or storage I/O. The pilot requests 4 CPUs, 10 GiB RAM and 110 GiB ephemeral storage; the Pod also checks for at least 100 GiB actually free. Verify restricted admission, DNS and public HTTPS egress through the installed K3s network-policy implementation before running the build. The namespace is intended for trusted CI operators only; do not give public pull requests or general users permission to create runs or edit tasks.

4. Create exactly one attended run and observe it before authorizing another:

   ```sh
   kubectl --context k3s-on-omv create -f ci/tekton/pilot/run.yaml
   kubectl --context k3s-on-omv -n ci-builds get pipelineruns
   kubectl --context k3s-on-omv -n ci-builds logs -l tekton.dev/pipelineRun=RUN_NAME -c step-test-and-build --follow
   kubectl --context k3s-on-omv -n ci-builds get pipelinerun RUN_NAME -o jsonpath='{.status.conditions}{"\n"}{.status.results}{"\n"}'
   ```

   `patched` means the expected regression was present in the stock test failures, the patched tests passed, and Bazel produced an `amd64` Envoy binary. Inspect the printed stock failure summary; do not accept a run with other or ambiguous stock failures. `stock` means the regression tests passed without the patch and no replacement binary was built. A compilation failure or incompatible patch is not a candidate. The binary lives only in the Task Pod's `emptyDir`; the PipelineRun records its checksum, not a deployable artifact. On resource pressure, cancel the exact PipelineRun, inspect the Pod and wait for resource usage to recover before attempting another run. Keep the result and logs before removing that run.

## Next gate and rollback

After an accepted run, evaluate Pipelines-as-Code for trusted GitHub events and `.tekton/` PipelineRuns, with an explicit trust policy before accepting any public repository events. Add a separate publish step with scoped credentials and an image smoke test. Chains can attest the resulting image digest when publication is in place. The Dashboard should be separate, read-only and accessible only through authenticated OIDC. None requires installing the Tekton Operator in this first phase. An `amd64`-only image must never replace a multi-architecture image for a workload that is still eligible for ARM nodes.

To retire the pilot, stop any run and remove its workload resources first. Pruning is disabled on both the child and root Applications: a Git revert stops declaring the child but is not an uninstall. Disable/remove the exact child Application before any controlled cleanup. Remove the controller only after checking for other Tekton users. Removing Tekton CRDs also removes their stored objects and is a separate destructive operation. The current GitHub workflow and Argo CD gateway remain independent.
