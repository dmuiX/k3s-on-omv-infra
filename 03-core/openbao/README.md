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

## OpenBao 2.7 upgrade activation

The chart is pinned to `0.30.2`, which renders OpenBao `2.7.1`. This release is
required before enabling the cert-manager-managed listener because it adds the
listener-scoped `tls_auto_reload` facility. The upgrade release itself does not
add, remove or change any listener.

The StatefulSet deliberately retains `OnDelete`; an Argo sync updates the pod
template but does not restart a Raft voter. Treat activation as a guarded live
maintenance operation:

1. prove all three existing pods are Ready and unsealed, all three Raft peers
   are present as voters, and the active node is known;
2. prove a fresh Raft snapshot is stored off-cluster and independently usable,
   and verify recovery/unseal custody without printing that material;
3. sync the reviewed Application and confirm only the StatefulSet template is
   pending activation;
4. replace one standby pod, wait for it to run `2.7.1`, become Ready and unsealed,
   and rejoin as a healthy voter before touching another pod;
5. repeat for the other standby, then replace the active pod last and wait for a
   healthy leader and three voters;
6. verify the UI/API health, Kubernetes Auth, PKI issuance and audit writes. Run
   the isolated VSO compatibility PoC again before adding the TLS listener.

Never replace two voters concurrently. Stop immediately on a sealed pod, missing
peer, failed readiness, failed audit write or loss of leadership. Before the
first pod replacement, rollback is a Git revert. After any `2.7.1` node has
served cluster traffic, do not attempt an ad-hoc image downgrade; preserve the
volumes and use the separately reviewed snapshot recovery procedure if upstream
compatibility guidance requires restoration.
