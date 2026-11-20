#!/usr/bin/env python3
"""Render an administrator migration from trusted postgres-consumer Helm output."""

import argparse
import re
import sys
from collections.abc import Iterable
from typing import Any

import yaml

IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
RESERVED_ROLES = {
    "app", "openbao_manager", "postgres", "public", "none",
    "current_role", "current_user", "session_user", "system_user",
}


def identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("database and login names must be simple PostgreSQL identifiers")
    return value


def mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected Kubernetes manifest mappings")
    return value


def select_logins(
    documents: Iterable[Any], namespace: str, cluster: str, backend: str, provider: str
) -> dict[str, str]:
    databases: dict[str, dict[str, Any]] = {}
    static_roles: dict[str, dict[str, Any]] = {}
    for document in documents:
        if document is None:
            continue
        document = mapping(document)
        key = (document.get("apiVersion"), document.get("kind"))
        if key not in (("postgresql.cnpg.io/v1", "Database"),
                       ("database.vault.upbound.io/v1alpha1", "SecretBackendStaticRole")):
            continue
        spec = mapping(document.get("spec"))
        metadata = mapping(document.get("metadata"))
        if key[1] == "Database":
            if metadata.get("namespace") != namespace or mapping(spec.get("cluster")).get("name") != cluster:
                continue
            registry = databases
        else:
            ref = mapping(spec.get("providerConfigRef"))
            spec = mapping(spec.get("forProvider"))
            if (spec.get("dbName"), spec.get("backend"), ref.get("name")) != (cluster, backend, provider):
                continue
            registry = static_roles
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("expected a nonempty metadata.name")
        if name in registry:
            raise ValueError(f"duplicate declaration: {name}")
        registry[name] = spec

    logins: dict[str, str] = {}
    for name, static_role in sorted(static_roles.items()):
        login = identifier(static_role.get("username"))
        if login in RESERVED_ROLES or login.startswith("pg_"):
            raise ValueError(f"refusing reserved login: {login}")
        database = databases.get(name)
        if static_role.get("name") != name or not database or database.get("owner") != login:
            raise ValueError(f"static role {name} must match its own Database and owner")
        dbname = identifier(database.get("name"))
        if any(other != name and (spec.get("owner") == login or spec.get("name") == dbname)
               for other, spec in databases.items()):
            raise ValueError(f"more than one declaration for login or database: {name}")
        logins[login] = dbname
    return logins


def render_sql(logins: dict[str, str]) -> str:
    if not logins:
        return "-- No opted-in PostgreSQL logins.\n"
    # Names are restricted to simple identifiers before interpolation.
    bindings = ",\n        ".join(
        f"('{identifier(login)}', '{identifier(database)}')"
        for login, database in sorted(logins.items())
    )
    return f"""BEGIN;
SET LOCAL search_path = pg_catalog;
DO $rotation_grants$
DECLARE
    binding record;
    manager oid;
    target oid;
BEGIN
    IF pg_catalog.pg_is_in_recovery() OR current_user <> 'postgres' THEN
        RAISE EXCEPTION 'Run rotation grants as postgres on the writable primary';
    END IF;
    SELECT oid INTO manager FROM pg_catalog.pg_roles
    WHERE rolname = 'openbao_manager' AND rolcanlogin AND rolcreaterole
      AND NOT (rolsuper OR rolreplication OR rolbypassrls OR rolcreatedb);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'The restricted openbao_manager login must be provisioned first';
    END IF;

    FOR binding IN SELECT * FROM (VALUES
        {bindings}
    ) AS requested(login_name, database_name) LOOP
        SELECT oid INTO target FROM pg_catalog.pg_roles WHERE rolname = binding.login_name;
        IF EXISTS (
            SELECT FROM pg_catalog.pg_database
            WHERE (datname = binding.database_name AND datdba IS DISTINCT FROM target)
               OR (datdba = target AND datname <> binding.database_name)
        ) THEN
            RAISE EXCEPTION 'Database ownership does not match rotation declaration: %', binding.login_name;
        END IF;
        IF target IS NULL THEN
            EXECUTE pg_catalog.format('CREATE ROLE %I LOGIN', binding.login_name);
            SELECT oid INTO STRICT target FROM pg_catalog.pg_roles WHERE rolname = binding.login_name;
        ELSIF EXISTS (
            SELECT FROM pg_catalog.pg_roles r
            WHERE (oid = target AND (NOT rolcanlogin OR rolsuper OR rolreplication OR rolbypassrls
                                     OR rolcreatedb OR rolcreaterole))
               OR ((rolsuper OR rolreplication OR rolbypassrls OR rolcreatedb OR rolcreaterole
                    OR left(rolname, 3) = 'pg_') AND pg_catalog.pg_has_role(target, oid, 'MEMBER'))
        ) OR EXISTS (
            SELECT FROM pg_catalog.pg_roles r
            WHERE r.oid <> target AND r.rolcanlogin
              AND pg_catalog.pg_has_role(target, r.oid, 'MEMBER')
        ) THEN
            RAISE EXCEPTION 'Refusing privileged or cross-login role: %', binding.login_name;
        END IF;
        EXECUTE pg_catalog.format(
            'GRANT %I TO openbao_manager WITH ADMIN TRUE, INHERIT FALSE, SET FALSE', binding.login_name
        );
        IF (SELECT count(*) FROM pg_catalog.pg_auth_members
            WHERE roleid = target AND member = manager) <> 1 OR NOT EXISTS (
            SELECT FROM pg_catalog.pg_auth_members WHERE roleid = target AND member = manager
              AND admin_option AND NOT inherit_option AND NOT set_option
        ) THEN
            RAISE EXCEPTION 'Unexpected or conflicting grant for %', binding.login_name;
        END IF;
    END LOOP;
END;
$rotation_grants$;
COMMIT;
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True, help="namespace of the PostgreSQL Cluster")
    parser.add_argument("--cluster", required=True, help="PostgreSQL Cluster and OpenBao connection name")
    parser.add_argument("--backend", default="database", help="OpenBao database mount")
    parser.add_argument("--provider", default="openbao", help="OpenBao provider config name")
    args = parser.parse_args()
    try:
        logins = select_logins(
            yaml.safe_load_all(sys.stdin), args.namespace, args.cluster, args.backend, args.provider
        )
        print(render_sql(logins), end="")
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        parser.exit(1, f"Cannot render rotation grants: {exc}\n")


if __name__ == "__main__":
    main()
