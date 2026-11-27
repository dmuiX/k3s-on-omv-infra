#!/venv/bin/python3
"""Run an isolated CNPG point-in-time recovery and publish bounded evidence."""
import base64, copy, json, math, os, re, ssl, subprocess, sys, time, uuid
from datetime import datetime, timezone
from functools import cache
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

API = "https://kubernetes.default.svc"
SOURCE_NS, VALIDATION_NS = "cloudnative-pg", "backup-validation"
SOURCE = "k3s-postgres"
RESTORE = STORE = "postgresql-restore-test"
RUN_ANNOTATION = "k3s-on-omv.io/restore-run-id"
IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.7-system-bookworm@sha256:17b1b2d8b41398e4a5aa5466b49c866cb500dcaf08e9a4923ca443f2cd13c25a"
MIN_RESTORE_GIB, MAX_RESTORE_GIB = 5, 40

@cache
def api_settings() -> tuple[dict[str, str], ssl.SSLContext]:
    """Load the in-cluster configuration on the first API call, not on import."""
    service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    token = (service_account / "token").read_text().strip()
    tls = ssl.create_default_context(cafile=str(service_account / "ca.crt"))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, tls


def api(path, method="GET", body=None, allowed=(), *, timeout: float = 30):
    headers, tls = api_settings()
    data = None if body is None else json.dumps(body).encode()
    try:
        with urlopen(Request(API + path, data=data, method=method, headers=headers), context=tls, timeout=timeout) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except HTTPError as exc:
        exc.close()
        if exc.code in allowed:
            return None
        raise RuntimeError(f"Kubernetes API {method} {path} failed with HTTP {exc.code}") from exc


def secret(namespace, name):
    payload = api(f"/api/v1/namespaces/{namespace}/secrets/{name}")["data"]
    return {key: base64.b64decode(value).decode() for key, value in payload.items()}


def connect(values, host, *, dbname: str | None = None, read_only: bool = False):
    import psycopg2

    kwargs = {
        "host": host,
        "port": values.get("port", "5432"),
        "dbname": dbname or values.get("dbname", "app"),
        "user": values["username"],
        "password": values["password"],
        "connect_timeout": 15,
        "sslmode": "require",
    }
    if read_only:
        kwargs["options"] = "-c default_transaction_read_only=on"
    return psycopg2.connect(**kwargs)


APP_DATABASE_CHECKS: dict[str, tuple[tuple[str, str], ...]] = {
    "grafana": (
        ("migration_log", 'SELECT count(*) FROM public.migration_log'),
        ("org", 'SELECT count(*) FROM public.org'),
        ("user", 'SELECT count(*) FROM public."user"'),
    ),
    "radar": (
        ("radar_timeline_schema_migrations", "SELECT count(*) FROM public.radar_timeline_schema_migrations"),
        ("radar_timeline_seen_resources", "SELECT count(*) FROM public.radar_timeline_seen_resources"),
        ("radar_timeline_events", "SELECT count(*) FROM public.radar_timeline_events"),
    ),
}


def verify_app_databases(host: str) -> dict[str, dict[str, int]]:
    """Verify Grafana and Radar databases are readable and non-empty on the isolated restore."""
    creds = secret(VALIDATION_NS, f"{RESTORE}-superuser")
    counts: dict[str, dict[str, int]] = {}
    for dbname, checks in APP_DATABASE_CHECKS.items():
        db_counts: dict[str, int] = {}
        with connect(creds, host, dbname=dbname, read_only=True) as db, db.cursor() as cursor:
            for label, query in checks:
                cursor.execute(query)
                row = cursor.fetchone()
                count = int(row[0]) if row else 0
                if count < 1:
                    raise RuntimeError(f"restored database {dbname!r} table {label!r} is empty")
                db_counts[label] = count
        counts[dbname] = db_counts
    return counts


def connect_source_at_startup(values: dict[str, str], host: str):
    """Retry only the known libpq disconnect, before any SQL is issued."""
    import psycopg2

    for delay in (1, 2, 4):
        try:
            return connect(values, host)
        except psycopg2.OperationalError as exc:
            # Match the whole diagnostic so mixed errors (e.g. auth) do not retry.
            if not re.fullmatch(
                r"(?:connection to server [^\n]+ failed: )?server closed the connection unexpectedly"
                r"(?:\n[ \t]+This probably means the server terminated abnormally"
                r"\n[ \t]+before or while processing the request\.)?",
                str(exc).strip(),
            ):
                raise
            time.sleep(delay)
    return connect(values, host)


