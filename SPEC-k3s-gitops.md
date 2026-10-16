# SPEC — K3s Homelab: einfacher Bootstrap und GitOps-Betrieb

**Stand:** 14.10.2026  
**Fassung:** v5  
**Status:** Architektur- und Implementierungsvorgabe, keine fertige Deployment-Konfiguration

> **Leitlinie:** Einfachheit zuerst, GitOps als primärer Betriebsweg. Helm Values vor zusätzlichen Ressourcen; vorhandene Kubernetes-CRDs vor Eigenbau; Crossplane nur dort, wo außerhalb von Kubernetes liegende Konfiguration deklarativ gebraucht wird. Kustomize nur bei nachweisbarem Nutzen. Keine Klartext-Credentials in Git und keine ungeschützten Bootstrap-Schlüssel.

Diese Spezifikation beschreibt den vollständigen Aufbau und gewünschten Endzustand eines neuen Clusters. Bestandteil sind die konkrete HA-Auslegung, OpenBao einschließlich PKI, das CloudNativePG-Barman-Cloud-Plugin, HashiCorp Vault Secrets Operator (VSO), Radar, drei getrennte Backup-Ziele und verbindliche Benamungsregeln.

## 1. Ziele und Prioritäten

- Drei On-Premise-Debian-Nodes mit K3s reproduzierbar betreiben: `omv`, `wyse5070` und der Raspberry Pi `raspi4` mit 4 GiB RAM.
- Die Control Plane besteht aus drei K3s-Servern mit embedded etcd.
- Sinnvolle HA nur dort einsetzen, wo mehrere Replikate tatsächlich Verfügbarkeit bringen. Singletons werden ausdrücklich als Singletons dokumentiert und nicht durch ein PDB als HA bezeichnet.
- Kubernetes-Secrets at rest und persistente Longhorn-Volumes verschlüsseln. Das initiale Schlüsselmaterial verwaltet Ansible geschützt unter `$HOME/secrets/`.
- Ansible nur für Host-/K3s-Bootstrap, erste Argo-CD-Installation, nicht über GitOps lösbare OpenBao-Initialisierung und geschützte Erfassung initialer Zugangsdaten verwenden. Der normale Betrieb erfolgt über Argo CD.
- Vorhandene Helm-Charts und Operator-CRDs mit knappen Values verwenden. Keine selbst gebauten Controller und keine eigenen Helm-Charts, solange native Ressourcen oder ein gepflegter Chart ausreichen.
- Neue Workloads mit möglichst wenig app-spezifischer Konfiguration bereitstellen: Helm Values oder native Manifeste, HTTPRoute, TLS, OIDC, VSO-Secret-Synchronisierung, PostgreSQL und bei Bedarf Redis.
- Drei voneinander getrennte Backup-Ziele betreiben: PostgreSQL, OpenBao und allgemeine K8up-Workload-Backups.
- Persistentes, terminalfreundliches Logging über Fluent Bit zum zentralen rsyslog auf dem OMV-Host bereitstellen.
- Den paketierten K3s-`local-storage`-Provisioner von Anfang an deaktivieren. Longhorn ist der einzige dynamische Storage-Provider für persistente Plattform- und Workload-Daten.

## 2. Komponenten und Zuständigkeiten

| Komponente | Aufgabe und Zielzustand |
|---|---|
| **K3s** | Drei Server mit embedded etcd und aktivierter Secrets Encryption at Rest. |
| **Traefik (K3s)** | Edge-Ingress und Gateway-API-Controller. Kein paralleler Istio-Ingress. |
| **Gateway API** | Gemeinsame `GatewayClass`/`Gateway`-Plattformressourcen und möglichst eine `HTTPRoute` pro Anwendung. |
| **cert-manager** | Cloudflare DNS-01 für öffentliche Zertifikate und OpenBao-PKI-ClusterIssuer für interne Service- und Client-Zertifikate. |
| **Longhorn** | Verschlüsselter persistenter Storage. Allgemeine PVCs verwenden drei Storage-Replikate; datenbankseitig replizierte PostgreSQL-PVCs verwenden bewusst nur ein Longhorn-Replikat pro Instanz. |
| **Argo CD** | Zentraler GitOps-Controller für Plattform und Anwendungen nach dem Bootstrap. |
| **OpenBao** | Drei Raft-Voter, OIDC-Provider, KV, Kubernetes Auth, Database Secrets Engine und interne PKI. Jede Instanz besitzt genau ein Daten-PVC. |
| **Crossplane + provider-vault** | Deklarative OpenBao-Ressourcen aus der exakt aktivierten 19-Typen-Liste, soweit die gepinnten CRDs sie gegen OpenBao zuverlässig unterstützen: gemeinsame Mount-/Policy-/Auth-Konfiguration sowie PostgreSQL-, OIDC- und PKI-Steady-State-Ressourcen. |
| **HashiCorp Vault Secrets Operator (VSO)** | Synchronisiert statische und dynamische Werte aus OpenBao in namespace-lokale Kubernetes-Secrets. Workloads referenzieren diese Secrets über normale Kubernetes-Mechanismen. |
| **CloudNativePG** | Drei PostgreSQL-Instanzen, je ein eigenes verschlüsseltes Longhorn-PVC, TLS und native `Database`-Ressourcen, die jeweils vom konsumierenden Workload besessen werden. |
| **Barman Cloud Plugin** | WAL-Archivierung und geplante PostgreSQL-Base-Backups in den dedizierten PostgreSQL-Bucket. Eine größenbasierte Retention hält mindestens zwei vollständige Backups und entfernt ältere Backup-/WAL-Ketten ausschließlich über die offiziellen Barman-Cloud-Kommandos. |
| **K8up** | Backups gewöhnlicher Workload-PVCs in den dedizierten K8up-Bucket. CloudNativePG-PVCs und OpenBao-Raft-PVCs werden nicht als Ersatz für deren anwendungskonsistente Backups behandelt. |
| **Redis-Operator** | Stellt bei Bedarf deklarativ eine eigene Redis-Instanz beziehungsweise einen Redis-Cluster pro konsumierendem Workload bereit, vergleichbar mit CloudNativePG-Ressourcen. Das Argo-CD-interne Redis zählt nicht als Workload-Redis. |
| **Istio Ambient** | Internes Workload-mTLS ohne Sidecars in den Anwendungspods. `ztunnel` läuft als Node-Komponente; Waypoint-Proxies werden nur für nachgewiesene L7-Anforderungen eingesetzt. Traefik bleibt alleiniger Edge-Controller. |
| **Fluent Bit** | DaemonSet zum Sammeln der Pod-/Containerlogs aller drei Nodes und zur Weiterleitung an das zentrale rsyslog. Die Output-Konfiguration bleibt austauschbar, damit später Loki ergänzt oder als Ziel eingesetzt werden kann. |
| **Log-Backend** | Zunächst das vorhandene zentrale rsyslog/journald. Historische Logs müssen per CLI abrufbar sein. Loki ist kein Bestandteil dieses ersten Aufbaus. |
| **kube-prometheus-stack** | Prometheus, Alertmanager und Grafana jeweils mit zwei Replikaten auf `omv` und `wyse5070`. Monitoring läuft nicht auf `raspi4`. |
| **Radar** | Konkreter Referenz-Workload für HTTPRoute, TLS, OpenBao OIDC, PostgreSQL und VSO-Secret-Synchronisierung. Radar bleibt zunächst bewusst single-replica. |

### 2.1 Repository-Grenzen

| Repository | Schutzklasse und Inhalt | Darf nicht enthalten |
|---|---|---|
| **Bootstrap** | Geschützt und clusterspezifisch. Hostinventar, SSH-Benutzer, private Node-Adressen, K3s-Konfiguration, Traefik/Gateway, Argo-Erstinstallation, create-once Schlüssel, OpenBao-Initialisierung, geschützte Credential-Abfragen und Live-Abnahmen. | Steady-State-Workload-Manifeste, wiederkehrende Anwendungskonfiguration oder Secretwerte im Checkout. |
| **Infra** | Öffentlich/publizierbar. Wiederverwendbare Plattform-Applications, gepinnte Charts, öffentliche Values, Controller, Plattformressourcen, Policies und Tests. | Reale Hostnamen, ACME-Identität, private CIDRs, R2-Account-Endpunkte, Bucketnamen oder aufgelöste Secrets. |
| **Live** | Geschützt. Clusterbezogene, nicht geheime Werte: Hostnamen, Ports, ACME-E-Mail, private CIDRs, R2-Endpunkte/Bucketnamen, native private Values und `vault:`-Referenzen. Live ist nur Argo-Quelle und besitzt keine Root-Application. | Aufgelöste Zugangsdaten, generische Controllerlogik, gerenderte Upstream-Charts oder eine zweite Eigentümerschaft für Infra-Ressourcen. |
| **Apps** | Anwendungen nach abgeschlossener Plattformabnahme. Jede App besitzt ihre eigenen Values, Routes, VSO-, OIDC-, Datenbank-, Redis- und Backup-Ressourcen. Eine separate Apps-Root wird erst nach Infra-Abnahme aktiviert. | Shared Controller, CRDs, Gateway/GatewayClass, Longhorn, OpenBao, zentrale Datenplattform oder Monitoring-Controller. |

Secrets und Schlüsselmaterial gehören in keines der Git-Repositories. Bootstrap speichert create-once Material ausschließlich geschützt unter `$HOME/secrets/`; Live enthält nur nicht geheime Identifikatoren und Secret-Manager-Referenzen.

### 2.2 Root-, Source- und Ownership-Vertrag

- Bootstrap installiert Argo CD und erstellt oder verifiziert zunächst exakt die Infra-Root. Es verändert deren Child-Selector danach nicht mehr. Nach erfolgreicher Infra-Abnahme erstellt oder verifiziert Bootstrap separat die Apps-Root und aktiviert sie.
- Infra besitzt die Auswahl aller Plattform-Applications, Apps die Auswahl aller Anwendungs-Applications. Live liefert beiden ausschließlich explizit referenzierte private Quellen; weder Infra noch Apps besitzt die Root des jeweils anderen Repositories.
- Sync-Waves ordnen nur Applications innerhalb derselben Root. Die Apps-Root darf ihre Abhängigkeit von Infra nicht durch eine eigene Wave vortäuschen; Bootstrap aktiviert sie erst nach erfolgreicher Infra-Abnahme.
- Upstream-Plattformkomponenten verwenden Argo-Multi-Source in dieser Reihenfolge: gepinnter Chart, öffentliche Infra-Values, private Live-Values, verfasste Infra-Ressourcen, explizit benötigte private Live-Ressourcen und zuletzt ein ausdrücklich dokumentierter Override. Anwendungen verwenden entsprechend: gepinnter Chart, öffentliche Apps-Values, private Live-Values, verfasste Apps-Ressourcen, explizit benötigte private Live-Ressourcen und zuletzt ein ausdrücklich dokumentierter Override. Doppelte Objektidentitäten zwischen diesen Quellen sind außerhalb des benannten Overrides verboten.
- Plattform-Applications synchronisieren und self-healen, aber prunen nicht automatisch. Zustandsbehaftete Löschungen benötigen einen gesondert geprüften Cleanup.
- Traefik, GatewayClass und gemeinsames Gateway gehören Bootstrap/K3s. Jede Anwendung besitzt ihre HTTPRoute; Live liefert nur private Hostwerte.
- Die zentrale VSO-Application besitzt nur den Operator. Der jeweilige Verbraucher besitzt `VaultConnection`, `VaultAuth`, Secret-CR und Ziel-Secret-Vertrag.
- Controller-Applications besitzen nur Controller/CRDs. CNPG-Operator und Barman-Plugin sind Controller; `k3s-postgres`, ObjectStore und zentrale Backups gehören der PostgreSQL-Plattform. Anwendungsdatenbanken, DB-Rollen und Credentials gehören dem jeweiligen Verbraucher.
- Kein Kubernetes- oder OpenBao-Objekt darf zwei fortlaufende Eigentümer haben.

### 2.3 Ownership-Matrix

