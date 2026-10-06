# OpenBao-Konfiguration als IaC — lokal vorbereitet, nicht ausgerollt

## Was Git nach dem einmaligen Bootstrap steuert

- `04-openbao-access-config/workload/policies/vault-secrets-webhook-read.hcl`:
  die Webhook-ACL. Aktuell gewünscht: **read auf `kv/data/*`** (KV v2).
  Weitere Berechtigungen durch Git-Review an dieser HCL-Datei ändern.
- `04-openbao-access-config/workload/policies/human-admin.hcl`: die
  Git-verwaltete Policy für einen persönlichen Admin, nicht für den Webhook.
- `04-openbao-access-config/workload/config.json`: KV-Mount `kv/`,
  `userpass/`-Auth-Mount und Kubernetes-Auth-Rolle `vault-secrets-webhook`
  (gebundener ServiceAccount, Namespace, Policy-Namen, Token-TTL). Die TTL muss
  eine positive ganze Dauer mit `s`, `m` oder `h` sein; Null würde OpenBaos
  Mount-/System-Standard übernehmen und wird vor API-Zugriffen abgelehnt. Initial-Job
  und 15-Minuten-CronJob verifizieren KV v2 und `userpass/` und gleichen
  Policies/Webhook-Rolle mit OpenBao ab. Ein verschwundener `userpass/`-Mount
  wird **nicht** automatisch leer neu erzeugt: Benutzer würden fehlen.
  Stattdessen meldet der Job einen Fehler.
  Unbekannte zugewiesene Rollen-Policies oder geänderte ServiceAccount-
  Bindungen (einschließlich zusätzlicher Namespace-Selektoren) werden
  **nicht blind überschrieben**: der Job schlägt fehl. Die Rolle wird vor
  den ACL-Schreibzugriffen geprüft/abgeglichen, damit unerwartete Bindungen
  nicht noch vor dem Fehler breitere Policy-Rechte erhalten. Fehlt die neue
  Policy beim Bootstrap noch, gewährt ihr Rollenverweis bis zur erfolgreichen
  Policy-Anlage keine Rechte; ein fehlgeschlagener Lauf muss wiederholt werden.
- `04-openbao-access-config/app.yml`: Argo-CD-App in Wave 4, nach OpenBao und
  vor Zertifikat- und Backup-Secrets. `01-argocd-bootstrap/application-health-config.yml`
  muss vor dem Rollout geprüft und aktiviert sein: bei einem fehlgeschlagenen
  Child-Sync darf die Root-App nicht zur nächsten Wave übergehen.

**Achtung:** `kv/data/*` umfasst **alle** Secrets in diesem KV-Mount, auch
zukünftige. Jeder mit Berechtigung, ein Webhook-selektiertes Kubernetes-Secret
zu erstellen **und zu lesen**, kann darüber deren Werte abrufen. Dazu kommt:
Wer die Config-Job-HCL oder Rollen-Datei in Git kontrolliert, kann dem Webhook
weitergehende Policies geben. Die geplante Zuordnung des Rollen-Update-Rechts
ist entsprechend privilegiert. Restriktiertes Git-Review und Secret-RBAC
sind hier maßgeblich; bei Bedarf später auf einen eigenen KV-Mount umstellen.

## Einmaliger, versionierter Bootstrap — ohne UI-Zwang

Das einmalige `bootstrap.py` und seine privilegierte Writer-Policy liegen im
separaten Repository `k3s-on-omv-bootstrap` unter
`ansible/roles/k3s_cluster/files/openbao-access/`. Das vom Operator nach Review
ausgeführte Skript verbindet sich ausschließlich
über einen lokal gebundenen OpenBao-Port-Forward (`127.0.0.1:18200`), fragt
einen vorhandenen Admin-Token verdeckt ab und überträgt ihn nur an localhost.
Kein Token kommt in Git, CLI-Argumente, eine ConfigMap oder ein langlebiges
Kubernetes-Secret. Ist verdeckte Terminal-Eingabe nicht verfügbar, bricht das
Skript ab, statt Token oder Passwort mit sichtbarem Echo einzulesen.
Das Skript wird **nicht** automatisch von Argo ausgeführt.

Nach gesonderter Freigabe, von einem vertrauenswürdigen Arbeitsplatz:

```sh
kubectl -n openbao port-forward --address 127.0.0.1 svc/openbao-ui 18200:8200
# In einem zweiten Terminal, vom Infra-Repo aus:
python3 -B ../k3s-on-omv-bootstrap/ansible/roles/k3s_cluster/files/openbao-access/bootstrap.py
```

