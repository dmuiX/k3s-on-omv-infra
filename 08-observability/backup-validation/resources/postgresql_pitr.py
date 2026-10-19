#!/venv/bin/python3
"""Run an isolated CNPG point-in-time recovery and publish bounded evidence."""
import base64, copy, json, os, re, ssl, subprocess, sys, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import psycopg2

API = "https://kubernetes.default.svc"
TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text().strip()
TLS = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
SOURCE_NS, VALIDATION_NS = "cloudnative-pg", "backup-validation"
SOURCE, RESTORE, STORE = "k3s-postgres", "postgresql-pitr-validation", "postgresql-pitr-validation"
IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.6-system-bookworm@sha256:b4bec141fa5ca2bab490b197c814fb1342147c8405be1fb0673951fa6db7f699"


def api(path, method="GET", body=None, allowed=()):
    data = None if body is None else json.dumps(body).encode()
    try:
        with urlopen(Request(API + path, data=data, method=method, headers=HEADERS), context=TLS, timeout=30) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except HTTPError as exc:
        if exc.code in allowed:
            return None
        raise RuntimeError(f"Kubernetes API {method} {path} failed with HTTP {exc.code}") from exc


def secret(namespace, name):
    payload = api(f"/api/v1/namespaces/{namespace}/secrets/{name}")["data"]
    return {key: base64.b64decode(value).decode() for key, value in payload.items()}


def connect(values, host):
    return psycopg2.connect(host=host, port=values.get("port", "5432"), dbname=values.get("dbname", "app"), user=values["username"], password=values["password"], connect_timeout=15, sslmode="require")


def put_lease(result, pitr=False, mirror=False):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    body = {"apiVersion":"coordination.k8s.io/v1","kind":"Lease","metadata":{"name":"postgresql-restore-status","namespace":VALIDATION_NS,"annotations":{"k3s-on-omv.io/result":result,"k3s-on-omv.io/pitr-verified":str(pitr).lower(),"k3s-on-omv.io/omv-mirror-verified":str(mirror).lower()}},"spec":{"holderIdentity":"postgresql-pitr-validation","renewTime":now,"leaseDurationSeconds":691200}}
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


def main():
    run = uuid.uuid4().hex
    source_credentials = secret(SOURCE_NS, f"{SOURCE}-app")
    source_host = f"{SOURCE}-rw.{SOURCE_NS}.svc"
    before, after = f"{run}-before", f"{run}-after"
    try:
        put_lease("running")
        with connect(source_credentials, source_host) as db, db.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS public.platform_backup_validation (run_id text PRIMARY KEY, phase text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp())")
            cursor.execute("INSERT INTO public.platform_backup_validation(run_id, phase) VALUES (%s, 'before')", (before,))
        with connect(source_credentials, source_host) as db, db.cursor() as cursor:
            cursor.execute("SELECT clock_timestamp()")
            target = cursor.fetchone()[0].isoformat()
        time.sleep(2)
        with connect(source_credentials, source_host) as db, db.cursor() as cursor:
            cursor.execute("INSERT INTO public.platform_backup_validation(run_id, phase) VALUES (%s, 'after')", (after,))
        time.sleep(360)

        source_store = api(f"/apis/barmancloud.cnpg.io/v1/namespaces/{SOURCE_NS}/objectstores/k3s-postgres-backups")
        store = {"apiVersion":source_store["apiVersion"],"kind":"ObjectStore","metadata":{"name":STORE,"namespace":VALIDATION_NS},"spec":copy.deepcopy(source_store["spec"])}
        store["spec"]["configuration"]["endpointURL"] = os.environ["S3_ENDPOINT"]
        creds = store["spec"]["configuration"]["s3Credentials"]
        creds["accessKeyId"]["name"] = creds["secretAccessKey"]["name"] = "postgresql-validation-r2"
        api(f"/apis/barmancloud.cnpg.io/v1/namespaces/{VALIDATION_NS}/objectstores", "POST", store)
        cluster = {"apiVersion":"postgresql.cnpg.io/v1","kind":"Cluster","metadata":{"name":RESTORE,"namespace":VALIDATION_NS},"spec":{"instances":1,"imageName":IMAGE,"storage":{"storageClass":"longhorn-local","size":"5Gi"},"bootstrap":{"recovery":{"source":"origin","recoveryTarget":{"targetTime":target}}},"externalClusters":[{"name":"origin","plugin":{"enabled":True,"name":"barman-cloud.cloudnative-pg.io","parameters":{"barmanObjectName":STORE,"serverName":SOURCE}}}]}}
        api(f"/apis/postgresql.cnpg.io/v1/namespaces/{VALIDATION_NS}/clusters", "POST", cluster)
        wait_ready()
        restored = secret(VALIDATION_NS, f"{RESTORE}-app")
        with connect(restored, f"{RESTORE}-rw.{VALIDATION_NS}.svc") as db, db.cursor() as cursor:
            cursor.execute("SELECT run_id, phase FROM public.platform_backup_validation WHERE run_id IN (%s,%s)", (before, after))
            rows = set(cursor.fetchall())
        if rows != {(before, "before")}:
            raise RuntimeError("PITR boundary validation failed")
        if not mirror_matches_catalog():
            raise RuntimeError("OMV mirror does not contain every completed base-backup catalog entry")
        put_lease("success", True, True)
    except Exception:
        put_lease("failed")
        raise
    finally:
        cleanup_errors = []
        for path in (
            f"/apis/postgresql.cnpg.io/v1/namespaces/{VALIDATION_NS}/clusters/{RESTORE}",
            f"/apis/barmancloud.cnpg.io/v1/namespaces/{VALIDATION_NS}/objectstores/{STORE}",
        ):
            try:
                api(path, "DELETE", {}, (404,))
            except Exception as exc:
                cleanup_errors.append(exc)
        try:
            with connect(source_credentials, source_host) as db, db.cursor() as cursor:
                cursor.execute("DELETE FROM public.platform_backup_validation WHERE run_id IN (%s,%s)", (before, after))
        except Exception as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            put_lease("failed-cleanup")
            raise RuntimeError("validation cleanup failed") from cleanup_errors[0]


if __name__ == "__main__":
    main()
