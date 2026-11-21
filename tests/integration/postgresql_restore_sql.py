"""Exercise the shipped verifier on a synthetic, private TLS PostgreSQL instance.

Run with initdb and pg_ctl on PATH and psycopg2 installed; no external DSN is accepted.
The fixture uses the queried columns, not a full application migration. Pinned sources:
https://github.com/grafana/grafana/blob/v13.2.4/pkg/registry/apis/dashboard/register.go
https://github.com/grafana/grafana/blob/v13.2.4/pkg/storage/unified/sql/db/migrations/resource_mig.go
https://github.com/grafana/grafana/blob/v13.2.4/pkg/services/sqlstore/migrations/datasource_mig.go
https://github.com/skyhook-io/radar/blob/v1.17.0/internal/timeline/migrations/postgres/0001_initial.sql
https://github.com/skyhook-io/radar/blob/v1.17.0/pkg/timeline/types.go
"""
import os
import secrets
import socket
import subprocess
import tempfile
import unittest
from contextlib import closing
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

import psycopg2

SCRIPT = Path(__file__).resolve().parents[2] / '08-backup-validation/resources/postgresql_restore_test.py'
SPEC = spec_from_file_location('validator', SCRIPT)
V = module_from_spec(SPEC)
SPEC.loader.exec_module(V)


