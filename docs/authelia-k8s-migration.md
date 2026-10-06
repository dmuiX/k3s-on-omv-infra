# Authelia von OMV Compose nach Kubernetes — Entwurf, kein Cutover

Ziel: **Authelia und PostgreSQL laufen vollständig in Kubernetes**, Compose
wird nach verifiziertem Cutover beendet. Dieses Dokument erzeugt keine
Kubernetes-Ressourcen und ändert die bestehende Installation nicht.

## Lesend erfasster Ist-Zustand

- Compose betreibt Authelia **4.39.28** und PostgreSQL **18**. Die Images sind
  derzeit als bewegliche Tags konfiguriert; beim ersten K8s-Rollout zunächst
  die tatsächlich laufenden Versionen/Digests pinnen.
- Authelia verwendet einen dateibasierten User-Backend-Pfad (`users_database.yml`),
  PostgreSQL für persistenten Authelia-State sowie OIDC mit mehreren Clients
  und einer JWKS-Signierkonfiguration. Der Container bindet `/config` ein;
  PostgreSQL hat einen persistenten Mount.
- Compose enthält einen `locket`-Schritt mit Bao-Bezug. **Zu klären:** Welche
  Werte werden dort erzeugt/aus dem Docker-OpenBao bezogen und wie werden
  sie sicher in K8s/OpenBao bereitgestellt? Docker-OpenBao und K8s-OpenBao
  sind verschiedene Instanzen. Weder Docker-Umgebungen noch Konfigurations-
  oder Secret-Werte wurden für diesen Entwurf ausgelesen.
- Das vorhandene `add-client.sh`-Skript bleibt Eingabe für die OIDC-Client-
  Migration; seine Werte/Schlüssel wurden nicht ausgelesen.

## Zielarchitektur

1. Dedizierter Namespace `authelia`, eigener ServiceAccount, isolierende
   NetworkPolicy, `ClusterIP`-Services. Argo-CD-Application **erst nach**
   Bootstrap-/Secret-/DB-Plan aktivieren; nicht als unvorbereiteten Child-App
   in den automatisch synchronisierenden Infra-Root aufnehmen.
2. Authelia-Deployment zunächst **ein Replica** mit gepinntem Image, CPU-/RAM-
   Requests/Limits und Health-Probes. PostgreSQL 18 getrennt mit eigenem
   ServiceAccount, PVC auf Longhorn (`Retain`/Recovery prüfen), Probes und
   Resource-Bounds. Ein Longhorn-Replica auf einem Node ist **nicht HA**.
3. Bestehende `configuration.yml`, OIDC-JWKS **inklusive privatem Schlüssel**,
   OIDC-Client-Secrets und User-Datei mit Passwort-Hashes nur als sensitive
   Daten behandeln: getrennte Kubernetes-Secrets bzw. ein überprüfter
   verschlüsselter Secret-Workflow, **kein öffentliches Git/ConfigMap**.
   Nicht blind neue Signing-, Session- oder Storage-Encryption-Keys erzeugen.
   Datenbank-Credentials separat als Secret. Ein Operator muss Quelle und
   Übertragung sicher prüfen; der Agent liest oder kopiert keine Secret-Werte.
4. Nicht geheime Konfiguration (Version, Ressourcen, Service, NetworkPolicy,
   Gateway-Route, Auth-/DB-Referenzen) als GitOps-Manifeste. Erst wenn die
   Authelia-Route unter der bisherigen **exakten OIDC-Issuer-URL** zuverlässig
   erreichbar ist, Argo CD/Grafana als OIDC-Clients umstellen. Lokalen
   Notfall-Login für Argo CD testen.

## Migration in überprüfbaren Phasen

1. **Bestandsaufnahme:** Sanitized Compose-/Authelia-Konfiguration mit
   Secret-Werten entfernt bereitstellen; klären, ob `locket` Werte aus der
   Docker-OpenBao-Instanz lädt und welche K8s-Secret-Referenzen nötig sind.
   OIDC-Issuer, Redirects, Client-Namen, Signing-Key-Identität, Session- und
   Encryption-Key-Abhängigkeiten prüfen, ohne Werte zu veröffentlichen.
2. **Recovery:** Konsistenten PostgreSQL-Backup- und Restore-Test sowie
   Sicherung von Authelia-Konfiguration/User-Datei/Schlüsseln **außerhalb**
   des Clusters durchführen. Erst dann PVC, Secrets und einen zunächst
   extern nicht erreichbaren K8s-Test-Stack vorbereiten.
3. **K8s-Test:** Bestehende Daten in einen **separaten** K8s-Postgres-
   Datenspeicher migrieren; Authelia mit denselben Schlüsseln und passenden
   DB-/Datei-Referenzen starten. Readiness, Login, MFA und OIDC Discovery/
   JWKS prüfen. Compose bleibt verfügbar; niemals beide Instanzen
   gleichzeitig gegen dieselbe schreibbare Datenbank betreiben.
4. **Cutover:** Bisherige Issuer-URL und HTTPS-Host beibehalten. Heute erreicht
   externer HTTPS-Traffic zuerst den Docker-Reverse-Proxy, während K3s seinen
   Gateway auf einem anderen Port betreibt: Host-/Port-/Forwarded-Header-
   Routing explizit planen, bevor DNS/Proxy geändert wird. Anschließend
   Argo CD und Grafana OIDC-End-to-End sowie lokale Recovery-Logins prüfen.
   Compose erst abschalten, wenn Login und Client-Callbacks erfolgreich sind.
5. **Rollback:** Alte Compose-Konfiguration und unabhängige DB-Sicherung bis
   nach Beobachtungsphase behalten; bei Problemen Proxy zurückschalten.
   Nach produktiven K8s-Schreibvorgängen ist eine Rückkehr zum alten
   Compose-DB-Stand **kein verlustfreier automatischer Rollback**; vorher
   Daten-/Session-Strategie freigeben.

**Noch nicht erfüllt:** Es gibt derzeit weder eine K8s-Authelia-Application
noch freigegebene Manifestwerte/Secret-Quelle/DB-Restore. Einen generischen
Helm-Release ohne diese Bestandsaufnahme in den Root aufzunehmen, würde die
bestehende Anmeldung und die OIDC-Issuer-Identität gefährden.