| Capability | Kubernetes-/GitOps-Eigentümer | Externe oder create-once Konfiguration |
|---|---|---|
| Hosts, K3s, embedded etcd, Node-Taints | Bootstrap/Ansible | Bootstrap/Ansible |
| Traefik, Gateway API, GatewayClass, Gateway | Bootstrap/K3s | Bootstrap/Ansible |
| Argo CD Erstinstallation, Infra-Root und Apps-Root | Bootstrap/Ansible | Bootstrap/Ansible |
| Argo-Konfiguration und Plattform-Children | Infra/Argo CD | — |
| Longhorn Controller und StorageClasses | Infra/Argo CD | Longhorn-Schlüssel durch Bootstrap/Ansible |
| OpenBao StatefulSet/Raft | Infra/Argo CD | Init, Seal, Recovery-Custody und initialer Human-Admin durch Bootstrap/Ansible |
| OpenBao Snapshot-CronJob und R2-Ziel | `openbao-backups` in Infra/Argo CD | R2-Credentials create-once durch Bootstrap/Ansible in OpenBao |
| OpenBao Trust Root für Crossplane | Infra stellt Provider-Runtime bereit | Kubernetes-Auth-Mount/-Config, Provider-Policy und Provider-Auth-Rolle durch Bootstrap/Ansible |
| Unterstützte OpenBao-Steady-State-Ressourcen | Verbraucher-Manifeste in Infra/Apps, reconciliert durch Crossplane | PKI-Key-/Issuer-Ceremony und create-once Secretwerte durch Bootstrap/Ansible |
| cert-manager ClusterIssuer und interne Zertifikate | konsumierende Infra-/Apps-Application | OpenBao-PKI-Key-/Issuer-Ceremony durch Bootstrap/Ansible |
| Öffentliches Wildcard-Zertifikat und `wildcard-tls` | cert-manager aus der Bootstrap-eigenen Gateway-Annotation | Bootstrap besitzt Gateway, Listener, Hostwerte und Annotation; `letsencrypt-dns01` besitzt den referenzierten ClusterIssuer |
| VSO-Operator | Infra/Argo CD | — |
| VSO-Verbindung/Auth/Secret-CR | jeweiliger Verbraucher | OpenBao-Policy/Auth-Rolle gemäß Verbraucher |
| CNPG- und Barman-Controller | Infra/Argo CD | — |
| PostgreSQL-Cluster, ObjectStore, zentrale Backups und Size-Retention-CronJob | Infra/Argo CD | initiales OpenBao-Manager-Credential und R2-Credentials ausschließlich create-once durch Bootstrap/Ansible in OpenBao; R2-Endpunkt, Bucketname und Speicherbudgets als nicht geheime Werte in Live |
| Anwendungsdatenbank und DB-Rolle | konsumierende Infra-/Apps-Application | OpenBao-Rolle über Crossplane; Credentials über VSO |
| Radar | Apps/Argo CD | eigene Database-, OIDC-, DB-Rollen- und VSO-Ressourcen |
| Redis-Instanz/-Cluster | jeweiliger Verbraucher | Secrets über VSO |
| Pod-/Containerlogs | Fluent Bit in Infra | rsyslog-Speicherung und Hostsicherung auf OMV |

## 3. Konkrete HA- und Verfügbarkeitsauslegung

Mehr Replikate allein sind kein HA-Nachweis. Für jede Komponente gelten zusätzlich Readiness, Verteilung, PDB-Verhalten, Failover- und Neustarttests.

| Komponente | Replikate und Verteilung | Verfügbarkeitsmechanismus |
|---|---|---|
| **K3s Control Plane** | 3 Server, je einer auf `omv`, `wyse5070`, `raspi4` | embedded-etcd-Quorum; Wartung immer Node für Node. Ansible setzt bei der K3s-Registrierung stabile Capability-Labels. |
| **Longhorn Node-Komponenten** | DaemonSets auf allen 3 Nodes | CSI und Volume-Attach/Recovery pro Node. |
| **Longhorn zentrale Controller** | CSI-Controller je 3; Global Manager 3; UI 2 | PDBs schützen die Controller. UI-Verfügbarkeit ist nicht storagekritisch. |
| **cert-manager** | Controller, Cainjector und Webhook jeweils 2 | Verteilung auf unterschiedliche Nodes; PDB `minAvailable: 1`. |
| **K8up** | 2 Controller-Replikate | Leader Election; PDB `minAvailable: 1`. |
| **OpenBao** | 3 StatefulSet-Pods, je einer pro Node | Integriertes Raft mit 3 Votern, required Pod-Anti-Affinity/Topology-Verteilung und PDB `maxUnavailable: 1`. Automatisches statisches Unseal nach Neustarts. |
| **Vault Secrets Operator** | zunächst 1 Controller-Replikat | Bewusster Controller-Singleton; keine HA-Behauptung. VSO reconciliiert die gewünschten Secrets nach einem Neustart erneut. |
| **CloudNativePG Operator** | 2 Replikate | Leader Election und PDB `minAvailable: 1`. |
| **Barman Cloud Plugin** | 2 Replikate | PDB `minAvailable: 1`. |
| **PostgreSQL** | 3 Instanzen, required Anti-Affinity auf Hostname, `maxSkew: 1` | Eine Primary, zwei Replicas; mindestens eine synchrone Replica, automatisches Failover, CNPG-PDBs. |
| **Prometheus** | 2 unabhängige Replikate, je eines auf `omv` und `wyse5070` | Jede Replica besitzt ein eigenes 20-GiB-RWO-PVC mit genau einem Longhorn-Replikat und scrapt dieselben Ziele. Alert-Regeln laufen redundant; Alertmanager dedupliziert Meldungen. Es gibt keinen dritten Prometheus auf dem Pi. |
| **Alertmanager** | 2 Replikate, je eines auf `omv` und `wyse5070` | Cluster/Gossip, getrennte 1-GiB-RWO-PVCs mit je einem Longhorn-Replikat und Verteilung über beide Nodes. Kein drittes Replikat auf dem Pi. |
| **Grafana** | 2 Replikate, je eines auf `omv` und `wyse5070` | Gemeinsames PostgreSQL für Zustand und Sessions; Dashboards und Plugins werden identisch aus Git provisioniert. Kein gemeinsam gemountetes RWO-PVC. |
| **Argo CD** | Application Controller, Repo Server, API Server und internes Redis zunächst jeweils 1 | Bewusste Singletons; Git bleibt Source of Truth. Das interne Redis ist eine Argo-CD-Abhängigkeit und kein Workload-Redis. |
| **Traefik** | 1 Controller, ServiceLB auf allen Nodes | Bewusster Singleton beim Controller; keine HA-Behauptung. |
| **Crossplane/provider-vault** | zunächst je 1 | Controller-Retry und deklarativer Zustand, keine HA-Behauptung. |
| **Radar** | 1 | Bewusster Singleton wegen prozesslokaler Caches, Streams und Sessions; PDB macht ihn nicht HA. |

### 3.1 Storage-Replikation

- Die allgemeine verschlüsselte StorageClass `longhorn` nutzt `Retain` und drei Longhorn-Replikate.
- OpenBao verwendet pro Pod ein verschlüsseltes 1-GiB-Daten-PVC über `longhorn`: insgesamt drei PVCs. Raft repliziert die logischen OpenBao-Daten zusätzlich auf Anwendungsebene.
- PostgreSQL verwendet `longhorn-postgres` mit `Retain`, Verschlüsselung und genau einem Longhorn-Replikat pro PVC. Die drei CNPG-Instanzen liefern die Datenbankreplikation; eine zusätzliche Dreifachreplikation jedes PVCs wird bewusst vermieden.
- Beide Prometheus-Replikate und beide Alertmanager-Replikate verwenden jeweils ein eigenes RWO-PVC über `longhorn-monitoring`. Diese StorageClass verwendet genau ein Longhorn-Replikat, `WaitForFirstConsumer`, `dataLocality: strict-local` und den Longhorn-Node-Tag `monitoring-storage`. Bootstrap setzt vor der Longhorn-Installation auf `omv` und `wyse5070` die unterstützte Annotation `node.longhorn.io/default-node-tags: '["monitoring-storage"]'`. Kubernetes-Pods wählen `monitoring=true`; Longhorn wählt unabhängig davon den Longhorn-Tag. Die zweite Anwendungspod-Replica liefert bereits die Ausfallkopie; zwei Storage-Replikate je PVC würden unnötig vier physische Monitoring-Datenkopien erzeugen. Grafana verwendet wegen der zwei Replikate kein gemeinsam gemountetes RWO-PVC.
- `longhorn-postgres` verwendet ebenfalls genau ein Longhorn-Replikat, `WaitForFirstConsumer` und `dataLocality: strict-local`. Required Pod-Anti-Affinity verteilt die drei PostgreSQL-Pods, wodurch auch ihre lokalen Volume-Replikate auf drei Nodes verteilt werden. Die Abnahme prüft Pod-Node und Longhorn-Replica-Node für jede Instanz ausdrücklich.
- K3s wird mit deaktiviertem paketiertem `local-storage` gestartet. Weder `local-path-provisioner` noch eine `local-path`-StorageClass werden angelegt.

## 4. Bootstrap und sichere Erstkonfiguration

Der One-Shot orchestriert zwei klar getrennte Ansible-Phasen: zuerst Host/K3s, danach GitOps/OpenBao. Dies darf aus Sicht des Bedieners ein Ablauf sein, bleibt intern aber in zwei Playbooks getrennt, damit K3s-Basis und GitOps-Vertrauen unabhängig wiederholbar und prüfbar bleiben.

### 4.1 Host- und K3s-Playbook

Das Basis-Playbook muss:

1. Debian 13, systemd, `amd64`/`arm64`, eindeutige Nodeidentitäten, saubere Hosts, cgroup v2 und erforderliche Raspberry-Pi-Bootparameter prüfen; einen nötigen Reboot ankündigen und kontrolliert anhalten statt ihn zu improvisieren.
2. iSCSI, NFS, dm-crypt, Kernelmodule und weitere K3s/Longhorn-Abhängigkeiten über die nativen Paket- und systemd-Mechanismen bereitstellen.
3. gepinnte K3s-Binaries inklusive Prüfsummen installieren und drei Server mit embedded etcd, Flannel VXLAN, NetworkPolicy, festen Cluster-/Service-CIDRs, `secrets-encryption: true` und deaktiviertem `local-storage` aufbauen.
4. über K3s `node-label` bei der Registrierung `compute-class=standard` und `monitoring=true` auf `omv`/`wyse5070` sowie `compute-class=small` auf `raspi4` setzen. Alle drei Storage-Nodes erhalten `storage=true`. Workload-Scheduling verwendet diese verständlichen Labels statt konkrete Hostnamen.
5. `raspi4` mit `CriticalAddonsOnly=true:NoSchedule` registrieren. Die verbindliche Toleration-Allowlist umfasst nur node-lokale K3s-/CNI-Komponenten, Longhorn-Manager/CSI, Istio `ztunnel`, Fluent Bit, Node Exporter sowie genau je eine OpenBao- und PostgreSQL-Instanz. Schwere Monitoring-Pods, Grafana, Alertmanager, Prometheus und gewöhnliche Anwendungen erhalten keine Pi-Toleration. Zusätzliche Tolerations benötigen eine dokumentierte Kapazitäts- und Funktionsbegründung.
6. vor dem ersten Peer-Join einen frischen etcd-Snapshot zusammen mit dem passenden Server-Token direkt auf einen zweiten Host übertragen und beide Prüfsummen verifizieren.
7. etcd-Mitglieder, Quorum, Alarme, Learner-Status, Kubernetes-Nodes, Labels/Taints, Secret-Encryption und Pod-Netzwerk nach jedem Join prüfen.
8. Gateway-API-CRDs über den unterstützten K3s-Mechanismus vor Traefik bereitstellen, anschließend Traefik freigeben und GatewayClass/Gateway abnehmen.
9. den Longhorn-Schlüssel create-once unter `$HOME/secrets/` mit Verzeichnismodus `0700` und Dateimodus `0600` erzeugen, das immutable Kubernetes-Secret vor dem ersten verschlüsselten PVC anlegen und die erforderlichen Longhorn-Node-Annotationen auf den bereits registrierten Nodes setzen, bevor Argo Longhorn installieren darf.
10. kubectl-Zugang geschützt bereitstellen, ohne Clusterzugänge oder Schlüssel in Git zu schreiben.
11. Hostjournale mit dem unterstützten systemd/rsyslog-Mechanismus zum OMV-Host weiterleiten; Fluent Bit ist ausschließlich für Kubernetes-Pod-/Containerlogs zuständig.