def put_lease(
    result: str,
    *,
    restore_target_verified: bool = False,
    mirror_verified: bool = False,
    app_databases_verified: bool = False,
) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    body = {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": "postgresql-restore-status",
            "namespace": VALIDATION_NS,
            "annotations": {
                "k3s-on-omv.io/result": result,
                "k3s-on-omv.io/restore-target-verified": str(restore_target_verified).lower(),
                "k3s-on-omv.io/omv-mirror-verified": str(mirror_verified).lower(),
                "k3s-on-omv.io/app-databases-verified": str(app_databases_verified).lower(),
            },
        },
        "spec": {"holderIdentity": RESTORE, "renewTime": now, "leaseDurationSeconds": 691200},
    }
    path = f"/apis/coordination.k8s.io/v1/namespaces/{VALIDATION_NS}/leases/postgresql-restore-status"
    current = api(path, allowed=(404,))
    if current:
        body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        api(path, "PUT", body)
    else:
        api(f"/apis/coordination.k8s.io/v1/namespaces/{VALIDATION_NS}/leases", "POST", body)


def wait_ready(timeout=3000):
    end = time.monotonic() + timeout
    path = f"/apis/postgresql.cnpg.io/v1/namespaces/{VALIDATION_NS}/clusters/{RESTORE}"
    while time.monotonic() < end:
        item = api(path)
        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in item.get("status", {}).get("conditions", [])):
            return
        time.sleep(15)
    raise RuntimeError("isolated recovery cluster did not become Ready")


def mirror_matches_catalog():
    endpoint, bucket, server = os.environ["S3_ENDPOINT"], os.environ["S3_BUCKET"], os.environ["BARMAN_SERVER_NAME"]
    output = subprocess.run(["barman-cloud-backup-list","--format","json","--endpoint-url",endpoint,f"s3://{bucket}",server], check=True, capture_output=True, text=True, timeout=300).stdout
    ids = {str(item["backup_id"]) for item in json.loads(output)["backups_list"] if item.get("status") == "DONE"}
    mirrored = {path.parent.name for path in Path("/omv").glob(f"{server}/base/*/backup.info")}
    return bool(ids) and ids <= mirrored


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


def restore_storage_size(cluster_bytes: int | None) -> str:
    """Compute a disposable PVC size from the measured backup cluster_size."""
    if type(cluster_bytes) is not int or cluster_bytes <= 0:
        return f"{MIN_RESTORE_GIB}Gi"
    gib = math.ceil((cluster_bytes * 1.5) / (1024**3)) + 2
    return f"{max(MIN_RESTORE_GIB, min(MAX_RESTORE_GIB, gib))}Gi"


