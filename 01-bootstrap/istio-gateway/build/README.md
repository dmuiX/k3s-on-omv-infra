# Envoy backport build

[Envoy #48409](https://github.com/envoyproxy/envoy/pull/48409) wakes a blocked internal socket writer when its peer closes. Tekton tests the locked Istio/Envoy sources first and compiles the backport only when the expected stock regression fails. The pipeline is manual; Argo CD manages Tekton and the gateway separately and does not sync this directory.

## Run from a reviewed checkout

Check that the `tekton-pipelines` Application is healthy and its webhook is ready. Approve a Linux `amd64` worker labelled `ci.dmuix.com/envoy-builder=true` with sufficient CPU, memory and disk headroom. The build requests 4 CPUs, 10 GiB RAM and 110 GiB ephemeral storage; it checks for at least 100 GiB free. The Pi is ineligible.

The `tekton-pipelines` Argo CD Application must be synced first; it now owns the restricted `ci-builds` namespace. From the repository root, apply the pipeline and load its four input files from the same checkout, then create one run:

```bash
set -euo pipefail
build=01-bootstrap/istio-gateway/build
kubectl --context k3s-on-omv apply --server-side -f "$build/pipeline.yaml"
kubectl --context k3s-on-omv -n ci-builds create configmap envoy-build-inputs \
  --from-file="$build/lock.json" --from-file="$build/48409.patch" \
  --from-file="$build/regression.patch" --from-file="$build/inside.sh" \
  --dry-run=client -o yaml | kubectl --context k3s-on-omv apply --server-side -f -
kubectl --context k3s-on-omv create -f "$build/run.yaml"
```

Do not replace the inputs during an active run. Only trusted CI operators should be able to edit the pipeline or its inputs. If an earlier manual setup left the `one-build-pod` quota in `ci-builds`, removing it is a separate change; applying the new manifest will not delete it.

```sh
kubectl --context k3s-on-omv -n ci-builds get pipelineruns
kubectl --context k3s-on-omv -n ci-builds logs -l tekton.dev/pipelineRun=RUN_NAME -c step-test-and-build --follow
kubectl --context k3s-on-omv -n ci-builds get pipelinerun RUN_NAME -o jsonpath='{.status.conditions}{"\n"}{.status.results}{"\n"}'
```

`stock` means the pinned source passed without the patch. `patched` means the expected regression appeared, the patched tests passed, and an `amd64` binary was built. Review the stock log and reject other or ambiguous stock failures. The binary stays in the Pod's `emptyDir`; only its checksum is reported. This run does not publish an image or change the gateway.

For a newer Istio patch within the same minor, run `refresh.py` manually and review the lock and patch compatibility. Update the pinned toolchain image and version comment in `pipeline.yaml` with the lock. Before proposing any published image, verify its digest, architecture coverage, smoke test, and canary playback, seek, and drain. `promote.py` renders only a gateway image override and does not change Istio version pins.
