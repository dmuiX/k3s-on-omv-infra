# Personal administrator only; not for the webhook or configuration Job.
# This policy is highly privileged and can grant further permissions.
path "sys/mounts"          { capabilities = ["read"] }
path "sys/mounts/*"        { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "sys/auth"            { capabilities = ["read"] }
path "sys/auth/*"          { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "sys/policies/acl"    { capabilities = ["list"] }
path "sys/policies/acl/*"  { capabilities = ["create", "read", "update", "delete", "list"] }
path "auth/*"             { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "identity/*"         { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
path "kv/*"               { capabilities = ["create", "read", "update", "delete", "list", "patch"] }