### 4.2 GitOps- und OpenBao-Playbook

Das zweite Playbook muss:

1. die vollständige Drei-Node-Kohorte und alle Basisabnahmen erneut prüfen;
2. Argo CD create-once aus gepinnten Artefakten installieren, Ressourcen setzen, Infra und read-only Live registrieren, die Application-Health-Anpassung vor der Root bereitstellen und die exakte Infra-Root anlegen;
3. Longhorn, seine drei StorageClasses und einen temporären verschlüsselten PVC inklusive Write/Read/Reattachment abnehmen;
4. den OpenBao-Seal-Key create-once erzeugen, OpenBao genau einmal initialisieren und Root-/Recovery-Material sowie Seal-Key geschützt auf zwei unterschiedlichen Hosts mit übereinstimmenden Prüfsummen verwahren;
5. drei Raft-Voter, drei Daten-PVCs, Raft-Quorum und automatisches Unseal nach Pod-, Node- und vollständigem Cluster-Neustart prüfen;
6. OpenBao ausschließlich über einen lokalen Port-Forward mit dem geschützten Initial-Token erreichen und noch keine später Crossplane gehörende Steady-State-Konfiguration anlegen;
7. ausschließlich die unveränderliche Crossplane-Trust-Root imperativ in OpenBao einrichten: OpenBao-Kubernetes-Auth-Mount/-Konfiguration, Provider-Policy und Provider-Auth-Rolle. Argo besitzt ServiceAccount und `ProviderConfig`; diese reconciliieren erfolgreich, sobald die OpenBao-seitige Trust-Root existiert. Der Provider darf seine eigene Vertrauenskette nicht selbst verwalten;
8. Crossplane übernehmen lassen und anschließend alle vom gepinnten Provider unterstützten nicht geheimen OpenBao-Steady-State-Ressourcen deklarativ reconciliieren: KV-/`userpass`-Mounts, Password Policies, Nicht-Provider-ACL-Policies, konsumierende Kubernetes-Auth-Rollen, Database-Konfiguration/-Rollen, OIDC sowie PKI-Leaf-Rollen/URLs/CRL-Konfiguration;
9. nur die nicht deklarativ delegierbaren Ceremonies weiter mit Ansible durchführen: Human-User/MFA, PKI-Key-/Issuer-Erzeugung und create-once Secretwerte. Den persönlichen Admin create-once mit verdeckter Doppeleingabe erzeugen, TOTP im Browser einschreiben, erst nach erfolgreichem OTP-Test erzwingen und den MFA-Login prüfen. Root, Intermediates, beide internen Testzertifikate und OpenBaos öffentliche HTTPS-Route prüfen;
10. Cloudflare-DNS-Token, die drei getrennten bucketgebundenen R2-Credential-Sätze und das K8up-Repository-Passwort verdeckt abfragen und create-once in OpenBao schreiben. Vor dem Schreiben des OpenBao-Backup-Credentials muss der Bediener bestätigen, dass dessen verschlüsselte Quelle unabhängig von Cluster und OpenBao wieder abrufbar ist; der Ablauf erzeugt keine unnötige zusätzliche Kopie; anschließend die drei vorab benannten R2-Buckets über die S3-kompatible API verifizieren und nachweisen, dass jeder Credential-Satz ausschließlich seinen vorgesehenen Bucket erreichen kann. Fehlt ein Bucket, stoppt der Ablauf mit einer konkreten Cloudflare-R2-Anweisung, statt den eng begrenzten DNS-Token zweckzuentfremden oder dauerhaft ein accountweites Management-Token anzufordern;
11. das `openbao_manager`-Credential create-once erzeugen und die PostgreSQL-/Crossplane-/VSO-Kette bis zu drei gesunden CNPG-Instanzen, statischen Rollen und erfolgreicher Rotation prüfen;
12. native OpenBao-Snapshots, PostgreSQL-Backup/PITR und K8up mit isolierten temporären Restore-Zielen abnehmen;
13. nach Plattformabnahme das Apps-Repository read-only registrieren, die exakte Apps-Root create-once erstellen oder verifizieren, dadurch Radar aktivieren und HTTPS, OIDC Login/Logout, VSO, PostgreSQL und Wiederanlauf prüfen;
14. am Ende Infra- und Apps-Root, Applications, Zertifikate, Routes, Storage, Backups, Monitoring und Logging auf `Synced`, `Healthy` und funktionale Abnahme prüfen.

### 4.3 Ansible-Grenzen

- Ansible verwaltet keine wiederkehrenden Workload-Ressourcen und ersetzt keinen GitOps-Controller.
- Wiederkehrende Backups, Rotation, Secret-Synchronisierung und Anwendungskonfiguration gehören Argo CD und den jeweiligen Operatoren.
- Secret-bearing Tasks verwenden verdeckte Eingaben und unterdrückte Ausgaben. Secrets werden weder als Extra-Var noch als Kommandoargument übergeben.
- Teilzustände, fremde Eigentümerschaft und widersprüchliche create-once Artefakte führen zum Abbruch statt zu stiller Adoption oder Überschreibung.
- Destruktiver Reset, Datastore-Konvertierung und Disaster Recovery sind getrennte, ausdrücklich bestätigte Verfahren und nie Teil des normalen One-Shot-Laufs.

## 5. OpenBao: genaue Zielkonfiguration

### 5.1 Laufzeit und Storage

- OpenBao läuft als StatefulSet mit drei Pods und integriertem Raft.
- Jeder Pod erhält einen stabilen Raft-Node-ID und findet Peers über Kubernetes Auto-Join.
- Das statische Seal liest seinen Schlüssel read-only aus dem Secret `openbao-unseal-key`.
- Ressourcen pro Pod: Request `100m` CPU/`256Mi`, Limit `500m` CPU/`512Mi`.
- Die drei Daten-PVCs sind jeweils 1 GiB groß und verwenden die verschlüsselte StorageClass `longhorn`.
- OpenBao lauscht auf Anwendungsebene clusterintern auf HTTP Port 8200. Der Netzwerk-Hop ist dennoch verschlüsselt: OpenBao sowie seine Clients Crossplane, VSO, cert-manager und der Snapshot-CronJob nehmen am Istio Ambient Mesh teil, sodass `ztunnel` den Verkehr transparent per mTLS überträgt. Browserzugriff und OIDC werden durch Traefiks `websecure`-Gateway als HTTPS veröffentlicht; auch der Traefik-Pod wird gezielt in Ambient aufgenommen, damit sein Backend-Hop zu OpenBao mTLS verwendet. Der OpenBao-Prozess benötigt dadurch keinen zweiten internen TLS-Listener und keinen zweistufigen Git-Zustand.

### 5.2 Auth-Methoden und Maschinenrollen

Vorgesehene Auth-Methoden:

- `userpass` für den initialen Human-Admin mit MFA;
- `kubernetes` für Controller und Workloads;
- OIDC wird von OpenBao als Provider für Anwendungen verwendet und ist von einem möglichen OpenBao-Login via OIDC zu unterscheiden.

Zielrollen:

| Rolle | ServiceAccount / Namespace | Policy | TTL |
|---|---|---|---|
| `grafana-vso` | `grafana-vso` / `kube-prometheus-stack` | `grafana-vso-read` | 1 h |
| `radar-vso` | `radar-vso` / `radar` | `radar-vso-read` | 1 h |
| `postgresql-vso` | `postgresql-vso` / `cloudnative-pg` | `postgresql-vso-read` | 1 h |
| `openbao-snapshot` | `openbao-snapshot` / `openbao` | `openbao-snapshot` | 15 min |
| `k8up-validation-vso` | `k8up-validation-vso` / `backup-validation` | `k8up-validation-vso-read` | 15 min |
| `backup-capacity-vso` | `backup-capacity-vso` / `backup-validation` | `backup-capacity-vso-read` | 15 min |
| `<workload>-k8up-vso` | `<workload>-k8up-vso` / jeweiliges Workload-Namespace | `<workload>-k8up-vso-read` | 1 h |
| `crossplane-provider-vault` | `provider-vault` / `crossplane-system` | `crossplane-provider-vault` | 1 h |
| `cert-manager-pki-services` | `openbao-pki-services` / `cert-manager` | `cert-manager-pki-services-sign` | kurzlebig |
| `cert-manager-pki-clients` | `openbao-pki-clients` / `cert-manager` | `cert-manager-pki-clients-sign` | kurzlebig |
| `cert-manager-dns01` | `cert-manager-dns01` / `cert-manager` | `cert-manager-dns01-read` | 1 h |

Die VSO-Rollen werden von ihren namespace-lokalen `VaultAuth`-Ressourcen verwendet.

### 5.3 Policies und Ownership

Die benötigten Pfade werden explizit dokumentiert; keine generischen erfundenen Freigaben:

- `openbao-snapshot`: `read` und `sudo` auf `sys/storage/raft/snapshot` sowie `read` auf `kv/data/openbao-snapshots/r2-credentials`.
- `k8up-validation-vso-read` sowie jede workload-spezifische `<workload>-k8up-vso-read`-Policy: Token-Erneuerung und ausschließlich `read` auf `kv/data/k8up/r2-credentials`, `kv/data/k8up/repository-password` und deren Metadatenpfade. Jede Kubernetes-Auth-Rolle ist an genau den benannten ServiceAccount und dessen Namespace gebunden; es gibt keine Wildcard-Namespace-Rolle.
- `backup-capacity-vso-read`: Token-Erneuerung und ausschließlich `read` auf die drei R2-Credential-Pfade für PostgreSQL, OpenBao und K8up samt Metadatenpfaden. Die Rolle ist nur an `backup-capacity-vso` im Namespace `backup-validation` gebunden und dient ausschließlich dem read-only S3-Listing für die Aggregatprüfung der drei Backup-Buckets.
- `crossplane-provider-vault`: Token-Self-Service für Lookup/Renewal sowie die für die aktivierten CRDs erforderlichen create/read/update-Rechte auf:
  - die ausdrücklich deklarierten KV-, `userpass`-, Password-Policy- und Nicht-Provider-ACL-Policy-Ressourcen;
  - konsumierende Kubernetes-Auth-Rollen, ausdrücklich ohne die eigene Rolle `crossplane-provider-vault` und ohne Auth-Mount-Konfiguration;
  - `database/config/k3s-postgres`, Database-Rollen und statische Rollen;
  - `identity/oidc/key/*`, `identity/oidc/scope/*`, `identity/oidc/client/*`, `identity/oidc/provider/*` sowie freigegebene Gruppen/Rollen/Assignments;
  - freigegebene PKI-Leaf-Rollen, URL- und CRL-Konfiguration, aber keine PKI-Keys oder Issuer-Ceremony.
  Die endgültige HCL listet jeden Pfad explizit. Der Provider erhält weder Root-/Seal-/Recovery-Zugriff noch Rechte auf seine eigene Policy, Auth-Rolle oder Kubernetes-Auth-Konfiguration.
- `grafana-vso-read`: Token-Erneuerung und `read` auf `database/static-creds/grafana`.
- `radar-vso-read`: Token-Erneuerung sowie `read` auf `database/static-creds/radar`, `kv/data/radar/argocd` und dessen Metadatenpfad.
- `postgresql-vso-read`: Token-Erneuerung sowie `read` auf `kv/data/postgresql/openbao-manager`, `kv/data/postgresql/r2-credentials` und deren Metadatenpfade.
- `cert-manager-pki-services-sign`: nur Signieren über `pki-services/sign/services`.
- `cert-manager-pki-clients-sign`: nur Signieren über `pki-clients/sign/clients`.
- `cert-manager-dns01-read`: Token-Erneuerung sowie ausschließlich `read` auf `kv/data/cert-manager/cloudflare` und dessen Metadatenpfad. Die namespace-lokalen VSO-Ressourcen materialisieren daraus das von den DNS-01-Issuern referenzierte Secret.
- `human-admin`: Verwaltung von Mounts, Auth, Policies, Identity und KV entsprechend der Bootstrap-Administration. Der dauerhafte Root-Token wird nicht für den normalen Betrieb genutzt.

Ownership:

