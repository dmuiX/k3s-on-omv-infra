# Repository conventions

## Argo CD deployment sources

Deploy upstream applications with Argo CD multi-source Applications: a version-pinned Helm/OCI chart source plus Git-hosted values referenced through `$values`. Deploy the local `charts/cluster-config` chart the same way with private values from the live repository.

Keep authored Kubernetes/Kustomize resources in Git, but do not vendor Helm-rendered upstream manifests. Do not replace multi-source Helm Applications with generated plain-directory YAML unless the user explicitly requests that architecture change.
