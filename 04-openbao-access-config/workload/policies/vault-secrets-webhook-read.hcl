# Deliberately broad: the webhook may read every secret in the existing KV v2
# mount. Anyone able to submit and read a matching labelled Kubernetes Secret
# can request any field from kv/data/* through the webhook. Review before use.
path "kv/data/*" {
  capabilities = ["read"]
}