- Ansible verwaltet nur Initialisierung, Seal/Recovery-Custody, die unveränderliche Crossplane-Trust-Root, Human-User/MFA, PKI-Key-/Issuer-Ceremony und create-once Secretwerte.
- Sobald die Provider-Trust-Root funktioniert, verwaltet Crossplane alle getesteten, nicht geheimen und vom gepinnten Provider unterstützten OpenBao-Steady-State-Ressourcen.
- Argo CD verwaltet Kubernetes-Ressourcen.
- Kein Objekt darf gleichzeitig von Ansible und Crossplane fortlaufend verwaltet werden.

### 5.4 Begrenzte provider-vault-API-Aktivierung

Der vollständige `provider-vault` bleibt installiert, aber der Greenfield-Bootstrap aktiviert vor dem ersten Providerstart nur exakt 19 geprüfte Managed-Resource-Typen. Dadurch starten keine Controller und API-WATCHes für 229 ungenutzte Typen.

Sechs unmittelbar verwendete Typen:

1. `SecretBackendConnection` (`database.vault.upbound.io`)
2. `SecretBackendStaticRole` (`database.vault.upbound.io`)
3. `OidcClient` (`identity.vault.upbound.io`)
4. `OidcKey` (`identity.vault.upbound.io`)
5. `OidcProvider` (`identity.vault.upbound.io`)
6. `OidcScope` (`identity.vault.upbound.io`)

Zwölf für die deklarative OpenBao-Steady-State-Konfiguration reservierte Typen:

7. `Mount` (`vault.vault.upbound.io`)
8. `Policy` (`vault.vault.upbound.io`)
9. `SecretBackendV2` (`kv.vault.upbound.io`)
10. `Backend` (`auth.vault.upbound.io`)
11. `Policy` (`password.vault.upbound.io`)
12. `AuthBackendRole` (`kubernetes.vault.upbound.io`)
13. `SecretBackendRole` (`database.vault.upbound.io`)
14. `Group` (`identity.vault.upbound.io`)
15. `OidcRole` (`identity.vault.upbound.io`)
16. `SecretBackendRole` (`pki.vault.upbound.io`)
17. `SecretBackendConfigUrls` (`pki.vault.upbound.io`)
18. `SecretBackendCrlConfig` (`pki.vault.upbound.io`)

Bedingt reserviert:

19. `OidcAssignment` (`identity.vault.upbound.io`)

Die `ManagedResourceActivationPolicy` muss vor dem Provider-Package existieren. Innerhalb der `provider-vault`-Application erhält `activation-policy.yaml` Resource-Wave `-20`, `runtime.yaml` Wave `-10` und `provider.yaml` Wave `0`; Argo darf den Provider erst anwenden, nachdem Activation Policy und RuntimeConfig erfolgreich vorhanden sind. Eine Verengung der Policy deaktiviert bereits gestartete Typen nicht. Deshalb ist die exakte 19-Typen-Liste Teil des Erstaufbaus und kein nachträglicher Cleanup. Reaktivierungen erfolgen ausschließlich durch eine geprüfte Erweiterung dieser Liste.

Abnahme:

- genau 19 Vault-MRDs/zugehörige Managed-CRDs sind aktiv beziehungsweise registriert;
- die fünf Provider-Konfigurations-CRDs bleiben erhalten;
- Datenbank- und OIDC-Ressourcen reconciliieren erfolgreich;
- keine der übrigen 229 Managed APIs erzeugt Controller oder Kubernetes-API-WATCHes für ihre Vault-CRD-Gruppen;
- Gesamt-WATCHes, WATCHes auf Vault-CRD-Gruppen, Goroutines, RSS und Heap werden im warmen, führungsäquivalenten Zustand dokumentiert;
- ein niedriger Wert direkt nach Pod- oder K3s-Neustart gilt nicht als dauerhafter RAM-Nachweis.

### 5.5 PKI

Die OpenBao-PKI ist verbindlicher Bestandteil der Zielarchitektur:

- `pki-root`: interne EC-P-256-Root-CA mit zehn Jahren Laufzeit;
- `pki-services`: EC-P-256-Intermediate für Service-Zertifikate mit fünf Jahren Laufzeit;
- `pki-clients`: EC-P-256-Intermediate für Client-Zertifikate mit fünf Jahren Laufzeit;
- die privaten Schlüssel verbleiben in OpenBao;
- die Root-CA signiert beide Intermediates;
- feste Default-Issuer werden nach Erstellung gesetzt und verifiziert.

cert-manager verwendet zwei getrennte ClusterIssuer:

| ClusterIssuer | Signierpfad | Kubernetes-Rolle | Audience |
|---|---|---|---|
| `openbao-pki-services` | `pki-services/sign/services` | `cert-manager-pki-services` | `vault://openbao-pki-services` |
| `openbao-pki-clients` | `pki-clients/sign/clients` | `cert-manager-pki-clients` | `vault://openbao-pki-clients` |

Die PKI-Abnahme stellt je ein separates Testzertifikat über `openbao-pki-services` und `openbao-pki-clients` aus. Leaf-Zertifikate sind 90 Tage gültig und werden 30 Tage vor Ablauf erneuert. Das öffentliche OpenBao-HTTPS-Zertifikat für Browser und OIDC stammt wie die übrigen externen Routen aus dem von cert-manager anhand der Bootstrap-eigenen Gateway-Annotation erzeugten `wildcard-tls`-Secret. Dafür existiert kein zweites Certificate-Manifest; die Abnahme prüft Annotation, referenzierten ClusterIssuer, erzeugtes Certificate und Ready-Secret als zusammenhängenden Vertrag.

## 6. PostgreSQL und Barman Cloud

### 6.1 CloudNativePG

- Clustername: `k3s-postgres`.
- Drei Instanzen, required Pod-Anti-Affinity auf `kubernetes.io/hostname` und Topology Spread `maxSkew: 1`.
- Jede Instanz besitzt ein eigenes verschlüsseltes 5-GiB-PVC über `longhorn-postgres`.
- Mindestens eine synchrone Replica; Failover-Quorum aktiv; Primary-Updates erfolgen kontrolliert per Switchover.
- TLS ist für Clientverbindungen erforderlich.
- Jeder konsumierende Workload besitzt seine eigene native CNPG-`Database`-Ressource. OpenBao verwaltet die zugehörigen Anwendungs-Logins und deren Rotation; CNPG `managed.roles` darf dieselben Identitäten nicht parallel verwalten.

### 6.2 Barman Cloud Plugin

Das gepinnte Barman-Cloud-Plugin ist Bestandteil der Plattform:

- zwei Controller-Replikate mit PDB `minAvailable: 1`;
- WAL-Archiver für `k3s-postgres`;
- tägliches geplantes Base-Backup;
- Backup bevorzugt von einer Standby-Instanz;
- separates `ObjectStore`-Objekt und eigener PostgreSQL-Bucket;
- die `ObjectStore.spec.retentionPolicy` bleibt ungesetzt, damit keine zeitbasierte Plugin-Retention mit der größenbasierten Eigentümerschaft konkurriert;
- ein GitOps-verwalteter CronJob `postgresql-size-retention` begrenzt die Summe der abgeschlossenen Base-Backup-Archive auf 4 GiB und erfasst zusätzlich die tatsächliche Größe des vollständigen `k3s-postgres`-Barman-Prefixes einschließlich Metadaten und WAL für die Aggregatprüfung der drei Backup-Buckets;
- der CronJob verwendet die S3-kompatible R2-API nur zum Auflisten, Zuordnen und Summieren der Objektgrößen. Er löscht niemals einzelne R2-Objekte direkt;
- überschreitet die Summe der Base-Backups 4 GiB, wählt der CronJob ausschließlich das älteste erfolgreich abgeschlossene, nicht geschützte Base-Backup aus und führt zuerst `barman-cloud-backup-delete --dry-run --backup-id <id> --minimum-redundancy 2` aus. Nur wenn Katalog, Dry-Run und erwartete Backup-ID übereinstimmen, wird dasselbe offizielle Kommando ohne `--dry-run` ausgeführt;
- Barman entfernt damit das vollständige Backup und nur diejenigen zugehörigen WAL-Dateien, die vor dem ältesten verbleibenden Backup liegen und von keinem verbleibenden oder als archival markierten Backup benötigt werden. Nach jeder einzelnen Löschung werden Katalog, Base-Backup-Summe und tatsächliche Prefixgröße neu eingelesen. Die Bereinigung endet, sobald das 4-GiB-Base-Backup-Budget eingehalten ist oder nur noch zwei Backups verbleiben;
- mindestens die zwei neuesten erfolgreich abgeschlossenen Base-Backups bleiben unabhängig vom Budget erhalten. Sind die Backups klein, bleiben alle weiteren Backups erhalten, solange ihre gemeinsame Größe 4 GiB nicht überschreitet. Laufende Backups, unbekannte Katalogzustände, ein nicht aktueller WAL-Archiver, fehlgeschlagene Prüfungen oder ein PostgreSQL-Restore-Test, dessen letzter Erfolg älter als acht Tage ist, sperren jede Löschung und lösen einen Alert aus. `postgresql-size-retention` liest dafür ausschließlich die Lease `backup-validation/postgresql-restore-status`; nur `result=success` und ein höchstens acht Tage altes `renewTime` öffnen das Lösch-Gate. `size-retention.yaml` besitzt das dafür erforderliche namespace-übergreifende `get`-RBAC und keine Schreibrechte auf die Lease; der reguläre PostgreSQL-Restore läuft wöchentlich;
- die 4 GiB sind eine operative Softgrenze: Während ein neues Base-Backup vollständig geschrieben und verifiziert wird, darf die Belegung vor der sicheren Löschung des ältesten Backups vorübergehend darüber liegen; ein altes Backup wird niemals vor dem erfolgreichen Abschluss seines Nachfolgers gelöscht;
- kann die 4-GiB-Base-Backup-Summe wegen der zwei Mindestbackups nicht eingehalten werden, löscht der CronJob nicht weiter, meldet `budget blocked by minimum redundancy` und fordert eine bewusste Budgeterhöhung. Die Mindestredundanz wird nicht automatisch geopfert;
- der CronJob läuft mit `concurrencyPolicy: Forbid`, gepinntem Barman-Cloud-Image, begrenzter Laufzeit und denselben über VSO materialisierten PostgreSQL-R2-Credentials. Zugangsdaten erscheinen weder in Argumenten noch Logs;
- die Implementierung bleibt ein kleiner, unit-getesteter Policy-Adapter um unterstützte Schnittstellen und kein eigener Controller: Das Barman-Cloud-Plugin bietet nativ nur zeitbasierte Recovery-Window-Retention, während `barman-cloud-backup-delete` offiziell Backup-ID, `--minimum-redundancy`, `--dry-run` und die sichere Entfernung unbenutzter WAL-Dateien unterstützt. Diese belegte Funktionslücke rechtfertigt den Adapter. Katalog-/JSON-Auswertung, Größenberechnung und Löschschleife liegen in `size-retention.py`, nicht in einer ungetesteten Inline-Shell-Pipeline;
- Retention, Restore und Point-in-Time-Recovery werden explizit getestet;
- K8up darf CloudNativePG-PVCs nicht zusätzlich als vermeintlich konsistentes PostgreSQL-Backup sichern.

### 6.3 Datenbank-Credentials mit VSO

- OpenBao Database Secrets Engine verwaltet die DB-Verbindung und die Rollen.
- Crossplane verwaltet die getesteten Connection- und Role-CRs.
- Namespace-lokale VSO-Ressourcen (`VaultConnection`, `VaultAuth`, `VaultStaticSecret` oder `VaultDynamicSecret`) lesen nur die jeweils freigegebenen OpenBao-Pfade und materialisieren Kubernetes-Secrets für die Workloads.
- Grafana und Radar verwenden verbindlich OpenBao-Database-Static-Roles mit 30-tägiger Rotation. Deren geprüfte PostgreSQL-`rotationStatements` erstellen den festen Login beim ersten Lauf, falls er noch nicht existiert, und ändern ihn danach ausschließlich per `ALTER ROLE`. Erst in der folgenden Resource-Wave weist die konsumierende CNPG-`Database` diesen Login als Owner zu. Echte pro Lease erzeugte Dynamic Roles sind nicht Teil dieses Aufbaus. Trotz des CR-Namens `VaultDynamicSecret` und `allowStaticCreds: true` muss die Dokumentation diese statische Semantik korrekt benennen.
- VSO löst bei Credential-Rotation einen deklarativ konfigurierten Workload-Rollout aus, wenn die Anwendung das Kubernetes-Secret nicht selbst erneut lädt.

