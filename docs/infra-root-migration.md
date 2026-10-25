# Direct Infra root with private Helm values

The root `infra` Application reads this public repository directly. Its recursive
file selector includes only `*/app.yml` and `*/*/app.yml`, never `infra.yml` itself
or workload manifests. Child Applications retain their chart versions, resource
sources and sync settings.

The four former private Application patches are now native Helm value files in
the private Live repository at `infra/helm-values/<application>.yml`. A `ref: live`
source resolves `$live` from that repository's root. Existing Live manifest sources
remain; new reference-only sources do not generate resources.

## Publication and activation order

1. Merge the private Live change adding the four value files first. They are
   outside every existing Application resource directory, so publishing them
   cannot cause Argo CD to mistake Helm values for Kubernetes manifests.
2. Merge the Infra change. The old Live Kustomize wrapper remains available and
   still applies the same overrides. This permits a staged transition; new
   Application chart sources also consume the new files.
3. Update Bootstrap's root template to the same direct Infra source before the
   next deployment. Do not run the full deployment just to change the root.
4. Review a diff of the existing `argocd/infra` Application against this repo's
   `infra.yml`. Through an explicitly approved root-only update, replace only
   `spec.source` with the direct Infra directory source. Preserve Bootstrap's
   ownership annotations, other metadata and unrelated fields. Do not blindly
   apply the whole manifest or use a merge patch that leaves the old `kustomize`
   field alongside `directory`.
5. Verify the root references the Infra revision, contains exactly the same child
   Application identities, and no private settings or existing resources vanish.
   A paused Grafana Deployment remains an operator decision: do not resume it as
   part of this migration. Changes to its Application can trigger reconciliation,
   so review `spec.paused` drift before activation.

Root refresh alone is insufficient to activate the migration while its source
still points at Live. The old wrapper does not select this root manifest.
Keep the legacy wrapper and patches until acceptance; removal is a separate
cleanup. No provider ACL, credentials, database resources, chart version,
Alertmanager replica count or Grafana admin Secret ownership changes belong here.

## Evidence and validation

`tests/test_infra_root_values.py` compares the old patch-applied effective values
against Helm's new `valueFiles` then `valuesObject` precedence for all four apps.
It verifies private paths, safe staged publication and root file selection. Set
`HELM_CHART_CACHE` to a directory containing the exact chart archives to run the
optional native Helm render check:

```sh
HELM_CHART_CACHE=/path/to/chart-archives python -m unittest discover \
  -s tests -p test_infra_root_values.py
```

Native mechanisms checked: Argo CD multi-source external value files and Helm
value precedence; chart versions argo-cd 10.10.3, Longhorn 1.13.0, OpenBao 0.30.4
and kube-prometheus-stack 92.10.1. No custom renderer or chart dependency is added.
References: [external values](https://argo-cd.readthedocs.io/en/stable/user-guide/multiple_sources/#helm-value-files-from-external-git-repository)
and [Helm precedence](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/#helm-value-precedence).

Existing chart limitation: OpenBao 0.30.4's `templates/server-httproute.yaml`
hardcodes backendRefs without `group` or `weight` and offers no values field to
supply them. This migration preserves the effective route. Fixing those
API-defaulted fields requires a separately reviewed chart/route change; do not
claim the current chart renders them explicitly. Gateway parent references are
retained with explicit group and kind.

## Rollback

Before root activation, revert the Infra child Application changes while keeping
the private files; the original wrapper and patches still work. After activation,
restore the previous complete root `spec.source` (Live `infra/applications` with
`kustomize: {}`), preserving ownership metadata and removing `directory`. Keep
private files until no Application references them. Do not prune, delete Secrets,
change database passwords, shrink PVCs or resume Grafana to roll this back.