class RestoreSQL(unittest.TestCase):
    def test_real_readonly_tls_queries(self):
        # Inherited libpq service/hostaddr options must never redirect fixture setup.
        with (patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith('PG')}, clear=True),
              tempfile.TemporaryDirectory() as tmp):
            data = Path(tmp) / 'db'
            subprocess.run(['initdb', '-D', data, '-U', 'postgres', '--no-locale', '-E', 'UTF8',
                            '--auth-local=trust', '--auth-host=scram-sha-256'], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj',
                            '/CN=localhost', '-keyout', data/'server.key', '-out', data/'server.crt'],
                           check=True, capture_output=True)
            (data/'server.key').chmod(0o600)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            with (data/'postgresql.conf').open('a') as f:
                f.write(f"\nlisten_addresses='127.0.0.1'\nssl=on\nport={port}\nunix_socket_directories='{tmp}'\n")
            # Admin access only via a Unix socket in a private directory.
            reader_rules = V.validation_cluster('2026-11-20T03:30:00Z', '20261120T030705')['spec']['postgresql']['pg_hba']
            (data/'pg_hba.conf').write_text('\n'.join(['local all postgres trust', *reader_rules,
                                                       'host all all all scram-sha-256', '']))
            ctl = ['pg_ctl', '-w', '-D', data]
            subprocess.run([*ctl, '-l', data/'server.log', 'start'], check=True)
            try:
                self.exercise(tmp, port)
            finally:
                subprocess.run([*ctl, '-m', 'immediate', 'stop'], check=True)

    def exercise(self, tmp, port):
        native_connect = psycopg2.connect
        def admin(db, sql, params=None):
            with closing(native_connect(host=tmp, port=port, user='postgres', dbname=db)) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(sql, params)

        def fixture_login(username):
            login = {'username': username}
            login['password'] = secrets.token_urlsafe(24)
            return login

        credentials = fixture_login(V.READER)
        admin('postgres', 'CREATE ROLE restore_reader LOGIN IN ROLE pg_read_all_data PASSWORD %s', (credentials['password'],))
        writer = fixture_login('app')
        admin('postgres', 'CREATE ROLE app LOGIN PASSWORD %s', (writer['password'],))
        admin('postgres', 'CREATE DATABASE app OWNER app')
        for db in ('grafana', 'radar'):
            admin('postgres', f'CREATE ROLE {db};')
            admin('postgres', f'CREATE DATABASE {db} OWNER {db}')
        admin('grafana', '''SET ROLE grafana;
            CREATE TABLE public.resource ("group" varchar, resource varchar, name varchar, value text);
            CREATE TABLE public.data_source (uid varchar, type varchar);
            INSERT INTO public.resource VALUES ('dashboard.grafana.app', 'dashboards', 'nodes',
                '{"kind":"Dashboard","metadata":{"name":"nodes"},"spec":{"title":"Node metrics"}}');
            INSERT INTO public.data_source VALUES ('prometheus','prometheus');''')
        admin('radar', '''SET ROLE radar;
            CREATE TABLE public.radar_timeline_events (timestamp bigint, seq bigint, id text, source text,
                kind text, name text, event_type text);
            CREATE TABLE public.radar_timeline_seen_resources (resource_key bytea, seen_at timestamptz);
            INSERT INTO public.radar_timeline_events VALUES (1234567890, 1, 'id', 'informer','Pod','sample','update');
            INSERT INTO public.radar_timeline_seen_resources VALUES ('sample', now());''')

        def loopback(**kwargs):
            if kwargs['user'] == 'app':
                self.assertEqual((kwargs['host'], kwargs['dbname']), ('k3s-postgres-rw.cloudnative-pg.svc', 'app'))
            else:
                self.assertEqual((kwargs['host'], kwargs['user']),
                                 ('postgresql-pitr-validation-rw.backup-validation.svc', V.READER))
            self.assertEqual(kwargs['port'], 5432)
            return native_connect(**{**kwargs, 'hostaddr': '127.0.0.1', 'port': port})

        with self.assertRaises(psycopg2.OperationalError):
            native_connect(host='127.0.0.1', port=port, dbname='grafana', user=V.READER,
                           password=credentials['password'], sslmode='disable')
        with self.assertRaises(psycopg2.OperationalError):
            native_connect(host='127.0.0.1', port=port, dbname='postgres', user=V.READER,
                           password=credentials['password'], sslmode='require')
        with patch.object(psycopg2, 'connect', side_effect=loopback):
            for setting in ('PGHOSTADDR', 'PGSERVICE'):
                with patch.dict(os.environ, {setting: 'unexpected'}), self.assertRaisesRegex(RuntimeError, 'settings'):
                    V.connect(credentials, 'grafana')
                with patch.dict(os.environ, {setting: 'unexpected'}), self.assertRaisesRegex(RuntimeError, 'settings'):
                    V.connect_source(writer)
            # Model the recovered marker table, not WAL replay, on this one private instance.
            with closing(V.connect_source(writer)) as db, db, db.cursor() as cur:
                self.assertTrue(db.info.ssl_in_use)
                cur.execute('CREATE TABLE public.platform_backup_validation (run_id text PRIMARY KEY, phase text)')
                cur.execute("INSERT INTO public.platform_backup_validation VALUES ('fixture-before', 'before')")
            with closing(V.connect(credentials, 'app')) as db, db.cursor() as cur:
                cur.execute('SELECT run_id, phase FROM public.platform_backup_validation')
                self.assertEqual(cur.fetchall(), [('fixture-before', 'before')])
                cur.execute("SELECT has_table_privilege('public.platform_backup_validation', 'INSERT')")
                self.assertEqual(cur.fetchone(), (False,))
            with closing(V.connect(credentials, 'radar')) as db, db.cursor() as cur:
                self.assertTrue(db.info.ssl_in_use)
                cur.execute("SELECT current_user, current_setting('transaction_read_only'), "
                            "has_table_privilege('public.radar_timeline_events', 'INSERT')")
                self.assertEqual(cur.fetchone(), (V.READER, 'on', False))
            V.check_restored_app_schemas(credentials)
            admin('radar', "UPDATE public.radar_timeline_events SET kind = ''")
            with self.assertRaisesRegex(RuntimeError, 'radar application data'):
                V.check_restored_app_schemas(credentials)
            admin('grafana', "UPDATE public.resource SET value = jsonb_set(value::jsonb, '{spec,title}', '{}'::jsonb)::text")
            with self.assertRaisesRegex(RuntimeError, 'grafana application data'):
                V.check_restored_app_schemas(credentials)
            admin('grafana', 'TRUNCATE public.resource, public.data_source')
            with self.assertRaisesRegex(RuntimeError, 'grafana application data'):
                V.check_restored_app_schemas(credentials)
            admin('grafana', 'ALTER TABLE public.resource OWNER TO postgres')
            with self.assertRaisesRegex(RuntimeError, 'grafana application schema'):
                V.check_restored_app_schemas(credentials)


if __name__ == '__main__':
    unittest.main()
