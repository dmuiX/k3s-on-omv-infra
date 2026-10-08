# cert-manager controller egress

This chart belongs to the `cert-manager` Application in root sync wave 2 because
its NetworkPolicy selects the cert-manager controller pods in namespace
`cert-manager`. It is not owned by the later OpenBao PKI Application. Its
`cert-manager-controller-egress` name intentionally differs from the predecessor
`cert-manager-openbao-pki-egress` resource so an already-running wave-5
Application cannot self-heal over the wave-2 handoff.

The controller addresses `kubernetes.default.svc:443`, but K3s evaluates this
connection after Service DNAT on the current cluster. Private Live values
therefore provide the three server `/32` endpoints permitted on TCP 6443. The
public chart retains no LAN addresses. DNS, active OpenBao TCP 8200, and external
HTTPS TCP 443 are the only other egress paths.
