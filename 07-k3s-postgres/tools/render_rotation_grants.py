#!/usr/bin/env python3
"""Render an administrator migration from trusted postgres-consumer Helm output."""

import argparse
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode

IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
RESERVED_ROLES = {
    "app",
    "openbao_manager",
    "postgres",
    "public",
    "none",
    "current_role",
    "current_user",
    "session_user",
    "system_user",
}


def identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(
            "database and login names must be simple PostgreSQL identifiers"
        )
    return value


class ManifestLoader(yaml.SafeLoader):
    """Reject ambiguous keys in rendered manifests instead of taking the last one."""

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _ in node.value:
            if key_node.tag != "tag:yaml.org,2002:str":
                raise ValueError(f"expected a string YAML key at {key_node.start_mark}")
            key = key_node.value
            if not isinstance(key, str) or key in seen:
                raise ValueError(f"duplicate YAML key at {key_node.start_mark}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected Kubernetes manifest mappings")
    return value


def required(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"expected a nonempty string at {field}")
    return value


@dataclass(frozen=True)
class Database:
    name: str
    owner: str


def select_bindings(
    sources: Iterable[Iterable[object]],
    namespace: str,
    cluster: str,
    backend: str,
    provider: str,
) -> dict[str, str]:
    databases: dict[str, Database] = {}
    opted_in: dict[str, Database] = {}
    seen_databases: set[tuple[str, str]] = set()
    seen_roles: set[str] = set()
    for documents in sources:
        local_databases: dict[str, Database] = {}
        static_roles: dict[str, str] = {}
        for raw in documents:
            if raw is None:
                continue
            document = mapping(raw)
            key = (
                required(document.get("apiVersion"), "apiVersion"),
                required(document.get("kind"), "kind"),
            )
            if key[1].endswith("List") or "items" in document:
                raise ValueError(
                    "Kubernetes List input is not supported; render standalone manifests"
                )
            if key not in (
                ("postgresql.cnpg.io/v1", "Database"),
                ("database.vault.upbound.io/v1alpha1", "SecretBackendStaticRole"),
            ):
                continue
            spec = mapping(document.get("spec"))
            metadata = mapping(document.get("metadata"))
            name = required(metadata.get("name"), "metadata.name")
            if key[1] == "Database":
                db_namespace = required(metadata.get("namespace"), "metadata.namespace")
                identity = (db_namespace, name)
                if identity in seen_databases:
                    raise ValueError(
                        f"duplicate Database declaration: {db_namespace}/{name}"
                    )
                seen_databases.add(identity)
                db_cluster = required(
                    mapping(spec.get("cluster")).get("name"), "spec.cluster.name"
                )
                if (db_namespace, db_cluster) != (namespace, cluster):
                    continue
                database = Database(
                    required(spec.get("name"), "spec.name"),
                    required(spec.get("owner"), "spec.owner"),
                )
                local_databases[name] = databases[name] = database
            else:
                if name in seen_roles:
                    raise ValueError(f"duplicate static role declaration: {name}")
                seen_roles.add(name)
                ref = mapping(spec.get("providerConfigRef"))
                for_provider = mapping(spec.get("forProvider"))
                target_connection = (
                    required(for_provider.get("dbName"), "spec.forProvider.dbName"),
                    required(for_provider.get("backend"), "spec.forProvider.backend"),
                    required(ref.get("name"), "spec.providerConfigRef.name"),
                )
                if target_connection != (cluster, backend, provider):
                    continue
                if for_provider.get("name") != name:
                    raise ValueError(f"static role {name} must match its provider name")
                static_roles[name] = identifier(for_provider.get("username"))

        for name, login in static_roles.items():
            if login in RESERVED_ROLES or login.startswith("pg_"):
                raise ValueError(f"refusing reserved login: {login}")
            binding = local_databases.get(name)
            if binding is None or binding.owner != login:
                raise ValueError(
                    f"static role {name} must match its own Database and owner in the same render"
                )
            if binding.name in {"postgres", "template0", "template1"}:
                raise ValueError(
                    f"refusing maintenance or template database: {binding.name}"
                )
            opted_in[name] = binding

    for name, database in opted_in.items():
        if any(
            other != name and (db.owner == database.owner or db.name == database.name)
            for other, db in databases.items()
        ):
            raise ValueError(f"more than one declaration for login or database: {name}")
    return {database.owner: identifier(database.name) for database in opted_in.values()}


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
    IF pg_catalog.pg_is_in_recovery() OR current_user <> 'postgres'
       OR pg_catalog.current_database() <> 'postgres' THEN
        RAISE EXCEPTION 'Run rotation grants as postgres in the postgres database on the writable primary';
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
               OR (datname <> binding.database_name
                   AND pg_catalog.pg_has_role(target, datdba, 'MEMBER'))
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
        ) OR pg_catalog.pg_has_role(manager, target, 'SET') OR EXISTS (
            SELECT FROM pg_catalog.pg_roles r
            WHERE pg_catalog.pg_has_role(manager, r.oid, 'SET')
              AND pg_catalog.pg_has_role(r.oid, target, 'USAGE')
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
    parser.add_argument(
        "--namespace", required=True, help="namespace of the PostgreSQL Cluster"
    )
    parser.add_argument(
        "--cluster",
        required=True,
        help="PostgreSQL Cluster and OpenBao connection name",
    )
    parser.add_argument("--backend", default="database", help="OpenBao database mount")
    parser.add_argument(
        "--provider", default="openbao", help="OpenBao provider config name"
    )
    parser.add_argument(
        "renders",
        nargs="+",
        type=Path,
        help="separate render file for each consumer or ordinary database source",
    )
    args = parser.parse_args()
    try:
        sources = (
            yaml.load_all(path.read_text(encoding="utf-8"), Loader=ManifestLoader)
            for path in args.renders
        )
        logins = select_bindings(
            sources, args.namespace, args.cluster, args.backend, args.provider
        )
        sys.stdout.write(render_sql(logins))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(1, f"Cannot render rotation grants: {exc}\n")


if __name__ == "__main__":
    main()