## 7. Backups und Restore

Es gibt exakt drei logisch und namentlich getrennte Buckets und Credential-Sätze:

| Zweck | Zielname | Mechanismus | Credential-Pfad in OpenBao |
|---|---|---|---|
| PostgreSQL | `cloudnativepg-backups` | Barman Cloud: WAL + Base-Backups | `kv/postgresql/r2-credentials` |
| OpenBao | `openbao-backups` | CronJob über die native Raft-Snapshot-API | `kv/openbao-snapshots/r2-credentials` |
| Workload-PVCs | `k8up-volumes` | K8up | `kv/k8up/r2-credentials` plus separates Repository-Passwort |

Regeln:

- Die drei Bucketnamen müssen verschieden sein.
- Ansible fragt alle Zugangsschlüssel interaktiv und verdeckt ab. Keine Zugangsdaten in Git, Variablen-Dateien, Logs oder Kommandoargumenten.
- K8up dient allgemeinen Workload-PVCs. OpenBao-Raft und PostgreSQL besitzen ihre anwendungskonsistenten eigenen Backupwege.
- Ein erfolgreicher Backup-Job ist kein Restore-Nachweis.
- OpenBao und K8up halten mindestens 7 tägliche, 4 wöchentliche und 6 monatliche erfolgreiche Wiederherstellungspunkte. Jede K8up-`Schedule` führt den Backup täglich aus, einen Repository-Check wöchentlich und einen exklusiven Prune wöchentlich zeitlich getrennt vom Backup. Der Prune setzt Restic nativ mit `keepDaily: 7`, `keepWeekly: 4` und `keepMonthly: 6` um; K8up stellt bei geplanten Jobs die Exklusivität pro Repository sicher. Jeder Workload und `backup-validation` verwenden einen eigenen unveränderlichen Repository-Prefix im Bucket `k8up-volumes`; Backup, Check, Prune und Restore eines Eigentümers dürfen ausschließlich diesen Prefix verwenden. Diese Trennung verhindert kollidierende Prune-Eigentümerschaft, ist aber keine kryptografische Mandantengrenze: Alle K8up-Jobs verwenden bewusst denselben bucketgebundenen R2-Credential-Satz und dasselbe Repository-Passwort. Anwendungen erhalten die Secrets nicht als Workload-Mount; nur ihre K8up-Jobs referenzieren sie. Fehlgeschlagene oder stale Backup-, Check- und Prune-Läufe werden alarmiert.
- Der OpenBao-Snapshot-CronJob erzeugt täglich genau einen konsistenten Raft-Snapshot mit eindeutigem UTC-Zeitstempel und Prüfsumme. Er lädt dieselbe Datei immer als tägliches Objekt, sonntags zusätzlich als wöchentliches Objekt und am ersten Kalendertag zusätzlich als monatliches Objekt in getrennte Prefixe. Erst nach erfolgreichem Upload und Prüfsummenverifikation löscht er pro Prefix die ältesten darüber hinausgehenden Snapshotobjekte, sodass die neuesten 7 täglichen, 4 wöchentlichen und 6 monatlichen erfolgreichen Stände verbleiben. OpenBao stellt keine count-basierte Remote-Retention für Raft-Snapshots bereit; diese eng begrenzte, unit-getestete CronJob-Logik in `snapshot.py` schließt die belegte Funktionslücke. Die Objekte sind unabhängige Snapshotdateien ohne WAL-Kette, deshalb ist ihre gezielte Löschung zulässig. Altersbasierte R2-Lifecycle-Regeln dürfen diese Prefixe nicht verkürzen.
- PostgreSQL verwendet bewusst keine feste Anzahl und kein festes Recovery Window. Seine Retention ist größenbasiert: Die Summe aller abgeschlossenen Base-Backup-Archive darf 4 GiB belegen; mindestens die zwei neuesten erfolgreichen Base-Backups bleiben immer erhalten. Eine kleine Datenbank kann dadurch Hunderte, eine große Datenbank nur zwei Backups besitzen.
- WAL und Metadaten werden nicht in das 4-GiB-Base-Backup-Budget eingerechnet, weil sie eine zusammenhängende PITR-Kette bilden. Für OpenBao und K8up werden keine erfundenen festen Teilbudgets vorgegeben. Die Summe dieser drei Backup-Buckets ist die maßgebliche Backup-Aggregatgröße: 8 GiB sind die operative Softgrenze, ab 9 GiB wird kritisch alarmiert. `backup-validation` führt dafür regelmäßig genau ein read-only S3-Listing pro Bucket aus und veröffentlicht die Aggregatgröße; getrennte Prometheus-Regeln werten denselben Messwert bei 8 GiB als `warning` und bei 9 GiB als `critical` aus. Diese Messung behauptet keine accountweite Cloudflare-Belegung: Existieren weitere R2-Buckets im selben Account, müssen deren Größen bei der Free-Tier-Bewertung separat berücksichtigt werden. Die jeweiligen Mindest-Retentionen und die zwei PostgreSQL-Mindestbackups haben Vorrang vor automatischer Löschung; kann die Softgrenze deshalb nicht eingehalten werden, stoppt jede weitere automatische Reduktion an ihrer sicheren Mindestgrenze und verlangt eine bewusste Budgetentscheidung. Vor Aktivierung eines weiteren K8up-Workload-Schedules muss dessen erstes Vollbackup gegen die aktuelle Aggregatbelegung der drei Backup-Buckets geprüft werden; zusätzliche Buckets desselben Accounts bleiben eine separat zu bestätigende Free-Tier-Voraussetzung.
- Direkte S3-/R2-Löschungen im Barman-Prefix sind verboten. Der Size-Retention-Job darf lediglich vollständige, abgeschlossene Backup-IDs über `barman-cloud-backup-delete` entfernen; Barman entscheidet selbst über anschließend unbenutzte WAL-Dateien. Eine Lifecycle-Regel darf den Barman-Prefix daher nicht altersbasiert löschen.
- Der einzelne tägliche OpenBao-Snapshot-CronJob läuft mit `concurrencyPolicy: Forbid`; ein fehlgeschlagener zusätzlicher Wochen- oder Monatsupload macht den Lauf insgesamt fehlerhaft und wird alarmiert.
- PostgreSQL verwendet kontinuierliche WAL-Archivierung und tägliche Base-Backups. Das garantierte PITR-Fenster beginnt mit dem ältesten der mindestens zwei verbleibenden Base-Backups und reicht bei lückenloser Archivierung bis zum neuesten erfolgreich archivierten WAL. Das Speicherbudget hat ausdrücklich Vorrang vor einer sechsmonatigen PostgreSQL-Historie.
- Jede Woche wird eine isolierte Restore-Abnahme mit temporärem Ziel, Integritätsprüfung und datierter Dokumentation ausgeführt. PostgreSQL und K8up laufen zeitgesteuert über `backup-validation`. OpenBao wird mindestens monatlich über den geschützten Bootstrap-Restoreablauf praktisch geprüft, weil dafür extern verwahrtes Recovery-/Adminmaterial erforderlich ist; ein fälliger oder fehlgeschlagener Lauf alarmiert, wird aber nicht durch im Cluster gespeicherte Root-Credentials automatisiert.
- Ein Restore darf niemals laufende Produktionsdaten überschreiben.

## 8. Radar als Referenz-Workload

Radar gehört ausdrücklich zur Spezifikation und dient als konkrete Integrationsabnahme:

- gepinntes Upstream-Helm-Chart;
- zunächst genau eine Replica;
- externe `HTTPRoute` über den gemeinsamen Traefik-Gateway-Listener `websecure`;
- OpenBao als OIDC-Provider mit deklarativem OIDC-Key, Scope, Client und Provider;
- ein browsererreichbarer OpenBao-Issuer über eine eigene `HTTPRoute`, öffentlichen DNS-Namen und das wildcard TLS-Secret des `websecure`-Listeners;
- exakte Radar-Callback-URL `https://<radar-host>/auth/callback`; OIDC-Metadaten, Login und Logout verwenden ausschließlich den externen HTTPS-Issuer;
- PostgreSQL-Datenbank `radar` im zentralen CNPG-Cluster;
- DB-Credentials und weitere benötigte Secrets über HashiCorp VSO;
- Timeline-Aufbewahrung 30 Tage;
- Radar-eigene Datenbank liegt innerhalb des zentralen PostgreSQL-Backup-/Restore-Bereichs;
- Istio-mTLS im explizit ausgewählten Workload-Namespace.

Abnahme: HTTPS, OIDC-Login, Datenbankverbindung, Credential-Rotation, Timeline-Persistenz und Wiederanlauf nach Pod-Neustart funktionieren. Radar wird nicht als HA bezeichnet, solange es single-replica bleibt.

## 9. Weitere Workloads

Für neue Anwendungen werden nur app-spezifische Angaben ergänzt:

- offizieller Helm-Chart mit knappen Values oder native Manifeste;
- eine HTTPRoute zum gemeinsamen Gateway;
- OIDC-Client in OpenBao über getestete Crossplane-CRs;
- Secrets über OpenBao und namespace-lokale HashiCorp-VSO-Ressourcen;
- bei Bedarf CNPG-Database plus OpenBao-DB-Rolle;
- bei Bedarf eine eigene Redis-Instanz oder einen Redis-Cluster über die CRDs eines gepflegten Redis-Operators; ein gemeinsames Redis mit nummerierten logischen Datenbanken wird nicht als Mandanten- oder HA-Modell verwendet;
- explizite Aufnahme des Namespace in Istio, sobald das Mesh eingeführt ist;
- K8up-Schedule nur für geeignete Workload-PVCs.

Keine eigene Application-CRD oder Crossplane-Composition ist Pflicht. Sie darf erst eingeführt werden, wenn sie nachweislich einfacher als kleine lesbare Ressourcen ist.

## 10. Gateway, TLS und mTLS

- Traefik bleibt der einzige Edge-Controller.
- Gateway API ist für neue HTTP-Routen verbindlich.
- Das gemeinsame Gateway und sein `websecure`-Listener werden einmalig auf Plattformebene verwaltet.
- cert-manager verwendet Cloudflare DNS-01 für öffentliche Zertifikate.
- Istio läuft im Ambient-Modus und übernimmt internes mTLS, während Traefik alleiniger Edge-Controller bleibt.
- `ztunnel` stellt L4-mTLS auf den Nodes bereit. Waypoint-Proxies werden nur installiert, wenn ein Workload nachweislich L7-Routing oder L7-Policies benötigt.
- Alle von Infra oder Apps verwalteten normalen Plattform- und Application-Namespaces werden standardmäßig mit dem offiziellen Istio-Label `istio.io/dataplane-mode=ambient` aufgenommen. Dazu gehören unter anderem `argocd`, `cert-manager`, `k8up`, `openbao`, `crossplane-system`, `vault-secrets-operator`, `cloudnative-pg`, `kube-prometheus-stack`, `radar` und spätere Redis-/Application-Namespaces.
- Ausgenommen bleiben nur host- und meshnahe Systembereiche: `istio-system`, `longhorn` und `kube-system`. Damit entstehen keine Bootstrap-Abhängigkeiten für ztunnel/Istiod, CSI/Storage, CoreDNS, K3s-Systempods oder ServiceLB.
- Innerhalb von `kube-system` erhalten ausschließlich die Traefik-Pods über die unterstützten K3s-Traefik-Helm-Values gezielt `istio.io/dataplane-mode=ambient`, damit ihre Backend-Verbindungen zu Ambient-Workloads mTLS verwenden.
- Die OpenBao-Application besitzt `peer-authentication.yaml`. Nach Aufnahme aller OpenBao-Clients setzt diese Ressource für `openbao` eine `PeerAuthentication` im Modus `STRICT`. Die Abnahme muss beweisen, dass Crossplane, VSO, cert-manager, Snapshot-CronJob und Traefik OpenBao weiter erreichen und ein nicht aufgenommenes Test-Pod abgewiesen wird.
- Istio dokumentiert `istio.io/dataplane-mode=ambient` ausdrücklich sowohl für Namespaces als auch für einzelne Pods. Der von K3s verwendete Traefik-Helm-Chart unterstützt zusätzliche Pod-Labels über `deployment.podLabels`; darüber wird nur Traefik in `kube-system` aufgenommen. Ein Render-Test muss das Label am Traefik-PodTemplate vor dem Deployment nachweisen. Die gepinnte Istio-Version wird zusätzlich gegen K3s, ARM64 und Flannel geprüft.
- Ein Integrationstest muss externes HTTPS und internes Ambient-mTLS zwischen zwei Beispielworkloads getrennt nachweisen.

