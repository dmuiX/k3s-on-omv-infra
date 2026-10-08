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

## Native Raft snapshots

The chart's official snapshot agent creates an application-consistent native
Raft snapshot daily at `02:17 UTC` and uploads it directly to the private R2
object prefix. Jobs cannot overlap, run with bounded resources and use only the
dedicated `openbao-snapshot` ServiceAccount. Its 15-minute OpenBao token can read
only `sys/storage/raft/snapshot` and the exact credential object
`kv/openbao-snapshots/r2-credentials`. That KV-v2 object must contain exactly the
externally issued fields `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`; never
put either value in Git or command arguments.

The agent retains 14 days and emits no progress stream. NetworkPolicy limits it
to cluster DNS, the OpenBao API and outbound HTTPS. Prometheus alerts when a job
fails or no successful snapshot is observed for 26 hours. Before activation,
create the R2 credential object through a protected human/admin path, configure
R2 permissions for only the snapshot prefix, and enable object-lock or an
independent protected copy so an OpenBao compromise cannot erase every backup.
A successful upload is not restore acceptance: test native Raft restoration in
a separate isolated recovery exercise.

The excluded `06-data/openbao-backups` K8up application is legacy desired state
and must not be activated for OpenBao. It snapshots mounted files rather than
using OpenBao's native consistency boundary. K8up remains appropriate for other
eligible workloads.

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
