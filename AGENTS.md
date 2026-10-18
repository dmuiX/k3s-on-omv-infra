# Repository conventions

## Argo CD deployment sources

Deploy upstream applications with Argo CD multi-source Applications: a version-pinned Helm/OCI chart source plus Git-hosted values referenced through `$values`. The wave-1 `prometheus-operator-crds` Application is the single-source exception because it uses the dedicated chart without values. Deploy local Helm charts with private values from the live repository. Prefer native chart Gateway API support; the authored Argo CD route belongs to the `argocd-config` chart.

Keep authored Kubernetes/Kustomize resources in Git, but do not vendor Helm-rendered upstream manifests. Do not replace multi-source Helm Applications with generated plain-directory YAML unless the user explicitly requests that architecture change.