Es erstellt `kv/` als KV v2, **falls** der Mount noch fehlt, belässt einen
vorhandenen KV-v2-Mount unverändert und verweigert abweichende Versionen. Es
legt nötigenfalls den Kubernetes-Auth-Mount mit in-cluster TokenReview und
den `userpass/`-Auth-Mount an; vorhandene Mounts bleiben erhalten. Bei einem
vorhandenen Kubernetes-Auth-Mount prüft es, dass `kubernetes_host` konfiguriert
ist. Ein nur teilweise angelegter Mount (etwa nach einem abgebrochenen Bootstrap)
führt zum Abbruch; dessen Konfiguration vor dem nächsten Lauf separat prüfen und
freigeben, statt den Bootstrap fälschlich als erfolgreich zu behandeln. Es richtet
die eng gefasste Policy `openbao-access-config-writer`, die Kubernetes-Auth-
Rolle `openbao-access-config` (nur ServiceAccount
`openbao/openbao-access-config`), die Git-Policies für Webhook und
`human-admin` sowie die Webhook-Rolle ein. Vor den Policy-/Rollen-Schreibzugriffen
prüft es bei beiden vorhandenen Kubernetes-Rollen die exakten ServiceAccount-
und Namespace-Bindungen; abweichende Bindungen oder Namespace-Selektoren
führen zum Abbruch statt zu einer unbeabsichtigten Rechteerweiterung.
Vorhandene Audience-, CIDR-, Alias- und Token-Limit-Einstellungen bleiben bei
beiden Rollen erhalten; der Bootstrap setzt sie auch bei Wiederholung nicht zurück.
Anschließend fragt das Skript
**interaktiv** nach dem persönlichen Usernamen (leer = Benutzeranlage
überspringen) und, nur bei einem neuen Account, zweimal verdeckt nach dem
Passwort. Es legt den User unter `auth/userpass/users/<name>` mit
`human-admin` an. Ein bestehender Account wird **nicht** umbenannt, dessen
Passwort wird **nicht** zurückgesetzt. Danach den Login testen und MFA/TOTP
selbst einschreiben; Enforcement erst nach erfolgreichem Login und Enrollment
aktivieren, mit separatem Recovery-Zugang. Passwort und TOTP-Seed werden weder
von Argo noch vom CronJob verwaltet. Für den laufenden Loop wird
nur ein kurzlebiges, projiziertes ServiceAccount-Token benötigt – **kein**
Root-/Admin-Token. Vor dem Bootstrap die existierenden Auth-/Rollen-Einstellungen
und `openbao/openbao`-TokenReview-RBAC prüfen; beim Ausrollen ServiceAccount-
Token-Audience und Rollen-API-Version anhand des Live-Clusters validieren.
OpenBao-Initialisierung, Seal-/Recovery-Material und ein unabhängiger Restore
bleiben separate Voraussetzungen. Ein vollständig zerstörter Cluster kann
nicht allein aus einem in ihm laufenden Job rekonstruiert werden.

## Was **nicht** automatisch in Git/Job kommt

Die tatsächlichen Werte von Cloudflare-Token, K8up-Repository-Passwort und
R2-Schlüsseln. Dafür sind OpenBao-KV-Einträge `kv/cert-manager`,
`kv/k8up-repo-password` und `kv/r2-credentials` mit den in den Helm-
Referenzen genannten Feldern nötig. Diese Werte bleiben außerhalb des
öffentlichen Infra-Repos und dürfen nicht geloggt oder aus K8s Secrets
zurückgelesen werden. Sollen sie ebenfalls aus Git stammen, wäre ein
**separat** überprüfter Verschlüsselungs- und Key-Recovery-Pfad (z. B. SOPS)
notwendig; private Klartext-Helm-Values sind keine Verschlüsselung. Das
bestehende `openbao-unseal-key`-Secret des Servers ist keine Config-Job-
Berechtigung. Das Webhook aktualisiert K8s Secrets nur bei Admission
(CREATE/UPDATE), nicht automatisch nach KV-Rotation.

## Rollout, Drift und Rückweg

Vor dem Commit der neuen automatisch synchronisierenden Argo-App **zuerst**
Bootstrap- und Recovery-Weg freigeben und prüfen. Sonst scheitert der Job in
Wave 4 und blockiert spätere Waves. Offline-Checks: `ruby
tests/verify-openbao-access.rb`, `PYTHONDONTWRITEBYTECODE=1 python3 -m
unittest discover -s tests -p 'test_openbao_*.py'` und die vorhandenen
Infra-Tests. Diese Checks belegen nicht die Berechtigungen im Live-OpenBao.

Nach GitOps-Rollout nur Status, Job-Fehler und Kubernetes-Secret-**Namen**
prüfen; keine Werte oder Token ausgeben. Argo erkennt OpenBao-Drift nicht
selbst; der CronJob korrigiert sie erst beim nächsten **erfolgreichen** Lauf.
CronJob-Fehler und überfällige Erfolge müssen überwacht werden. Ein alter
Cron-Lauf und neuer Argo-Hook können bei gleichzeitiger Revision kurzfristig
konkurrieren; nach dem Rollout den nächsten erfolgreichen Cron-Lauf prüfen.

Rollback: Automation anhalten, letzten geprüften Git-Stand wiederherstellen,
Bootstrap-/Rollenänderungen nur nach separatem Review zurücknehmen. Das
Löschen der Argo-App löscht weder OpenBao-Policies noch Secrets. Vor Vertrauen
in K8up unbedingt den unabhängigen Restore testen; OpenBao darf nicht allein
von einem Backup abhängen, dessen Schlüssel in OpenBao liegen.