## 11. Logging und Monitoring

### 11.1 Logging

- Fluent Bit läuft als DaemonSet auf allen drei Nodes und versieht Logs mit Namespace-, Pod-, Container- und Node-Metadaten.
- Fluent Bit leitet an das zentrale rsyslog auf dem OMV-Host weiter.
- Collector und Backend bleiben getrennt: Ein späterer Wechsel zu Loki erfordert nur einen zusätzlichen oder geänderten Fluent-Bit-Output und keinen Austausch des Collectors.
- Historische Logs müssen auf OMV per CLI über `journalctl` beziehungsweise das konfigurierte rsyslog-Dateiziel abrufbar sein.
- Kubernetes Events, Pod-Status und `OOMKilled` werden bei gewünschter Historie separat erfasst; Container-stdout/stderr allein genügt nicht.
- Es wird kein separates Log-Backupziel eingerichtet. Die Logs liegen auf dem OMV-Host und werden durch dessen vorhandene tägliche Hostsicherung erfasst. Der One-Shot-Lauf verifiziert nur, dass das konkrete rsyslog-Speicherziel in dieser Hostsicherung enthalten ist; er verändert die OMV-Backupkonfiguration nicht.

### 11.2 Monitoring

- kube-prometheus-stack liefert historische Metriken und Alerts.
- Prometheus behält 15 Tage beziehungsweise höchstens 18 GB auf einem 20-GiB-PVC.
- Prometheus, Alertmanager und Grafana laufen jeweils mit zwei Replikaten, verteilt über Nodes mit `monitoring=true`. Sie dürfen nicht auf `raspi4` laufen. Node Exporter und erforderliche node-lokale DaemonSets bleiben dagegen auf dem Pi aktiv.
- Vor dem Deployment weist ein Ressourcenbudget nach, dass beide Standard-Nodes gleichzeitig je eine Prometheus-, Alertmanager- und Grafana-Replica zusätzlich zu ihren übrigen Plattformaufgaben tragen können. Prometheus-Ingestion und logischer TSDB-Speicher verdoppeln sich clusterweit, verteilen sich aber auf die beiden Nodes; pro Node läuft regulär genau eine Replica.
- Vorhandene Dashboards werden verwendet. Eigene Dashboards sind keine Startvoraussetzung.
- Alerts mindestens für PVC-Füllstand, fehlgeschlagene/stale Backups, PostgreSQL-Zustand und OpenBao-Raft/Snapshot-Zustand. kube-state-metrics exportiert über eine eng begrenzte Label-Allowlist die Ergebnislabels der Leases `backup-validation/openbao-restore-status` und `backup-validation/postgresql-restore-status`, damit die zugehörigen Prometheus-Regeln Fehler und Überalterung erkennen können.
- Die API-Server-ServiceMonitor-Konfiguration behält die vorhandene Histogram-Bucket-Ausdünnung und filtert die sechs nachweislich ungenutzten hochkardinalen Familien `apiserver_watch_events_dispatch_duration_seconds`, `apiserver_storage_list_duration_seconds`, `apiserver_watch_list_duration_seconds`, `apiserver_watch_cache_read_wait_seconds`, `apiserver_watch_cache_initialization_duration_seconds` und `etcd_request_duration_seconds`, jeweils einschließlich `_bucket`, `_sum` und `_count`.
- Vor Aktivierung weist ein Render-/Regel-/Dashboard-Test nach, dass keine geladene Rule oder mitgelieferte Dashboard-Abfrage diese Familien benötigt. API-Server-Targets, SLO-/Availability-Metriken und Storage-Alerts müssen danach gesund bleiben.
- Die Abnahme dokumentiert Scrape-Samples vor/nach Relabeling und den warmen TSDB-Zustand. Eine sofort unveränderte Head-Series-Zahl ist wegen WAL/Head-Compaction kein Fehlschlag und ein kalter Post-Restart-Wert kein belastbarer RAM-Erfolg.

## 12. Verbindliche Repository- und Benamungsregeln

### 12.1 Infra-Waves und Ownership

Nur Infra codiert die Plattform-Abhängigkeiten in der ersten Verzeichnisebene. Der verbindliche Pfad lautet:

```text
<NN>-<phase>/<workload>/<datei>
```

Verbindliche Plattform-Waves:

| Wave | Phase | Workloads/Applications |
|---:|---|---|
| 1 | `01-bootstrap` | `argocd-config`, `prometheus-operator-crds` |
| 2 | `02-controllers` | `cert-manager`, `k8up`, `longhorn`, `crossplane`, `vault-secrets-operator`, `cloudnative-pg`, `barman-cloud`, `redis-operator`, `istio-ambient` |
| 3 | `03-controllers` | `provider-vault` |
| 4 | `04-core` | `openbao` |
| 5 | `05-platform` | `provider-vault-connection` |
| 6 | `06-platform` | `openbao-config`, `openbao-pki`, `openbao-backups`, `letsencrypt-dns01` |
| 7 | `07-data` | `postgresql` |
| 8 | `08-observability` | `fluent-bit`, `kube-prometheus-stack`, `backup-validation` |

Die Infra-Root hat Wave 0 und darf als einziges Root-Dokument außerhalb dieses Schemas liegen. Applications derselben Wave starten unabhängig; alphabetische Namen erzeugen keine Reihenfolge. Spätere Waves benötigen echte Health-/Readiness-Gates, nicht nur vorhandene CRs.

### 12.2 Live- und Apps-Pfade

Live dupliziert keine Waves, weil es keine Deployment-Reihenfolge besitzt. Es behält die vorhandene Trennung: gemeinsame private Werte in `clusters/omv/values.yml`, genau ein privater Helm-Values-Satz als flache Datei und explizite private Kubernetes-Ressourcen in einem Workload-Unterverzeichnis:

```text
clusters/omv/values.yml
clusters/omv/<workload>-values.yml
clusters/omv/<workload>/<ressource>.yaml
```

Die Workloadnamen müssen mit Infra beziehungsweise Apps übereinstimmen. Beispiele sind `clusters/omv/openbao-values.yml`, `clusters/omv/radar-values.yml` und `clusters/omv/postgresql/backup-object-store.yaml`. Ein Unterverzeichnis wird nicht nur für eine zweite Values-Datei angelegt.

Apps besitzt die Root-Datei `apps.yml` und unmittelbar darunter ein Verzeichnis pro Workload; es dupliziert die Infra-Waves nicht:

```text
apps.yml
<workload>/app.yml
<workload>/<ressource>.yaml
```

Radar liegt folglich unter `radar/app.yml`. Seine Aktivierung erfolgt erst nach Infra-Abnahme. Reihenfolge innerhalb einer Anwendung wird über echte Argo-/Kubernetes-Abhängigkeiten und erforderlichenfalls Resource-Waves abgebildet, nicht durch nummerierte Apps-Verzeichnisse.

### 12.3 Dateien und Ressourcennamen

1. Jede Child-Application liegt im Verzeichnis ihres Workloads und heißt `app.yml`, zum Beispiel `radar/app.yml` oder `cloudnative-pg/app.yml`. Nur die beiden Root-Dateien `infra.yml` und `apps.yml` liegen außerhalb eines Workloadverzeichnisses. Workloadpräfixe wie `radar-application.yml` sind verboten, weil das Verzeichnis den Kontext bereits liefert.
2. Andere Dateien sind so kurz wie möglich und so aussagekräftig wie nötig. Nur Informationen, die bereits eindeutig aus dem Verzeichnis hervorgehen, werden entfernt; fachlich relevante Qualifikatoren bleiben erhalten. Deshalb wird beispielsweise `radar/radar-route.yaml` zu `radar/route.yaml`, aber `radar/radar-postgres-access.yaml` zu `radar/postgres-access.yaml` und nicht zu `radar/access.yaml`. Präzise Namen wie `kubernetes-api-egress.yaml`, `openbao-client-ca.yaml`, `backup-object-store.yaml`, `daily-backup.yaml`, `session-secret.yaml` und `postgres-access.yaml` bleiben erhalten. Implementierungsnamen wie `vso.yaml` werden nur verwendet, wenn die Datei tatsächlich die Komponente statt den fachlichen Zweck beschreibt.
3. Ein einzelner Helm-Values-Satz heißt `values.yml`. Erst mehrere Values-Sätze im selben Workload erhalten unterscheidende Zwecknamen wie `operator-values.yml` und `backup-plugin-values.yml`.
4. Kubernetes-Ressourcen, Argo Applications, Namespaces, ServiceAccounts, OpenBao-Rollen und Policies verwenden lowercase kebab-case. PostgreSQL-Identifier dürfen die von PostgreSQL benötigte Schreibweise verwenden.
5. Argo-CD-Application-Dateien und Helm-Values verwenden `.yml`; verfasste Kubernetes-Ressourcen verwenden `.yaml`. Strukturell vorgegebene Namen wie `Chart.yaml` und `kustomization.yaml` behalten ihre erforderliche Schreibweise.
6. Ein Workload darf in Infra und Live je einen Anteil besitzen; beide Anteile müssen denselben Workloadnamen verwenden und werden von genau einer Application explizit als Sources aufgeführt.
7. Controller-Ressourcen bleiben beim Controller, Consumer-Ressourcen beim Consumer. Insbesondere liegen `VaultAuth`/Secret-CR, OIDC, Database, Redis und K8up-Schedule bei der konsumierenden Anwendung.
8. Kustomize ist nur mit dokumentierter Transformation erlaubt, nicht als reine Dateiliste.
9. Upstream-Charts werden nicht gerendert eingecheckt. Versionen, Images, Actions und Provider sind unveränderlich gepinnt.
10. Leere Verzeichnisse, Platzhalter und parallele Namen für dieselbe Capability sind verboten.

### 12.4 Kanonische Capability-Namen

- `argocd-config`: fortlaufende Argo-CD-Konfiguration nach der create-once Installation;
- `prometheus-operator-crds`: vorgezogene Monitoring-CRDs ohne Workloads;
- `cert-manager`, `k8up`, `longhorn`, `crossplane`, `redis-operator` und `istio-ambient`: jeweils eigener Plattform-Controller beziehungsweise Runtime;
- `cloudnative-pg`: nur CNPG-Operator;
- `barman-cloud`: nur Backup-Plugin;
- `vault-secrets-operator`: nur VSO-Controller;
- `provider-vault`: Provider-Package, Runtime und Aktivierung;
- `openbao`: ausschließlich Runtime und Raft;
- `provider-vault-connection`: ausschließlich ProviderConfig nach OpenBao;
- `openbao-config`: gemeinsame deklarative OpenBao-Mounts, Policies und Password Policies;
- `openbao-pki`: cert-manager-Integration nach PKI-Ceremony;
- `openbao-backups`: nativer Raft-Snapshot-CronJob, OpenBao-Zugriff und R2-Ziel;
- `letsencrypt-dns01`: Cloudflare-Credential-Vertrag und öffentliche Issuer;
- `postgresql`: zentraler Cluster, Manager-Rolle, ObjectStore und Backups; anwendungsspezifische Databases und DB-Rollen liegen beim Verbraucher;
- `fluent-bit`: Kubernetes-Logcollector;
- `kube-prometheus-stack`: Monitoring-Stack einschließlich Grafana-Verbraucherressourcen;
- `backup-validation`: Aggregatprüfung der drei Backup-Buckets und isolierte Restore-Abnahmen;
- `radar`: Referenzanwendung in Apps.

Infra, Live, Apps und Bootstrap erhalten Tests, die Pfade, Infra-Application-Waves, die flache Apps-Konvention `apps.yml` plus `<workload>/app.yml`, Source-Reihenfolge, eindeutige Eigentümerschaft, publizierbare Infra-Inhalte und fehlende Secretwerte prüfen.

### 12.5 Verbindlicher Application- und Source-Baum

Der folgende Baum ist der vollständige Mindestbestand der ownership-tragenden Root-/Application-/Konfigurationsbereiche. Rollen dürfen intern weitere Task- und Testdateien besitzen; sie dürfen dadurch keine zweite Ressourceneigentümerschaft einführen.

