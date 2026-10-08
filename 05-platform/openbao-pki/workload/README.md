# Mandatory staged OpenBao PKI integration

This component is a mandatory platform phase, but it is intentionally inactive
in the default root configuration. `infra.yml` explicitly includes
`application.yml` and also excludes it, so exclusion wins until the
external-root ceremony and guarded installation have passed. The Application's
workload source is pinned to an immutable reviewed commit. After the gates pass,
the GitOps bootstrap promotes the root configuration to activate this pinned
Application; do not register it independently, switch it to a branch, or remove
the exclusion ad hoc.

The component provides two issuer profiles and stages one first-party consumer,
`Certificate/openbao-internal-tls`. The certificate is issued while the existing
HTTP listener remains available and is not mounted by OpenBao in this release.
That staging boundary prevents a missing certificate from affecting the running
Raft cluster:

| ClusterIssuer | OpenBao signing endpoint | permitted DNS hierarchy | leaf use |
|---|---|---|---|
| `openbao-pki-services` | `pki-services/sign/services` | below `.svc` or `.svc.cluster.local` | server only |
| `openbao-pki-clients` | `pki-clients/sign/clients` | below `.clients.cluster.local` | client only |

Both profiles set default and maximum leaf lifetime to 90 days, reject IP SANs,
localhost, bare domains, globs and unrestricted names, and use separate
cert-manager ServiceAccounts, OpenBao policies, PKI roles, auth roles and token
audiences. ClusterIssuer token audiences are exactly
`vault://openbao-pki-services` and `vault://openbao-pki-clients`, matching
cert-manager's ClusterIssuer audience convention. The Role permits the existing
`cert-manager` controller ServiceAccount to create tokens only for those two
identities.

## Pre-existing trust prerequisites

Operators must create and validate both `pki-services/` and `pki-clients/` PKI
mounts, their keys, signed intermediate certificates, complete chains and the
`auth/kubernetes/` configuration out of band. Do not place keys or bootstrap
tokens in Git. The reconciler verifies that each mount is a PKI mount and that
its default issuer reports a certificate plus a distinct issuing chain before
performing any write. It has no mount, issuer, key, root-generation or
intermediate-signing code.

Bootstrap one narrow ACL policy for the reconciler using the following reviewed
shape (exact path syntax may be entered through the OpenBao administrative UI):

```hcl
path "sys/mounts" { capabilities = ["read"] }
path "pki-services/issuer/default" { capabilities = ["read"] }
path "pki-clients/issuer/default" { capabilities = ["read"] }
path "sys/policies/acl/cert-manager-pki-services-sign" { capabilities = ["create", "read", "update"] }
path "sys/policies/acl/cert-manager-pki-clients-sign" { capabilities = ["create", "read", "update"] }
path "pki-services/roles/services" { capabilities = ["create", "read", "update"] }
path "pki-clients/roles/clients" { capabilities = ["create", "read", "update"] }
path "auth/kubernetes/role/cert-manager-pki-services" { capabilities = ["create", "read", "update"] }
path "auth/kubernetes/role/cert-manager-pki-clients" { capabilities = ["create", "read", "update"] }
```

Bind that policy in a pre-existing Kubernetes auth role named
`openbao-pki-reconciler` to only ServiceAccount `openbao-pki-reconciler` in
namespace `openbao`, with audience `openbao-pki-reconciler`, no default policy,
and a short token TTL. This bootstrap role is a prerequisite rather than an
object managed by itself. The Job uses only its projected ServiceAccount token
to log in; it never accepts or uses a root token.

The initial Argo sync hook verifies prerequisites (including a usable signing
key and `issuing-certificates` usage) and reconciles the six owned objects before
the ClusterIssuers are applied. The hourly CronJob repairs drift. Its
NetworkPolicy denies reconciler ingress and permits it only cluster DNS and
TCP/8200 to the active OpenBao server pods. The cert-manager controller policy
belongs to the wave-2 cert-manager Application and is rendered from
`02-controllers/cert-manager/network-policy` with endpoint CIDRs from the
private Live values. It preserves DNS, external TCP/443 and TCP/8200 to active
OpenBao, then permits TCP/6443 only to those private `/32` endpoints. K3s needs
the endpoint rule because this cluster evaluates Kubernetes Service traffic
after DNAT. Keeping the CIDRs in the private values prevents LAN topology from
leaking into this public repository.

The issuer and reconciler URLs remain HTTP because they bootstrap the certificate
from the existing internal listener. NetworkPolicy limits reachability but does
not encrypt node/CNI traffic; do not activate this component where that network
is untrusted.

The staged certificate contains only the two DNS names of the future
`openbao-active-tls` Service, is valid for 90 days, renews 15 days early and uses
an RSA-2048 key matching the constrained OpenBao signing role and stored only in
`Secret/openbao-internal-tls`. A later reviewed
release may mount that Secret read-only and add a parallel TLS listener and
active-only Service. It must not disable or repoint the HTTP issuer/reconciler
path in the same release. Keeping that narrow bootstrap path avoids making
OpenBao startup or certificate renewal depend circularly on the certificate it
is trying to issue. VSO will use only the parallel TLS Service with the issuing
CA; Kubernetes Auth remains its client identity and mTLS is not required.

## Ownership and rollback

The loop owns only these objects inside OpenBao:

- ACL policies `cert-manager-pki-services-sign` and
  `cert-manager-pki-clients-sign`, each granting only its exact leaf-sign path;
- PKI roles `pki-services/roles/services` and `pki-clients/roles/clients`;
- Kubernetes auth roles `cert-manager-pki-services` and
  `cert-manager-pki-clients`, including exact ServiceAccount, namespace, policy,
  one-hour token lifetime and audience.

Use the reviewed GitOps bootstrap rollback to disable the Argo Application and
stop reconciliation. Pruning can remove Kubernetes identities, ClusterIssuers
and the staged internal Certificate, so rollback must first prove that no TLS
listener or VSO connection consumes `Secret/openbao-internal-tls`. OpenBao
mounts, issuers, keys and CA chains remain untouched; removing the six managed
OpenBao policy/role objects, if desired, is a separate reviewed administrative
action.

Validate offline before bootstrap activation:

```sh
ruby tests/verify-openbao-pki.rb
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_openbao_pki_reconcile.py'
```
