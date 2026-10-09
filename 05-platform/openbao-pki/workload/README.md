# OpenBao PKI Kubernetes integration

Ansible owns the OpenBao PKI ceremony and API configuration: initialization,
seal and custody operations, PKI mounts and issuers, signing policies and roles,
Kubernetes auth roles, and interactive secret entry. None of those operations
run in the cluster or under Argo CD.

This Argo CD Application owns only the steady-state Kubernetes resources needed
by cert-manager after Ansible has configured OpenBao:

- `ClusterIssuer/openbao-pki-services` and `ClusterIssuer/openbao-pki-clients`;
- the two dedicated cert-manager ServiceAccounts and narrowly scoped
  TokenRequest Role/RoleBinding;
- `Certificate/openbao-internal-tls`.

The issuer profiles use separate signing endpoints, Kubernetes auth roles,
ServiceAccounts, and token audiences:

| ClusterIssuer | OpenBao signing endpoint | token audience |
|---|---|---|
| `openbao-pki-services` | `pki-services/sign/services` | `vault://openbao-pki-services` |
| `openbao-pki-clients` | `pki-clients/sign/clients` | `vault://openbao-pki-clients` |

The certificate is issued from the existing HTTP listener and is not mounted by
OpenBao in this release. It contains only the two DNS names of the future
`openbao-active-tls` Service, is valid for 90 days, renews 15 days early, and
stores an RSA-2048 key in `Secret/openbao-internal-tls`.

The cert-manager controller NetworkPolicy remains owned by the wave-2
cert-manager Application. Its private Kubernetes API endpoint CIDRs come from
the Live values repository.

Validate the Kubernetes desired state offline with:

```sh
ruby tests/verify-openbao-pki.rb
```