Bootstrap:

```text
k3s-on-omv-bootstrap/
├── ansible/
│   ├── site.yml
│   ├── gitops-bootstrap.yml
│   ├── inventory/
│   │   ├── hosts.yml
│   │   └── group_vars/all/versions.yml
│   ├── roles/
│   │   ├── k3s_cluster/
│   │   ├── gitops_bootstrap/
│   │   ├── openbao_db_passwords/
│   │   └── openbao_pki_bootstrap/
│   └── tests/
└── docs/
    ├── gitops.md
    ├── gateway.md
    └── recovery/
```

Bestehende Rollen werden erweitert, statt parallele Rollen für denselben Lebenszyklus einzuführen. `site.yml` endet an der K3s-/Hostgrenze. `gitops-bootstrap.yml` ist der einzige geschützte Einstieg für Argo-/OpenBao-/Credential-/Restore-Abnahmen.

Infra:

```text
k3s-on-omv-infra/
├── infra.yml
├── 01-bootstrap/
│   ├── argocd-config/
│   │   ├── app.yml
│   │   ├── Chart.yaml
│   │   ├── values.yml
│   │   └── application-health.yaml
│   └── prometheus-operator-crds/
│       └── app.yml
├── 02-controllers/
│   ├── barman-cloud/
│   │   ├── app.yml
│   │   └── values.yml
│   ├── cert-manager/
│   │   ├── app.yml
│   │   └── values.yml
│   ├── cloudnative-pg/
│   │   ├── app.yml
│   │   └── values.yml
│   ├── crossplane/
│   │   ├── app.yml
│   │   └── values.yml
│   ├── istio-ambient/
│   │   ├── app.yml
│   │   └── values.yml
│   ├── k8up/
│   │   ├── app.yml
│   │   └── values.yml
│   ├── longhorn/
│   │   ├── app.yml
│   │   ├── values.yml
│   │   ├── storage-class.yaml
│   │   ├── postgres-storage-class.yaml
│   │   └── monitoring-storage-class.yaml
│   ├── redis-operator/
│   │   ├── app.yml
│   │   └── values.yml
│   └── vault-secrets-operator/
│       ├── app.yml
│       └── values.yml
├── 03-controllers/
│   └── provider-vault/
│       ├── app.yml
│       ├── values.yml
│       ├── activation-policy.yaml
│       ├── provider.yaml
│       └── runtime.yaml
├── 04-core/
│   └── openbao/
│       ├── app.yml
│       ├── values.yml
│       └── peer-authentication.yaml
├── 05-platform/
│   └── provider-vault-connection/
│       ├── app.yml
│       └── provider-config.yaml
├── 06-platform/
│   ├── letsencrypt-dns01/
│   │   ├── app.yml
│   │   └── credentials.yaml
│   ├── openbao-config/
│   │   ├── app.yml
│   │   ├── mounts.yaml
│   │   ├── password-policies.yaml
│   │   └── policies.yaml
│   ├── openbao-backups/
│   │   ├── app.yml
│   │   ├── kustomization.yaml
│   │   ├── snapshot.py
│   │   └── snapshot.yaml
│   └── openbao-pki/
│       ├── app.yml
│       ├── backend-config.yaml
│       ├── issuer-identities.yaml
│       ├── issuers.yaml
│       └── token-request-rbac.yaml
├── 07-data/
│   └── postgresql/
│       ├── app.yml
│       ├── cluster.yaml
│       ├── server-tls.yaml
│       ├── backup.yaml
│       ├── kustomization.yaml
│       ├── size-retention.py
│       ├── size-retention.yaml
│       └── credentials.yaml
├── 08-observability/
│   ├── backup-validation/
│   │   ├── app.yml
│   │   ├── access.yaml
│   │   ├── kustomization.yaml
│   │   ├── capacity-check.py
│   │   ├── capacity-check.yaml
│   │   ├── k8up-restore.py
│   │   ├── restore-schedules.yaml
│   │   └── restore-status.yaml
│   ├── fluent-bit/
│   │   ├── app.yml
│   │   └── values.yml
│   └── kube-prometheus-stack/
│       ├── app.yml
│       ├── values.yml
│       └── grafana/
│           ├── database.yaml
│           ├── postgres-access.yaml
│           └── credentials.yaml
├── tests/
└── values-schemas/
```

Die jeweils benachbarte `kustomization.yaml` erzeugt aus den Python-Dateien checksum-basierte ConfigMaps; die Adapter werden daraus read-only in ein gepinntes, ARM64-/AMD64-kompatibles Image gemountet, dessen Runtime und benötigte offizielle CLIs/Libraries vorab nachgewiesen werden. Laufzeitinstallationen aus dem Internet sind verboten. Reicht kein vorhandenes gepflegtes Image aus, wird ein minimales Adapter-Image aus gepinnten, checksum-geprüften Abhängigkeiten gebaut und unveränderlich per Digest referenziert. Die Adapter besitzen Unit-Tests unter `tests/` und werden nicht als unversionierte Inline-Shell in CronJobs dupliziert. `snapshot.yaml`, `credentials.yaml`, `access.yaml` und ähnliche Sammeldateien enthalten jeweils nur die eng zusammengehörigen Ressourcen ihres bezeichneten Zwecks. Sobald ein Chart dieselbe Funktion nativ und lesbar bereitstellt, verbleibt sie in `values.yml` und die zusätzliche Datei entfällt; leere Platzhalterdateien sind verboten.

Live:

```text
k3s-on-omv-live/
├── clusters/omv/
│   ├── values.yml
│   ├── cert-manager-values.yml
│   ├── fluent-bit-values.yml
│   ├── kube-prometheus-stack-values.yml
│   ├── longhorn-values.yml
│   ├── openbao-values.yml
│   ├── radar-values.yml
│   ├── backup-validation/
│   │   └── backup-target.yaml
│   ├── letsencrypt-dns01/
│   │   └── issuers.yaml
│   ├── openbao/
│   │   └── route.yaml
│   ├── openbao-backups/
│   │   └── snapshot-target.yaml
│   └── postgresql/
│       ├── backup-budget.yaml
│       └── backup-object-store.yaml
└── tests/
```

Nur tatsächlich benötigte private Values-Dateien werden angelegt. Fehlt einer Komponente ein privater Wert, existiert dafür keine leere Live-Datei. Reale Credentials bleiben trotz dieses Baums ausschließlich in OpenBao beziehungsweise externer Bootstrap-Custody.

Apps:

```text
k3s-on-omv-apps/
├── apps.yml
├── radar/
│   ├── app.yml
│   ├── values.yml
│   ├── database.yaml
│   ├── postgres-access.yaml
│   ├── credentials.yaml
│   └── openbao-oidc.yaml
└── tests/
```

Radar konfiguriert seine native Gateway-API-Route im gepinnten Chart über `values.yml`; deshalb existiert keine zusätzliche `route.yaml`. Falls die geprüfte Chartversion keine gleichwertige native Route unterstützt, wird genau `radar/route.yaml` ergänzt. `credentials.yaml` bezeichnet den fachlichen Secret-Bereitstellungsvertrag; der Dateiname wird nicht nach VSO benannt. Weitere Anwendungen folgen demselben flachen Muster und erhalten nur Dateien für tatsächlich benötigte Fähigkeiten.

Application-Inventar und Quellen:

- `infra.yml` selektiert `01-bootstrap/*/app.yml` bis `08-observability/*/app.yml` sowie exakt `01-bootstrap/argocd-config/application-health.yaml`, das vor dem ersten Child-Application-Health-Gate angewendet und danach fortlaufend von Infra besessen wird.
- `apps.yml` selektiert ausschließlich `<workload>/app.yml` im Apps-Repository.
- Jede Infra-Child-Application verwendet nur Infra und ausdrücklich benötigte Live-Quellen. Insbesondere referenziert `openbao/app.yml` die private `openbao/route.yaml`, `openbao-backups/app.yml` das private `openbao-backups/snapshot-target.yaml`, `backup-validation/app.yml` das private `backup-validation/backup-target.yaml` und `letsencrypt-dns01/app.yml` die privaten Issuer-Werte/-Ressourcen.
- Jede Apps-Child-Application verwendet nur das gepinnte Upstream-Artefakt, Apps und ausdrücklich benötigte Live-Quellen.
- Weder Infra noch Apps selektieren Manifeste des jeweils anderen Repositories. Plattformabhängigkeiten werden über APIs und Health-Gates erfüllt, nicht durch fremde Dateieigentümerschaft.

## 13. One-Shot-Ausführungsvertrag

Die Architekturentscheidungen für den ersten Aufbau sind festgelegt. Die AI fragt nur tatsächlich geheime oder clusterspezifische Werte interaktiv ab, insbesondere Repository-Zugang, Cloudflare-DNS-Token, drei R2-Credential-Sätze, private Hostnamen und den rsyslog-Endpunkt auf OMV. Live enthält die festgelegten nicht geheimen Backupbudgets: PostgreSQL strebt höchstens 4 GiB Base-Backup-Archive an, behält aber unabhängig davon mindestens zwei erfolgreiche Base-Backups; für die drei Backup-Buckets zusammen gelten 8 GiB als operative Softgrenze und 9 GiB als kritische Schwelle. OpenBao und K8up erhalten keine unbelegten festen Teilbudgets. Die AI darf keine Zugangsdaten erfinden oder in Git schreiben.

Die Infra-Application `backup-validation` besitzt dauerhaft das schmale Namespace `backup-validation`, die ServiceAccounts `k8up-validation-vso` und `backup-capacity-vso`, deren VSO-Ressourcen, die read-only Kapazitätsprüfung der drei Backup-Buckets und die zeitgesteuerten Restore-Abnahmen; sie besitzt keine Produktionsdaten. Jeder PostgreSQL-Restorelauf stellt in einem isolierten temporären Cluster einen vor dem Backup geschriebenen Canary wieder her, prüft zusätzlich einen konkreten PITR-Zeitpunkt per SQL und aktualisiert danach die runtime-eigene Lease `postgresql-restore-status` mit `renewTime`, Ergebnislabel und nicht geheimer Run-ID. Bei Fehler wird `result=failed` geschrieben. Die Lease ist ein ausschließlich vom Validierungsjob geschriebenes Runtime-Objekt und wird nicht von Argo reconciliiert; Argo besitzt über `access.yaml` nur das eng begrenzte Lease-Schreib-RBAC dieses Jobs. Der Retention-Job erhält ausschließlich `get`. Der One-Shot-Lauf prüft K8up dort unabhängig von späteren Anwendungen mit einem temporären PVC und einer bekannten Testdatei: Backup in den ausschließlich diesem Test gehörenden Repository-Prefix, Restore in ein zweites temporäres PVC und Prüfsummenvergleich. Jeder spätere zeitgesteuerte Validierungslauf wiederholt den vollständigen Zyklus mit neuem Canary und frisch erzeugten PVCs; er restauriert niemals nur dauerhaft denselben alten Snapshot. Nach dokumentiertem Erfolg werden Test-Workloads und ihre PVCs gelöscht; die durch `Retain` verbliebenen PVs und Longhorn-Volumes werden nur anhand der vorab aufgezeichneten Validierungs-UIDs und erst im detached Zustand entfernt. Namespace, ServiceAccounts, VSO-Verträge und Restore-Schedules bleiben für die wiederkehrende Abnahme erhalten. Dauerhafte K8up-Schedules sowie ihre namespace-lokalen ServiceAccounts, VSO-Ressourcen, eindeutigen Repository-Prefixe und OpenBao-Rollen werden zusammen mit jedem Workload angelegt, der tatsächlich persistente PVC-Daten besitzt.

Die 7/4/6-Auswahl wird ohne Zeitreise ausschließlich in Validierungs-Prefixen geprüft. Für K8up erzeugt das gepinnte Restic-Werkzeug Fixture-Snapshots mit kontrollierten Zeitstempeln und verifiziert `forget --dry-run` gegen `keepDaily: 7`, `keepWeekly: 4` und `keepMonthly: 6`; der produktive K8up-Prefix bleibt unberührt. Für OpenBao legt der Test im Validierungs-Prefix checksum-validierte Fixture-Objekte mit kontrollierten UTC-Zeitstempeln an und prüft die count-basierte Auswahl zunächst im Dry-Run. Fixture-Objekte werden erst nach Vergleich der erwarteten IDs gelöscht. Diese Tests belegen die Auswahlalgorithmen; die reale altersbasierte Bestandsprüfung bleibt zusätzlich erforderlich.

