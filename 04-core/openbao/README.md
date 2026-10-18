# OpenBao runtime

This directory owns only the pinned OpenBao runtime in canonical wave 4. It configures three Raft voters, one encrypted 1 GiB data PVC per voter, static-seal input from the create-once Secret, bounded resources, and HTTP service listeners for Ambient mTLS.

OpenBao initialization, recovery custody, the immutable Crossplane trust root, human/MFA enrollment, and PKI key/issuer ceremonies remain Bootstrap responsibilities. Steady-state managed resources belong to `06-platform/openbao-config`; snapshots and retention belong to `06-platform/openbao-backups`.

The runtime chart must not be extended with invented snapshot values. Deployment remains blocked until the separate snapshot adapter and protected Live inputs satisfy `deployment-blockers.json`.

Any voter replacement is a guarded runtime operation: verify a current off-cluster native Raft snapshot, unseal custody, three healthy voters, and quorum; replace one standby at a time; wait for readiness and voter membership before continuing; replace the leader last. Never replace two voters concurrently.
