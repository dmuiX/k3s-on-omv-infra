#!/venv/bin/python3
"""Run an isolated CNPG point-in-time recovery and publish bounded evidence."""
import base64, copy, json, os, re, ssl, subprocess, time, uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

API = "https://kubernetes.default.svc"
SOURCE_NS, VALIDATION_NS = "cloudnative-pg", "backup-validation"
SOURCE, RESTORE, STORE = "k3s-postgres", "postgresql-pitr-validation", "postgresql-pitr-validation"
PLUGIN = "barman-cloud.cloudnative-pg.io"
SOURCE_API = f"/apis/postgresql.cnpg.io/v1/namespaces/{SOURCE_NS}"
LEDGER = "postgresql-restore-ledger"
LEDGER_PATH = f"/api/v1/namespaces/{VALIDATION_NS}/configmaps/{LEDGER}"
STORE_PATH = f"/apis/barmancloud.cnpg.io/v1/namespaces/{VALIDATION_NS}/objectstores"
CLUSTER_PATH = f"/apis/postgresql.cnpg.io/v1/namespaces/{VALIDATION_NS}/clusters"
LEASE_PATH = f"/apis/coordination.k8s.io/v1/namespaces/{VALIDATION_NS}/leases/postgresql-restore-status"
BACKUP_ID = re.compile(r"[0-9]{8}T[0-9]{6}")
MAX_CLAIMS = 20_000
RESTORE_EVIDENCE = "post-backup-pitr-v1"
RUN_ANNOTATION = "k3s-on-omv.io/restore-validation-run"
IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.6-system-bookworm@sha256:b4bec141fa5ca2bab490b197c814fb1342147c8405be1fb0673951fa6db7f699"


@cache
def api_settings() -> tuple[dict[str, str], ssl.SSLContext]:
    """Load the in-cluster configuration on the first API call, not on import."""
    service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    token = (service_account / "token").read_text().strip()
    tls = ssl.create_default_context(cafile=str(service_account / "ca.crt"))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, tls


def api(path, method="GET", body=None, allowed=()):
    headers, tls = api_settings()
    data = None if body is None else json.dumps(body).encode()
    try:
        with urlopen(Request(API + path, data=data, method=method, headers=headers), context=tls, timeout=30) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except HTTPError as exc:
        if exc.code in allowed:
            return None
        raise RuntimeError(f"Kubernetes API {method} {path} failed with HTTP {exc.code}") from exc


READER = "restore_reader"
RESTORE_HOST = f"{RESTORE}-rw.{VALIDATION_NS}.svc"


def restored_credentials(cluster_uid: str) -> dict[str, str]:
    item = api(f"/api/v1/namespaces/{VALIDATION_NS}/secrets/{RESTORE}-app")
    if not any(owner.get("kind") == "Cluster" and owner.get("name") == RESTORE
               and owner.get("uid") == cluster_uid and owner.get("controller") is True
               for owner in item["metadata"].get("ownerReferences", [])):
        raise RuntimeError("restore credentials do not belong to this recovery cluster")
    credentials = {k: base64.b64decode(item["data"][k], validate=True).decode() for k in ("username", "password")}
    if credentials["username"] != READER or not credentials["password"]:
        raise RuntimeError("unexpected restore login")
    return credentials


def source_credentials() -> dict[str, str]:
    item = api(f"/api/v1/namespaces/{SOURCE_NS}/secrets/{SOURCE}-app")
    credentials = {k: base64.b64decode(item["data"][k], validate=True).decode() for k in ("username", "password")}
    if credentials["username"] != "app" or not credentials["password"]:
        raise RuntimeError("unexpected source marker login")
    return credentials


def connect_source(credentials):
    import psycopg2

    if any(os.environ.get(k) for k in ("PGHOSTADDR", "PGSERVICE")):
        raise RuntimeError("unexpected source connection settings")
    return psycopg2.connect(host=f"{SOURCE}-rw.{SOURCE_NS}.svc", port=5432, dbname="app",
                            user=credentials["username"], password=credentials["password"], connect_timeout=15,
                            sslmode="require", gssencmode="disable", options="-c search_path=pg_catalog -c statement_timeout=20000")


