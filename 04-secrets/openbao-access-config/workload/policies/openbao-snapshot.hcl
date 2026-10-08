# Native Raft backup plus only the dedicated R2 credential object.
path "sys/storage/raft/snapshot" {
  capabilities = ["read"]
}

path "kv/data/openbao-snapshots/r2-credentials" {
  capabilities = ["read"]
}
