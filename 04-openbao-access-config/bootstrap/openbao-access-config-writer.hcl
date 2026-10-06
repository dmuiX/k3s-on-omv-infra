# Config Job may manage only its intended policy and webhook role and inspect kv/.
path "sys/policies/acl/vault-secrets-webhook-read" {
  capabilities = ["create", "read", "update"]
}
path "sys/policies/acl/human-admin" {
  capabilities = ["create", "read", "update"]
}
path "auth/kubernetes/role/vault-secrets-webhook" {
  capabilities = ["create", "read", "update"]
}
path "sys/mounts" {
  capabilities = ["read"]
}
path "sys/auth" {
  capabilities = ["read"]
}