def connect(restored, database):
    import psycopg2

    # libpq's hostaddr and service can redirect traffic despite an explicit host.
    if database not in ("app", "grafana", "radar") or any(os.environ.get(k) for k in ("PGHOSTADDR", "PGSERVICE")):
        raise RuntimeError("unexpected restore connection settings")
    return psycopg2.connect(host=RESTORE_HOST, port=5432, dbname=database, user=restored["username"],
                            password=restored["password"], connect_timeout=15, sslmode="require", gssencmode="disable",
                            options="-c default_transaction_read_only=on -c search_path=pg_catalog -c statement_timeout=20000")


def check_restored_app_schemas(restored: dict[str, str]) -> None:
    """Read real application records; return only booleans, never application payloads."""
    for database, tables, query in (
        ("grafana", ("resource", "data_source"), """
            SELECT EXISTS (SELECT FROM public.resource WHERE \"group\" = 'dashboard.grafana.app'
                AND resource = 'dashboards' AND value::jsonb->>'kind' = 'Dashboard'
                AND value::jsonb #>> '{metadata,name}' = name AND name <> ''
                AND jsonb_typeof(value::jsonb #> '{spec,title}') = 'string'
                AND length(btrim(value::jsonb #>> '{spec,title}')) > 0)
            AND EXISTS (SELECT FROM public.data_source WHERE uid = 'prometheus' AND type = 'prometheus')
        """),
        ("radar", ("radar_timeline_events", "radar_timeline_seen_resources"), """
            SELECT EXISTS (SELECT FROM public.radar_timeline_events WHERE timestamp > 0 AND seq > 0
                AND id <> '' AND kind <> '' AND name <> ''
                AND source IN ('informer', 'k8s_event', 'historical')
                AND event_type IN ('add', 'update', 'delete', 'Normal', 'Warning'))
            AND EXISTS (SELECT FROM public.radar_timeline_seen_resources
                WHERE octet_length(resource_key) > 0 AND seen_at IS NOT NULL)
        """),
    ):
        with closing(connect(restored, database)) as db, db.cursor() as cursor:
            cursor.execute("""
                SELECT c.relname FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                  AND pg_catalog.pg_get_userbyid(c.relowner) = %s AND c.relname IN (%s, %s)
            """, (database, *tables))
            if {row[0] for row in cursor.fetchall()} != set(tables):
                raise RuntimeError(f"restored {database} application schema check failed")
            cursor.execute(query)
            if cursor.fetchone() != (True,):
                raise RuntimeError(f"restored {database} application data check failed")


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp lacks a timezone")
    return parsed


def next_backup(ledger: dict) -> tuple[str, datetime] | None:
    """Select only after checking every page of the Backup list snapshot."""
    meta = ledger["metadata"]
    baseline = timestamp(meta["creationTimestamp"])
    claims = ledger.get("data", {})
    now = datetime.now(timezone.utc)
    if (meta.get("name") != LEDGER or meta.get("namespace") != VALIDATION_NS
            or not meta.get("resourceVersion") or meta.get("deletionTimestamp") or baseline > now
            or ledger.get("binaryData") or not isinstance(claims, dict)
            or any(not BACKUP_ID.fullmatch(key) or value not in ("claimed", "unverified", "failed", "failed-cleanup", "success")
                   for key, value in claims.items())):
        raise RuntimeError("invalid restore ledger")
    if len(claims) >= MAX_CLAIMS:
        raise RuntimeError("restore ledger at capacity")

    oldest = None
    query = {"limit": "200"}
    seen_tokens = set()
    for _ in range(100):
        page = api(f"{SOURCE_API}/backups?{urlencode(query)}")
        if not isinstance(page["items"], list) or len(page["items"]) > 200:
            raise RuntimeError("invalid Backup list page")
        for item in page["items"]:
            meta, spec, status = item["metadata"], item["spec"], item.get("status", {})
            if (meta.get("deletionTimestamp") or spec.get("cluster", {}).get("name") != SOURCE
                    or spec.get("method") != "plugin" or spec.get("pluginConfiguration", {}).get("name") != PLUGIN
                    or status.get("phase") != "completed"):
                continue
            stopped = timestamp(status["stoppedAt"])
            if stopped <= baseline:
                continue
            backup_id = status.get("backupId", "")
            created = timestamp(meta["creationTimestamp"])
            started = timestamp(status["startedAt"])
            if (not meta.get("uid") or not meta.get("name") or not BACKUP_ID.fullmatch(backup_id)
                    or status.get("method") != "plugin" or not created <= started <= stopped <= now):
                raise RuntimeError("completed Backup is missing valid native backup evidence")
            candidate = (stopped, backup_id, meta["name"])
            if backup_id not in claims and (oldest is None or candidate < oldest):
                oldest = candidate
        token = page["metadata"].get("continue")
        if not token:
            if oldest is None:
                return None
            stopped, backup_id, name = oldest
            print(f"Selected Backup {name}, Barman ID {backup_id}", flush=True)
            return backup_id, stopped
        if not isinstance(token, str) or token in seen_tokens:
            raise RuntimeError("invalid Backup pagination token")
        seen_tokens.add(token)
        query["continue"] = token
    raise RuntimeError("Backup list exceeds pagination limit")


