#!/usr/bin/env python3
"""Verify grant reconciliation and rotation after a disposable physical PostgreSQL 18 restore."""

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

from test_postgres_consumer import render, rotation_sql, values

APP_ROLES = ("grafana", "radar")
ORIGINAL_PASSWORD = "restore-test-original"
ROTATED_PASSWORD = "restore-test-rotated"
MANAGER_PASSWORD = "restore-test-manager"
interrupted = False


def stop_test(_signum: int, _frame: object) -> None:
    # Do not interrupt child acquisition or process teardown.
    global interrupted
    interrupted = True


def check_interrupted() -> None:
    if interrupted:
        raise SystemExit("restore test interrupted")


class LocalPostgres:
    """Run only PostgreSQL instances owned by this test, with private Unix sockets."""

    def __init__(self, binaries: Path, work: Path) -> None:
        self.binaries = binaries.resolve()
        self.socket = work / "socket"
        self.socket.mkdir(mode=0o700)
        home = work / "home"
        home.mkdir(mode=0o700)
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(home),
            "LC_ALL": "C",
        }
        empty_passfile = home / "pgpass"
        empty_passfile.touch(mode=0o600)
        self.env["PGPASSFILE"] = str(empty_passfile)

    def run(
        self, tool: str, *args: str, sql: str | None = None, password: str | None = None
    ) -> str:
        check_interrupted()
        env = self.env if password is None else self.env | {"PGPASSWORD": password}
        command = [str(self.binaries / tool), *args]
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE if sql is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        ) as client:
            deadline = time.monotonic() + 120
            try:
                while True:
                    check_interrupted()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, 120)
                    try:
                        output, _ = client.communicate(sql, timeout=min(remaining, 1))
                        break
                    except subprocess.TimeoutExpired:
                        sql = (
                            None  # communicate() retains pending input across retries.
                        )
            finally:
                if client.poll() is None:
                    client.kill()
                    client.wait()
            if client.returncode:
                raise subprocess.CalledProcessError(client.returncode, command)
        check_interrupted()
        return output.strip()

    @contextmanager
    def running(self, data: Path) -> Iterator[None]:
        check_interrupted()
        with (data / "test.log").open("w") as log:
            server = subprocess.Popen(
                [
                    self.binaries / "postgres",
                    "-D",
                    str(data),
                    "-c",
                    "listen_addresses=",
                    "-k",
                    str(self.socket),
                ],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                for _ in range(100):
                    check_interrupted()
                    if server.poll() is not None:
                        raise RuntimeError(
                            f"test PostgreSQL exited during startup ({server.returncode})"
                        )
                    result = subprocess.run(
                        [
                            self.binaries / "pg_isready",
                            "-q",
                            "-t",
                            "1",
                            "-h",
                            str(self.socket),
                            "-U",
                            "postgres",
                            "-d",
                            "postgres",
                        ],
                        env=self.env,
                        check=False,
                        timeout=2,
                    )
                    if result.returncode == 0:
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError("test PostgreSQL did not become ready")
                yield
            finally:
                if server.poll() is None:
                    server.send_signal(
                        signal.SIGINT
                    )  # Fast shutdown of this exact test process.
                    try:
                        server.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
        check_interrupted()

    def query(
        self,
        sql: str,
        login: str = "postgres",
        database: str = "postgres",
        password: str | None = None,
    ) -> str:
        return self.run(
            "psql",
            "-X",
            "-w",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            str(self.socket),
            "-U",
            login,
            "-d",
            database,
            sql=sql,
            password=password,
        )


def rendered_sql(env: dict[str, str], system_id: str) -> tuple[str, list[str]]:
    manifests = [
        render(
            values(login),
            "kube-prometheus-stack" if login == "grafana" else login,
            env=env,
        ).stdout
        for login in APP_ROLES
    ]
    hook = (
        "--argo-hook",
        "k3s-postgres-1",
        "fce60004-619e-42ac-9888-117201afcbcb",
        "85d30f18-8bb9-4e7f-8629-575a8b75760b",
        system_id,
    )
    migration = rotation_sql(*manifests, env=env, hook=hook)
    migration.check_returncode()
    (config,) = (
        doc
        for doc in yaml.safe_load_all(migration.stdout)
        if doc["kind"] == "ConfigMap"
    )
    statements = []
    for manifest in manifests:
        (role,) = (
            doc
            for doc in yaml.safe_load_all(manifest)
            if doc["kind"] == "SecretBackendStaticRole"
        )
        (statement,) = role["spec"]["forProvider"]["rotationStatements"]
        (sql,) = json.loads(statement)
        statements.append(sql.replace("{{password}}", ROTATED_PASSWORD))
    migration_sql: str = config["data"]["grants.sql"]
    return migration_sql, statements


def check_restore(pg: LocalPostgres, work: Path) -> None:
    source, restored = work / "source", work / "restored"
    pg.run(
        "initdb",
        "-D",
        str(source),
        "-U",
        "postgres",
        "--auth-local=trust",
        "--auth-host=reject",
        "--no-locale",
    )
    with pg.running(source):
        pg.query(
            f"CREATE ROLE openbao_manager LOGIN CREATEROLE PASSWORD '{MANAGER_PASSWORD}'; CREATE ROLE ordinary LOGIN;"
        )
        for login in APP_ROLES:
            pg.query(f"CREATE ROLE {login} LOGIN PASSWORD '{ORIGINAL_PASSWORD}';")
            pg.query(f"CREATE DATABASE {login} OWNER {login};")
            pg.query(
                f"CREATE TABLE restore_marker (id int); INSERT INTO restore_marker VALUES (42); ALTER TABLE restore_marker OWNER TO {login};",
                database=login,
            )
        # Save one missing and one incorrect same-grantor membership in the backup.
        pg.query(
            "GRANT radar TO openbao_manager WITH ADMIN FALSE, INHERIT TRUE, SET TRUE;"
        )
        pg.run(
            "pg_basebackup",
            "--no-password",
            "-h",
            str(pg.socket),
            "-U",
            "postgres",
            "-D",
            str(restored),
            "-X",
            "stream",
            "--checkpoint=fast",
        )

    pg.run("pg_verifybackup", str(restored))
    hba = restored / "pg_hba.conf"
    hba.write_text(
        "local all grafana,radar,openbao_manager scram-sha-256\n" + hba.read_text()
    )
    membership = """
        SELECT pg_get_userbyid(roleid), admin_option, inherit_option, set_option
        FROM pg_auth_members WHERE member = 'openbao_manager'::regrole ORDER BY 1;
    """
    with pg.running(restored):
        system_id = pg.query("SELECT system_identifier FROM pg_control_system();")
        migration, rotation_statements = rendered_sql(pg.env, system_id)
        wrong_id = "2" if system_id == "1" else "1"
        wrong_cluster, _ = rendered_sql(pg.env, wrong_id)
        try:
            pg.query(wrong_cluster)
        except subprocess.CalledProcessError as exc:
            assert (
                exc.returncode == 3
            ), "psql must reject a mismatched system identifier"
        else:
            raise AssertionError("migration accepted a mismatched system identifier")
        assert (
            pg.query(membership) == "radar|f|t|t"
        ), "backup did not contain the expected failing grants"
        for login in APP_ROLES:
            assert (
                pg.query(
                    "SELECT id FROM restore_marker;", login, login, ORIGINAL_PASSWORD
                )
                == "42"
            )
        for _ in range(2):
            pg.query(migration)
            assert pg.query(membership) == "grafana|t|f|f\nradar|t|f|f"
        for login in APP_ROLES:
            assert (
                pg.query(
                    "SELECT id FROM restore_marker;", login, login, ORIGINAL_PASSWORD
                )
                == "42"
            )
        for sql in rotation_statements:
            pg.query(sql, login="openbao_manager", password=MANAGER_PASSWORD)
        for login in APP_ROLES:
            assert (
                pg.query(
                    "SELECT id FROM restore_marker;", login, login, ROTATED_PASSWORD
                )
                == "42"
            )
    print(
        "PASS: physical backup verified, restore grants reconciled twice, rendered rotation SQL and application logins work"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pg_bin", type=Path, help="directory containing native PostgreSQL 18 binaries"
    )
    args = parser.parse_args()
    if not __debug__:
        parser.error("run without Python optimization so assertions remain enabled")
    signal.signal(signal.SIGTERM, stop_test)
    signal.signal(signal.SIGINT, stop_test)
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="pg-grants-") as directory:
        work = Path(directory)
        pg = LocalPostgres(args.pg_bin, work)
        version = pg.run("postgres", "--version")
        if not version.startswith("postgres (PostgreSQL) 18."):
            parser.error(f"expected native PostgreSQL 18, got {version!r}")
        print(version, flush=True)
        check_restore(pg, work)


if __name__ == "__main__":
    main()
