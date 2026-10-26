# Service route

One HTTPRoute to same-namespace Services, with explicit resource identity,
Gateway parents, hostnames and per-rule backend name, port and weight. Gateway
group/kind and Service group/kind are fixed in the template, not configurable.
The schema rejects unknown fields. Supported optional fields are path matches,
request/response header modifiers and request/backendRequest timeouts.

Private hostnames belong in Live values or Application patches. This chart
creates neither Services nor Secrets and never derives names from release names.

OpenBao 0.30.4's native `templates/server-httproute.yaml` hardcodes backend
kind/name/port but provides no values for backend group or weight. Those API
defaults cannot be pinned using its supported values. Disable that native route
when adding this source. Longhorn's route continues targeting its existing OIDC
proxy; the proxy input ConfigMap remains separately owned by Live Kustomize.

`tests/test_service_route.py` compares pre-migration identity/spec baselines.
OpenBao's baseline was rendered from the exact cached 0.30.4 chart with the
actual Live Application patch applied through offline Kustomize. Longhorn's
baseline includes its original route and a digest of the unchanged ConfigMap.
Hostname digests keep private domains out of the public fixtures. The only
allowed spec additions are OpenBao backend `group: ""` and `weight: 1`.