def put_lease(result: str, backup_id: str, pitr: bool = False, mirror: bool = False, *, owned_version: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    body = {
        "apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
        "metadata": {"name": "postgresql-restore-status", "namespace": VALIDATION_NS, "annotations": {
            "k3s-on-omv.io/result": result,
            "k3s-on-omv.io/pitr-verified": str(pitr).lower(),
            "k3s-on-omv.io/omv-mirror-verified": str(mirror).lower(),
            "k3s-on-omv.io/restored-backup-id": backup_id,
        }},
        "spec": {"holderIdentity": "postgresql-pitr-validation", "renewTime": now.isoformat(timespec="microseconds").replace("+00:00", "Z"), "leaseDurationSeconds": 691200},
    }
    if result == "success" and pitr:
        body["metadata"]["annotations"]["k3s-on-omv.io/restore-evidence"] = RESTORE_EVIDENCE
    if result == "running":
        current = api(LEASE_PATH, allowed=(404,))
        if current is None:
            created = api(LEASE_PATH.rsplit("/", 1)[0], "POST", body)
            return created["metadata"]["resourceVersion"]
        if current["metadata"].get("annotations", {}).get("k3s-on-omv.io/result") == "running":
            # Do not overlap restores, even for distinct IDs. The Job deadline is 5400s.
            if now <= timestamp(current["spec"]["renewTime"]) + timedelta(seconds=5400):
                raise RuntimeError("another restore validation is running")
        owned_version = current["metadata"].get("resourceVersion")
    # Publish only against the exact running Lease we acquired, even on CAS failure.
    if not owned_version:
        raise RuntimeError("cannot update unversioned Lease")
    body["metadata"]["resourceVersion"] = owned_version
    updated = api(LEASE_PATH, "PUT", body)
    return updated["metadata"]["resourceVersion"]


def refresh_mirror() -> None:
    lease = api(LEASE_PATH, allowed=(404,))
    if lease is None:
        return
    annotations = lease["metadata"].get("annotations", {})
    backup_id = annotations.get("k3s-on-omv.io/restored-backup-id", "")
    mirror = "k3s-on-omv.io/omv-mirror-verified"
    if (annotations.get("k3s-on-omv.io/result") == "success"
            and annotations.get("k3s-on-omv.io/restore-evidence") == RESTORE_EVIDENCE
            and annotations.get("k3s-on-omv.io/pitr-verified") == "true"
            and annotations.get(mirror) == "false" and BACKUP_ID.fullmatch(backup_id)
            and mirror_matches_catalog(backup_id)):
        if not lease["metadata"].get("resourceVersion"):
            raise RuntimeError("cannot update unversioned Lease")
        annotations[mirror] = "true"
        api(LEASE_PATH, "PUT", lease)


def wait_ready(uid: str, timeout=3000):
    end = time.monotonic() + timeout
    path = f"/apis/postgresql.cnpg.io/v1/namespaces/{VALIDATION_NS}/clusters/{RESTORE}"
    while time.monotonic() < end:
        item = api(path)
        if item["metadata"]["uid"] != uid or item["metadata"].get("deletionTimestamp"):
            raise RuntimeError("isolated recovery cluster changed while waiting for readiness")
        status = item.get("status", {})
        if (READER in status.get("managedRolesStatus", {}).get("byStatus", {}).get("reconciled", [])
                and any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))):
            return
        time.sleep(15)
    raise RuntimeError("isolated recovery cluster did not become Ready")


