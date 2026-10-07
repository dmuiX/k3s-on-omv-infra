# OpenBao storage sizing

OpenBao runs three Raft server pods. Each pod receives one `1Gi` Raft data claim
and one `1Gi` audit claim from the encrypted, three-replica `longhorn`
StorageClass. Because every claim has a replica on every eligible node, the six
claims reserve approximately 6Gi on the limiting Raspberry Pi storage node.
Raft replication and Longhorn replication protect against different failures;
neither is a backup.

The chart values make both claim sizes explicit so an upstream chart default
cannot silently consume the Pi's capacity. The `PlatformPVCUsageWarning` and
`PlatformPVCUsageCritical` rules in the monitoring values cover the OpenBao
namespace at 70% and 85% usage.

## Audit growth

An audit PVC mount does not provide bounded retention by itself. Before enabling
a file audit device for sustained use, define and validate a rotation mechanism
that OpenBao can reopen safely, an off-cluster retention destination, and the
required retention period. Until that mechanism exists, treat the 70% alert as
a mandatory intervention point rather than allowing unbounded audit growth.
Do not delete the active audit file or its PVC to recover capacity.

## Resizing and replacement

The claims may be expanded declaratively after reviewing free Longhorn capacity.
They cannot be shrunk in place. Existing larger claims retain their current
capacity even after these values change. A smaller replacement requires a
separately approved migration or restore procedure, and the original volume,
backup, unseal material, and Longhorn encryption key must remain available until
independent verification succeeds. Rebuilding the entire cluster is not required
solely to change claim size, but it may be appropriate for an explicitly approved
fresh-cluster attempt that contains no data requiring preservation.