Der geschützte OpenBao-Restoreablauf schreibt vor dem zu prüfenden Snapshot einen zufälligen bekannten Canary-Wert in einen ausschließlich dafür vorgesehenen KV-Pfad. Bootstrap lädt den verifizierten Snapshot mit dem vom Bediener aus externer Verwahrung bereitgestellten R2-Credential, erstellt ein isoliertes temporäres Restore-Namespace ohne externe Route oder Produktions-Service, kopiert das statische Seal-Material ohne Ausgabe in ein kurzlebiges Secret und stellt den Snapshot in einer separaten OpenBao-Instanz mit eigenem PVC wieder her. Der Bediener stellt das extern verwahrte Admin-/Recovery-Material verdeckt bereit; die Abnahme prüft Unseal, Raft-Lesbarkeit und den exakten Canary-Wert. Danach werden temporäre Instanz, Secret und Namespace gelöscht. Weil die verwendeten Longhorn-StorageClasses `Retain` setzen, zeichnet Bootstrap die UIDs aller Validierungs-PVCs/PVs vorab auf und entfernt anschließend ausschließlich die nachweislich zu diesem Lauf gehörenden, detached PVs und Longhorn-Volumes; ein bloßes PVC-Löschen gilt nicht als Cleanup. Kein Restore- oder Cleanup-Schritt adressiert Produktions-Service, Produktions-PVCs oder das laufende Raft-Cluster. Nach Erfolg oder Fehlschlag aktualisiert Bootstrap ausschließlich `renewTime`, Ergebnislabel und nicht geheime Run-ID der runtime-eigenen Lease `openbao-restore-status` im Namespace `backup-validation`. Diese Lease gehört nicht Argo und wird von keinem Git-Manifest reconciliiert; `restore-status.yaml` besitzt nur die erforderlichen Prometheus-Regeln für die PostgreSQL- und OpenBao-Restore-Leases; die eng auf beide Restore-Leases begrenzte kube-state-metrics-Label-Allowlist gehört den `kube-prometheus-stack`-Values. Die OpenBao-Regel alarmiert bei fehlender Lease, Fehlerlabel oder einem Alter von mehr als 35 Tagen; die PostgreSQL-Regel verwendet die in Abschnitt 7 festgelegte Acht-Tage-Grenze. Weder Token noch Snapshotinhalt gelangen in das Statusobjekt.

Der PostgreSQL-Size-Retention-Test verwendet niemals den produktiven Barman-Katalog. Der One-Shot erstellt dafür einen temporären kleinen CNPG-Cluster mit eigenem eindeutigen Barman-Servernamen unter einem isolierten `validation/<run-id>/`-Prefix desselben PostgreSQL-Buckets und erzeugt mindestens drei erfolgreiche Test-Base-Backups. Nur im Fixture werden nacheinander ein Budget unterhalb der Gesamtsumme, ein Budget unterhalb der Summe der zwei neuesten Backups und ein ausschließlich an den Fixture-Job injizierter künstlich veralteter Restore-Status gesetzt; die produktive Lease wird dabei weder verändert noch ersetzt. Damit prüft der Lauf Dry-Run-Zuordnung, Löschung des ältesten vollständigen Backups, `--minimum-redundancy 2`, Erhalt der erforderlichen WAL-Kette, die Minimum-Redundancy-Sperre und das stale-Restore-Gate. Erst danach wird die produktive Retention mit 4 GiB aktiviert. Nach erfolgreicher Abnahme löscht `barman-cloud-backup-delete --backup-id` ausschließlich für diesen eindeutig validierten Fixture-Server nacheinander alle verbliebenen Testbackups ohne gesetzte Mindestredundanz. Der Barman-Katalog muss danach leer sein. Verbleiben anschließend Objekte, ist eine direkte Löschung nur für den zuvor aufgezeichneten exakten `validation/<run-id>/`-Prefix zulässig, nachdem ein erneutes Barman-Listing bestätigt hat, dass dort kein Backup mehr registriert ist; produktive Prefixe bleiben ausnahmslos verboten. Die zugehörigen Retain-PVs und Longhorn-Volumes werden anschließend nach demselben UID-gebundenen Validierungs-Cleanup entfernt.

Der Redis-Ansatz ist festgelegt: Ein gepflegter, ARM64-kompatibler Operator stellt je konsumierendem Workload eine eigene Redis-Instanz oder einen eigenen Redis-Cluster über CRDs bereit. Die implementierende AI prüft vor der Auswahl anhand der Primärdokumentation aktive Pflege, unterstützte Kubernetes-Version, ARM64-Images, Persistenz, Backup/Restore und HA-Funktionen. Sie verwendet weder das Argo-CD-interne Redis noch nummerierte logische Redis-Datenbanken als Ersatz für getrennte Workload-Instanzen.

Der One-Shot prüft den gewählten Redis-Operator nach dessen Readiness in einem temporären Namespace: Er erstellt die kleinste unterstützte persistente Redis-CR, wartet auf Ready, schreibt und liest einen zufälligen Canary, löscht den Redis-Pod kontrolliert und liest den Canary nach Wiederanlauf erneut. Anschließend löscht er CR und Namespace und entfernt ausschließlich die über vorab aufgezeichnete UIDs zugeordneten detached Retain-PVs/Longhorn-Volumes. Die Abnahme verwendet keine Anwendungsnamespace und hinterlässt keine Redis-Testinstanz.

Vor jeder Implementierung prüft die AI Versionen und native Funktionen anhand der Primärdokumentation. Ist eine verbindliche Anforderung mit den ausgewählten Komponenten nicht unterstützt, stoppt sie vor dem Deployment und nennt genau den belegten Konflikt. Andernfalls führt sie den Aufbau ohne weitere Architektur-Rückfragen in der festgelegten Abhängigkeitsreihenfolge aus.

## 14. Definition of Done

- [ ] Drei K3s-Server sind Ready und embedded etcd hält einen kontrollierten Einzel-Node-Ausfall aus.
- [ ] K3s wurde ohne paketierten `local-storage`-Provisioner aufgebaut; `local-path-provisioner` und `local-path`-StorageClass existieren nicht.
- [ ] Longhorn provisioniert und reattached verschlüsselte PVCs; der Schlüssel liegt geschützt unter `$HOME/secrets/`.
- [ ] OpenBao läuft mit drei Raft-Votern auf drei Nodes und wird nach Pod-, Node- und Cluster-Neustart automatisch unsealed.
- [ ] Alle drei OpenBao-Daten-PVCs sind gebunden und auf die drei Nodes verteilt.
- [ ] Die konkreten OpenBao-Auth-Rollen und Policies entsprechen Abschnitt 5; die VSO-Rollen funktionieren.
- [ ] OpenBao-PKI besitzt Root, Service-Intermediate und Client-Intermediate; beide ClusterIssuer stellen testweise gültige Zertifikate aus.
- [ ] HashiCorp VSO und seine namespace-lokalen CRs sind funktionsfähig.
- [ ] CloudNativePG läuft mit drei Instanzen auf drei Nodes, synchroner Replica und verschlüsselten Einzelreplikat-PVCs.
- [ ] Barman Cloud archiviert WAL und erstellt tägliche Base-Backups; Restore und PITR sind nachgewiesen.
- [ ] Die PostgreSQL-Retention reduziert abgeschlossene Base-Backup-Archive auf zusammen höchstens 4 GiB, soweit dies unter Erhalt von mindestens zwei erfolgreichen Base-Backups möglich ist, löscht ausschließlich vollständige Backup-IDs über Barman, behält diese Mindestredundanz und entfernt nur nachweislich unbenutzte WAL-Dateien. Kleine Backups bleiben in entsprechend größerer Anzahl erhalten. Dry-Run, Budgetüberschreitung, Minimum-Redundancy-Sperre und fehlerhaftes/stales Restore-Gate sind am isolierten temporären Barman-Prefix statt am produktiven Katalog getestet.
- [ ] Die drei getrennten Buckets für PostgreSQL, OpenBao und K8up-Workloads werden mit getrennten Credentials verwendet; ihre aggregierte Belegung warnt ab 8 GiB und alarmiert kritisch ab 9 GiB. Weitere Buckets desselben Cloudflare-Accounts sind ausdrücklich nicht Teil dieses Messwerts.
- [ ] Beim One-Shot sind die OpenBao- und K8up-Retentionen auf 7 tägliche, 4 wöchentliche und 6 monatliche erfolgreiche Punkte konfiguriert, ihre Auswahl-/Prune-Logik ist mit isolierten Fixtures geprüft und der neueste verfügbare Stand jedes Backupwegs wurde wiederhergestellt. Die tatsächliche altersbasierte Bestandsprüfung verlangt zu jedem Zeitpunkt nur so viele Tages-/Wochen-/Monatsstände, wie seit Inbetriebnahme planmäßig entstehen konnten; spätestens nach sechs Monaten müssen 7/4/6 reale Stände vorhanden sein. PostgreSQL erfüllt stattdessen das konfigurierte Speicherbudget bei mindestens zwei vollständigen Backups. PostgreSQL und K8up werden zeitgesteuert isoliert wiederhergestellt; der geschützte OpenBao-Restore wird mindestens monatlich fällig gestellt und für jeden Backupweg mindestens einmal erfolgreich dokumentiert.
- [ ] Radar funktioniert mit HTTPRoute/TLS, OpenBao OIDC, VSO-Secret-Synchronisierung und PostgreSQL.
- [ ] Istio-mTLS funktioniert zwischen zwei Beispielworkloads, ohne Traefiks Edge-Ownership zu duplizieren.
- [ ] Der ausgewählte Redis-Operator hat im temporären Abnahmelauf eine eigene persistente Redis-Instanz bereitgestellt; Canary-Write/Read, kontrollierter Pod-Neustart, erneutes Lesen und UID-gebundener Cleanup waren erfolgreich.
- [ ] Fluent Bit liefert Logs aller Nodes an das zentrale rsyslog auf OMV; historische CLI-Abfragen funktionieren und das Speicherziel ist nachweislich von der täglichen OMV-Hostsicherung umfasst.
- [ ] Je zwei Prometheus-, Alertmanager- und Grafana-Replikate laufen verteilt auf den beiden `monitoring=true`-Nodes; Ausfall eines dieser Nodes unterbricht Metrikerfassung und Alerting nicht, der gemeinsame Grafana-Zustand bleibt verfügbar und der externe Grafana-Zugriff erholt sich nach der erwarteten kurzen Traefik-Singleton-Unterbrechung automatisch.
- [ ] Infra folgt der Plattform-Wave-Konvention, Live der Cluster/Workload-Konvention und Apps der flachen Konvention `apps.yml` plus `<workload>/app.yml`; gemeinsame Workloadnamen stimmen überein.
- [ ] Ein Neuaufbau ist aus Git, Bootstrap-Dokumentation und extern gesicherten Schlüsseln/Backups möglich.

## 15. Leitlinie für den implementierenden Agenten

Baue die kleinste funktionsfähige Lösung in Abhängigkeitsreihenfolge. Verwende keine vermeintlich vorhandenen CRDs oder OpenBao-Funktionen ohne Prüfung der installierten Version. Pinne Charts, Images und Provider. Weise Readiness, Failover und Integration pro Phase nach. Wo Werte, Berechtigungen oder Kompatibilität noch nicht entschieden sind, bleibt ein klarer TODO statt erfundener Konfiguration.

Besonders dürfen nicht versehentlich umgekehrt werden:

- Longhorn-Key und verschlüsselter Storage vor OpenBao- und PostgreSQL-PVCs;
- OpenBao vor davon abhängiger Konfiguration und VSO-Secret-Synchronisierung;
- drei CNPG-Instanzen mit je einem eigenen, einfach replizierten Longhorn-PVC;
- drei OpenBao-Raft-Instanzen mit je genau einem Daten-PVC;
- HashiCorp VSO für alle OpenBao-zu-Kubernetes-Secret-Flows;
- drei getrennte Backup-Buckets und Credential-Sätze;
- schwere Monitoring-Pods außerhalb des Raspberry Pi;
- Traefik für Edge, Istio nur für internes Mesh;
- Wave/Workload/kurzer Dateiname als einheitliche Repository-Struktur.