def catalog_ids(backup_id: str) -> set[str]:
    endpoint, bucket, server = os.environ["S3_ENDPOINT"], os.environ["S3_BUCKET"], os.environ["BARMAN_SERVER_NAME"]
    try:
        output = subprocess.run(
            ["barman-cloud-backup-list", "--format", "json", "--endpoint-url", endpoint, f"s3://{bucket}", server],
            check=True, capture_output=True, text=True, timeout=180,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError("Barman catalog command failed or timed out") from None
    entries = json.loads(output)["backups_list"]
    matches = [item for item in entries if item.get("backup_id") == backup_id]
    if len(matches) != 1 or matches[0].get("status") != "DONE":
        raise RuntimeError("selected Backup is not uniquely completed in the Barman catalog")
    return {item["backup_id"] for item in entries if item.get("status") == "DONE"}


def mirror_matches_catalog(backup_id: str) -> bool:
    ids = catalog_ids(backup_id)
    mirrored = {path.parent.name for path in Path("/omv").glob(f"{os.environ['BARMAN_SERVER_NAME']}/base/*/backup.info")}
    return ids <= mirrored


def validation_store(source: dict, endpoint: str) -> dict:
    """Clone the store without inheriting the production endpoint Secret reference."""
    spec = copy.deepcopy(source["spec"])
    sidecar = spec.get("instanceSidecarConfiguration", {})
    if "env" in sidecar:
        sidecar["env"] = [
            env for env in sidecar["env"] if env["name"] != "AWS_ENDPOINT_URL_S3"
        ]
    spec["configuration"]["endpointURL"] = endpoint
    creds = spec["configuration"]["s3Credentials"]
    creds["accessKeyId"]["name"] = creds["secretAccessKey"]["name"] = "postgresql-validation-r2"
    if "region" in creds:
        creds["region"]["name"] = "postgresql-validation-r2"
    return {
        "apiVersion": source["apiVersion"],
        "kind": "ObjectStore",
        "metadata": {"name": STORE, "namespace": VALIDATION_NS},
        "spec": spec,
    }


def validation_cluster(target: str, backup_id: str) -> dict:
    """Build the disposable restore cluster, not a production storage profile."""
    return {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": RESTORE, "namespace": VALIDATION_NS},
        "spec": {
            "instances": 1, "imageName": IMAGE,
            "storage": {"storageClass": "longhorn-single-node-delete", "size": "5Gi"},
            "inheritedMetadata": {"annotations": {"k8up.io/backup": "false"}},
            "enableSuperuserAccess": False,
            "postgresql": {"pg_hba": [f"hostssl app,grafana,radar {READER} all scram-sha-256",
                                      f"host all {READER} all reject"]},
            "managed": {"roles": [{"name": READER, "login": True, "inRoles": ["pg_read_all_data"],
                                   "passwordSecret": {"name": f"{RESTORE}-app"}}]},
            "bootstrap": {"recovery": {"source": "origin", "database": READER, "owner": READER, "recoveryTarget": {
                "backupID": backup_id, "targetTime": target,
            }}},
            "externalClusters": [{"name": "origin", "plugin": {
                "enabled": True, "name": PLUGIN,
                "parameters": {"barmanObjectName": STORE, "serverName": SOURCE},
            }}],
        },
    }


def create_validation_resource(path: str, body: dict, run: str, created_paths: list[tuple[str, str]]) -> None:
    metadata = body["metadata"]
    metadata["annotations"] = {RUN_ANNOTATION: run}
    resource = f"{path}/{metadata['name']}"
    try:
        created = api(path, "POST", body)["metadata"]
    except Exception:
        # A failed request may already have created the object. Never claim
        # an unrelated resource that shares the stable validation name.
        observed = api(resource, allowed=(404,))
        if observed is not None:
            candidate = observed["metadata"]
            if candidate.get("annotations", {}).get(RUN_ANNOTATION) == run and candidate.get("uid"):
                created_paths.append((resource, candidate["uid"]))
        raise
    created_paths.append((resource, created["uid"]))


def validate(backup_id: str, stopped: datetime) -> None:
    catalog_ids(backup_id)
    run = uuid.uuid4().hex
    before, after = f"{run}-before", f"{run}-after"
    producer = source_credentials()
    markers_started = False
    created_paths = []
    try:
        source_store = api(f"/apis/barmancloud.cnpg.io/v1/namespaces/{SOURCE_NS}/objectstores/k3s-postgres-backups")
        store = validation_store(source_store, os.environ["S3_ENDPOINT"])
        if (store["spec"]["configuration"]["destinationPath"].rstrip("/") != f"s3://{os.environ['S3_BUCKET']}"
                or os.environ["BARMAN_SERVER_NAME"] != SOURCE):
            raise RuntimeError("restore source does not match the verified Barman catalog")
        print(f"Recovering completed Backup {backup_id} ({stopped.isoformat()}) in run {run}", flush=True)
        with closing(connect_source(producer)) as db, db, db.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp()")
            if cursor.fetchone()[0] <= stopped:
                raise RuntimeError("primary clock has not passed the selected backup completion")
            cursor.execute("CREATE TABLE IF NOT EXISTS public.platform_backup_validation (run_id text PRIMARY KEY, phase text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp())")
            cursor.execute("INSERT INTO public.platform_backup_validation(run_id, phase) VALUES (%s, 'before')", (before,))
            markers_started = True  # Even an ambiguous commit needs cleanup.
        with closing(connect_source(producer)) as db, db, db.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp()")
            target = cursor.fetchone()[0].isoformat()
            if timestamp(target) <= stopped:
                raise RuntimeError("PITR target is not after the selected backup completion")
        time.sleep(2)
        with closing(connect_source(producer)) as db, db, db.cursor() as cursor:
            cursor.execute("INSERT INTO public.platform_backup_validation(run_id, phase) VALUES (%s, 'after')", (after,))
        print(f"Restoring {backup_id} to {target} between committed markers for {run}", flush=True)
        time.sleep(360)  # Allow archive_timeout (5 min) to ship the after-marker WAL.
        for path, body in ((STORE_PATH, store), (CLUSTER_PATH, validation_cluster(target, backup_id))):
            create_validation_resource(path, body, run, created_paths)
        wait_ready(created_paths[-1][1])
        credentials = restored_credentials(created_paths[-1][1])
        try:
            with closing(connect(credentials, "app")) as db, db.cursor() as cursor:
                cursor.execute("SELECT run_id, phase FROM public.platform_backup_validation WHERE run_id IN (%s,%s)", (before, after))
                if set(cursor.fetchall()) != {(before, "before")}:
                    raise RuntimeError("PITR boundary validation failed")
            check_restored_app_schemas(credentials)
        except RuntimeError:
            raise  # Our bounded schema/data failure message.
        except Exception:
            # A driver error (e.g. invalid JSON) can include restored row contents.
            raise RuntimeError("restored application query failed") from None
    finally:
        cleanup_errors = []
        for path, uid in reversed(created_paths):
            try:
                api(path, "DELETE", {"preconditions": {"uid": uid}}, (404,))
            except Exception as exc:
                cleanup_errors.append(exc)
        if markers_started:
            try:
                with closing(connect_source(producer)) as db, db, db.cursor() as cursor:
                    cursor.execute("DELETE FROM public.platform_backup_validation WHERE run_id IN (%s,%s)", (before, after))
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            raise CleanupError("validation cleanup failed") from cleanup_errors[0]


class CleanupError(RuntimeError):
    pass


def record_state(ledger: dict, backup_id: str, state: str) -> dict:
    """CAS against the selection or confirmed claim. Never retry a stale write."""
    if not ledger["metadata"].get("resourceVersion"):
        raise RuntimeError("cannot update unversioned restore ledger")
    return api(LEDGER_PATH, "PUT", {**ledger, "data": {**ledger.get("data", {}), backup_id: state}})


def main():
    ledger = api(LEDGER_PATH)
    selected = next_backup(ledger)
    if selected is None:
        refresh_mirror()
        return
    backup_id, stopped = selected
    version = put_lease("running", backup_id)
    try:
        if any(api(path, allowed=(404,)) is not None for path in
               (f"{STORE_PATH}/{STORE}", f"{CLUSTER_PATH}/{RESTORE}")):
            raise RuntimeError("previous validation resources still exist")
        claim = record_state(ledger, backup_id, "unverified")
        try:
            validate(backup_id, stopped)
        except Exception as exc:
            record_state(claim, backup_id, "failed-cleanup" if isinstance(exc, CleanupError) else "failed")
            raise
        record_state(claim, backup_id, "success")
        mirror_verified = mirror_matches_catalog(backup_id)
        if not mirror_verified:
            print("Exact-backup restore passed; OMV mirror is missing catalog entries. Retention remains blocked.", flush=True)
    except CleanupError:
        put_lease("failed-cleanup", backup_id, owned_version=version)
        raise
    except Exception:
        put_lease("failed", backup_id, owned_version=version)
        raise
    put_lease("success", backup_id, True, mirror_verified, owned_version=version)


if __name__ == "__main__":
    main()