def latest_backup_bytes() -> int | None:
    """Read the latest completed backup's uncompressed cluster_size from Barman."""
    endpoint = os.environ.get("S3_ENDPOINT")
    bucket = os.environ.get("S3_BUCKET")
    server = os.environ.get("BARMAN_SERVER_NAME")
    if not (endpoint and bucket and server):
        return None
    try:
        output = subprocess.run(
            ["barman-cloud-backup-list", "--format", "json", "--endpoint-url", endpoint, f"s3://{bucket}", server],
            check=True, capture_output=True, text=True, timeout=300,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    done = [item for item in json.loads(output).get("backups_list", []) if item.get("status") == "DONE"]
    if not done:
        return None
    latest = max(done, key=lambda item: str(item.get("backup_id", "")))
    size = latest.get("cluster_size")
    return size if type(size) is int and size > 0 else None


def validation_cluster(target: str, storage_size: str = "5Gi") -> dict:
    """Build the disposable restore cluster, not a production storage profile."""
    return {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": RESTORE, "namespace": VALIDATION_NS},
        "spec": {
            "instances": 1, "imageName": IMAGE,
            "enableSuperuserAccess": True,
            "storage": {"storageClass": "longhorn-single-node-delete", "size": storage_size},
            "inheritedMetadata": {"annotations": {"k8up.io/backup": "false"}},
            "bootstrap": {"recovery": {"source": "origin", "recoveryTarget": {"targetTime": target}}},
            "externalClusters": [{"name": "origin", "plugin": {
                "enabled": True, "name": "barman-cloud.cloudnative-pg.io",
                "parameters": {"barmanObjectName": STORE, "serverName": SOURCE},
            }}],
        },
    }


def create_restore_resource(
    collection: str, body: dict, run: str, cleanup: dict[str, str | None]
) -> None:
    """Track the returned UID, or mark a possibly committed POST for cleanup lookup."""
    metadata = body["metadata"]
    metadata.setdefault("annotations", {})[RUN_ANNOTATION] = run
    path = f"{collection}/{metadata['name']}"
    try:
        uid = api(collection, "POST", body)["metadata"]["uid"]
        if not isinstance(uid, str) or not uid:
            raise ValueError(f"create response for {path} has no UID")
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        cleanup[path] = None
        raise
    except RuntimeError as exc:
        # api() wraps HTTP errors. A definitive rejection (e.g. 409) is not ours.
        cause = exc.__cause__
        if isinstance(cause, HTTPError) and (cause.code == 408 or cause.code >= 500):
            cleanup[path] = None
        raise
    cleanup[path] = uid


def reconcile_restore_uid(path: str, run: str) -> str | None:
    """Find the UID after an ambiguous create; never adopt another run's object."""
    deadline = time.monotonic() + 60
    while (remaining := deadline - time.monotonic()) > 0:
        current = api(path, allowed=(404,), timeout=min(30, remaining))
        if current is not None:
            metadata = current["metadata"]
            if (metadata.get("annotations") or {}).get(RUN_ANNOTATION) != run:
                return None
            uid = metadata.get("uid")
            if not isinstance(uid, str) or not uid:
                raise RuntimeError(f"no UID for restore resource {path}")
            return uid
        # A POST can still commit after a GET returns 404.
        if (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(2, remaining))
    message = (
        f"ambiguous create unresolved for {path} (run {run}): not found before reconciliation deadline; "
        "the create may still complete"
    )
    print(message, file=sys.stderr)
    raise RuntimeError(message)


def main():
    run = uuid.uuid4().hex
    source_credentials = secret(SOURCE_NS, f"{SOURCE}-app")
    source_host = f"{SOURCE}-rw.{SOURCE_NS}.svc"
    before, after = f"{run}-before", f"{run}-after"
    markers_need_cleanup = False
    resources: dict[str, str | None] = {}
    try:
        put_lease("running")
        with connect_source_at_startup(source_credentials, source_host) as db, db.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS public.platform_backup_validation (run_id text PRIMARY KEY, phase text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp())")
            cursor.execute("INSERT INTO public.platform_backup_validation(run_id, phase) VALUES (%s, 'before')", (before,))
            # A lost response to the context manager's commit may hide success.
            markers_need_cleanup = True
        with connect(source_credentials, source_host) as db, db.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp()")
            target = cursor.fetchone()[0].isoformat()
        time.sleep(2)
        with connect(source_credentials, source_host) as db, db.cursor() as cursor:
            cursor.execute("INSERT INTO public.platform_backup_validation(run_id, phase) VALUES (%s, 'after')", (after,))
        time.sleep(360)

        source_store = api(f"/apis/barmancloud.cnpg.io/v1/namespaces/{SOURCE_NS}/objectstores/k3s-postgres-backups")
        store = validation_store(source_store, os.environ["S3_ENDPOINT"])
        create_restore_resource(
            f"/apis/barmancloud.cnpg.io/v1/namespaces/{VALIDATION_NS}/objectstores", store, run, resources
        )
        storage_size = restore_storage_size(latest_backup_bytes())
        create_restore_resource(
            f"/apis/postgresql.cnpg.io/v1/namespaces/{VALIDATION_NS}/clusters",
            validation_cluster(target, storage_size), run, resources,
        )
        wait_ready()
        restored = secret(VALIDATION_NS, f"{RESTORE}-app")
        restore_host = f"{RESTORE}-rw.{VALIDATION_NS}.svc"
        with connect(restored, restore_host) as db, db.cursor() as cursor:
            cursor.execute("SELECT run_id, phase FROM public.platform_backup_validation WHERE run_id IN (%s,%s)", (before, after))
            rows = set(cursor.fetchall())
        if rows != {(before, "before")}:
            raise RuntimeError("restored rows do not match the requested recovery time")
        verify_app_databases(restore_host)
        if not mirror_matches_catalog():
            raise RuntimeError("OMV mirror does not contain every completed base-backup catalog entry")
    except Exception:
        put_lease("failed")
        raise
    finally:
        cleanup_errors = []
        for path, uid in reversed(resources.items()):
            try:
                if uid is None:
                    uid = reconcile_restore_uid(path, run)
                    if uid is None:
                        continue
                delete_options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}}
                api(path, "DELETE", delete_options, (404,))
            except Exception as exc:
                cleanup_errors.append(exc)
        if markers_need_cleanup:
            try:
                with connect(source_credentials, source_host) as db, db.cursor() as cursor:
                    cursor.execute("DELETE FROM public.platform_backup_validation WHERE run_id IN (%s,%s)", (before, after))
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            put_lease("failed-cleanup")
            raise RuntimeError("validation cleanup failed") from cleanup_errors[0]
    put_lease("success", restore_target_verified=True, mirror_verified=True, app_databases_verified=True)


if __name__ == "__main__":
    main()
