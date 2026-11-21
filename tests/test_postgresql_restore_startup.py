import json
import sys
import unittest
from contextlib import nullcontext, redirect_stderr
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, Mock, call, patch
from urllib.error import HTTPError
from uuid import UUID

SCRIPT = (
    Path(__file__).parents[1]
    / "08-backup-validation/resources/postgresql_restore_test.py"
)
SPEC = spec_from_file_location("postgresql_restore_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

CLOSED = "server closed the connection unexpectedly"
SOURCE = "k3s-postgres-rw.cloudnative-pg.svc"
CLUSTER = "/apis/postgresql.cnpg.io/v1/namespaces/backup-validation/clusters"
STORE = "/apis/barmancloud.cnpg.io/v1/namespaces/backup-validation/objectstores"
STORE_PATH = f"{STORE}/postgresql-restore-test"
CLUSTER_PATH = f"{CLUSTER}/postgresql-restore-test"
SOURCE_STORE = "/apis/barmancloud.cnpg.io/v1/namespaces/cloudnative-pg/objectstores/k3s-postgres-backups"
RUN = UUID(int=1)
RUN_ANNOTATION = "k3s-on-omv.io/restore-run-id"
STORE_CREATED = {"metadata": {"uid": "store-uid"}}
CLUSTER_CREATED = {"metadata": {"uid": "cluster-uid"}}


def delete_options(uid: str) -> dict:
    return {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}}


class OperationalError(Exception):
    pass


class PostgreSQLRestoreStartupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connect = Mock()
        self.enterContext(
            patch.dict(
                sys.modules,
                psycopg2=SimpleNamespace(
                    connect=self.connect,
                    OperationalError=OperationalError,
                ),
            )
        )
        self.enterContext(
            patch.object(
                VALIDATOR,
                "secret",
                return_value={
                    "username": "fixture",
                    "password": "fixture",
                },
            )
        )
        self.api = self.enterContext(
            patch.object(VALIDATOR, "api", wraps=VALIDATOR.api)
        )
        self.enterContext(
            patch.object(VALIDATOR, "api_settings", return_value=({}, None))
        )
        self.urlopen = self.enterContext(patch.object(VALIDATOR, "urlopen"))
        self.lease = self.enterContext(patch.object(VALIDATOR, "put_lease"))
        self.sleep = self.enterContext(patch.object(VALIDATOR.time, "sleep"))
        self.cursor = MagicMock()
        self.db = MagicMock()
        self.db.__enter__.return_value = self.db
        self.db.cursor.return_value.__enter__.return_value = self.cursor

    def assert_startup_failure(self, error: OperationalError) -> None:
        self.connect.side_effect = error
        with self.assertRaises(OperationalError) as caught:
            VALIDATOR.main()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.lease.call_args_list, [call("running"), call("failed")])
        self.api.assert_not_called()
        self.db.cursor.assert_not_called()

    def test_persistent_disconnect_stops_without_cleanup(self) -> None:
        self.assert_startup_failure(OperationalError(CLOSED))
        self.assertEqual(self.connect.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2), call(4)])

    def test_other_startup_errors_are_not_retried(self) -> None:
        for message in (
            'FATAL: password authentication failed for user "fixture"',
            f'{CLOSED}\nFATAL: password authentication failed for user "fixture"',
            "Connection refused",
        ):
            with self.subTest(message=message):
                self.assert_startup_failure(OperationalError(message))
                self.connect.assert_called_once()
                self.sleep.assert_not_called()
                self.connect.reset_mock()
                self.lease.reset_mock()

    def respond(self, *responses: dict | Exception) -> None:
        self.urlopen.side_effect = [
            (
                nullcontext(Mock(read=Mock(return_value=json.dumps(value).encode())))
                if isinstance(value, dict)
                else value
            )
            for value in responses
        ]

    def prepare_restore(self) -> None:
        now = 0.0

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        self.sleep.side_effect = advance
        self.enterContext(
            patch.object(VALIDATOR.time, "monotonic", side_effect=lambda: now)
        )
        self.enterContext(patch.object(VALIDATOR.uuid, "uuid4", return_value=RUN))
        self.enterContext(
            patch.dict(
                VALIDATOR.os.environ, S3_ENDPOINT="https://fixture.example.invalid"
            )
        )
        self.enterContext(patch.object(VALIDATOR, "wait_ready"))
        self.enterContext(
            patch.object(VALIDATOR, "mirror_matches_catalog", return_value=True)
        )
        self.source_store = {
            "apiVersion": "barmancloud.cnpg.io/v1",
            "spec": {
                "configuration": {
                    "s3Credentials": {"accessKeyId": {}, "secretAccessKey": {}}
                }
            },
        }
        self.respond(self.source_store, STORE_CREATED, CLUSTER_CREATED, {}, {})
        self.cursor.fetchone.return_value = (datetime(2026, 1, 1, tzinfo=timezone.utc),)
        self.cursor.fetchall.return_value = [(f"{RUN.hex}-before", "before")]
        self.connect.return_value = self.db

    def test_transient_disconnect_retries_only_the_initial_connection(self) -> None:
        self.prepare_restore()
        self.connect.side_effect = [
            OperationalError(
                f'connection to server at "{SOURCE}" (192.0.2.1), port 5432 failed: {CLOSED}\n'
                "\tThis probably means the server terminated abnormally\n"
                "\tbefore or while processing the request.\n"
            )
        ] + [self.db] * 5
        VALIDATOR.main()
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2), call(360)])
        self.assertEqual(self.connect.call_args_list[0], self.connect.call_args_list[1])
        initial = self.connect.call_args_list[0].kwargs
        self.assertEqual(initial["host"], SOURCE)
        self.assertEqual(initial["connect_timeout"], 15)
        self.assertEqual(initial["sslmode"], "require")
        self.assertEqual(
            [c.args[0].split()[0] for c in self.cursor.execute.call_args_list],
            ["CREATE", "INSERT", "SELECT", "INSERT", "SELECT", "DELETE"],
        )
        self.assertEqual(
            self.lease.call_args_list, [call("running"), call("success", restore_target_verified=True, mirror_verified=True)]
        )

    def test_later_source_connect_error_is_not_retried_but_markers_are_cleaned(
        self,
    ) -> None:
        error = OperationalError(CLOSED)
        self.connect.side_effect = [self.db, error, self.db]
        with self.assertRaises(OperationalError) as caught:
            VALIDATOR.main()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.connect.call_count, 3)
        self.sleep.assert_not_called()
        self.api.assert_not_called()
        self.assertEqual(
            [c.args[0].split()[0] for c in self.cursor.execute.call_args_list],
            ["CREATE", "INSERT", "DELETE"],
        )
        self.assertEqual(self.lease.call_args_list, [call("running"), call("failed")])

    def test_failed_first_insert_rolls_back_without_cleanup_sql(self) -> None:
        error = OperationalError(CLOSED)
        self.connect.return_value = self.db
        self.cursor.execute.side_effect = [None, error]
        with self.assertRaises(OperationalError) as caught:
            VALIDATOR.main()
        self.assertIs(caught.exception, error)
        self.db.__exit__.assert_called_once_with(OperationalError, error, ANY)
        self.connect.assert_called_once()
        self.api.assert_not_called()
        self.assertEqual(
            [c.args[0].split()[0] for c in self.cursor.execute.call_args_list],
            ["CREATE", "INSERT"],
        )
        self.assertEqual(self.lease.call_args_list, [call("running"), call("failed")])

    def test_marker_commit_error_is_not_retried_but_markers_are_cleaned(self) -> None:
        error = OperationalError(CLOSED)
        self.connect.return_value = self.db
        self.db.__exit__.side_effect = [error, None]
        with self.assertRaises(OperationalError) as caught:
            VALIDATOR.main()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.connect.call_count, 2)
        self.sleep.assert_not_called()
        self.api.assert_not_called()
        self.assertEqual(
            [c.args[0].split()[0] for c in self.cursor.execute.call_args_list],
            ["CREATE", "INSERT", "DELETE"],
        )
        self.assertEqual(self.lease.call_args_list, [call("running"), call("failed")])

    def test_already_exists_never_reads_or_deletes_the_collision(self) -> None:
        self.prepare_restore()
        for collection in (STORE, CLUSTER):
            with self.subTest(collection=collection):
                self.api.reset_mock()
                self.lease.reset_mock()
                error = HTTPError(collection, 409, "AlreadyExists", {}, BytesIO())
                if collection == STORE:
                    self.respond(self.source_store, error)
                    expected = [call(STORE, "POST", ANY)]
                else:
                    self.respond(self.source_store, STORE_CREATED, error, {})
                    expected = [
                        call(STORE, "POST", ANY),
                        call(CLUSTER, "POST", ANY),
                        call(STORE_PATH, "DELETE", delete_options("store-uid"), (404,)),
                    ]
                with self.assertRaisesRegex(RuntimeError, "HTTP 409") as caught:
                    VALIDATOR.main()
                self.assertIs(caught.exception.__cause__, error)
                self.assertEqual(
                    self.api.call_args_list, [call(SOURCE_STORE), *expected]
                )
                self.assertEqual(
                    self.lease.call_args_list, [call("running"), call("failed")]
                )

    def test_ambiguous_create_only_deletes_an_object_stamped_by_this_run(self) -> None:
        self.prepare_restore()
        for collection, path, error in (
            (STORE, STORE_PATH, TimeoutError("create response lost")),
            (CLUSTER, CLUSTER_PATH, HTTPError(CLUSTER, 504, "Timeout", {}, BytesIO())),
        ):
            for owner in (RUN.hex, "other-run", None):
                with self.subTest(collection=collection, owner=owner):
                    self.api.reset_mock()
                    self.lease.reset_mock()
                    found = {"metadata": {"uid": "read-uid", "annotations": {}}}
                    if owner is not None:
                        found["metadata"]["annotations"][RUN_ANNOTATION] = owner
                    prefix = [STORE_CREATED] if collection == CLUSTER else []
                    expected = [call(SOURCE_STORE)]
                    if prefix:
                        expected.append(call(STORE, "POST", ANY))
                    expected += [
                        call(collection, "POST", ANY),
                        call(path, allowed=(404,), timeout=ANY),
                        call(path, allowed=(404,), timeout=ANY),
                    ]
                    deletes = []
                    if owner == RUN.hex:
                        deletes.append((path, "read-uid"))
                    if collection == CLUSTER:
                        deletes.append((STORE_PATH, "store-uid"))
                    expected += [
                        call(endpoint, "DELETE", delete_options(uid), (404,))
                        for endpoint, uid in deletes
                    ]
                    not_found = HTTPError(path, 404, "NotFound", {}, BytesIO())
                    self.respond(
                        self.source_store,
                        *prefix,
                        error,
                        not_found,
                        found,
                        *([{}] * len(deletes)),
                    )
                    with self.assertRaises((TimeoutError, RuntimeError)) as caught:
                        VALIDATOR.main()
                    self.assertIs(caught.exception.__cause__ or caught.exception, error)
                    self.assertEqual(self.api.call_args_list, expected)
                    self.assertEqual(
                        self.lease.call_args_list, [call("running"), call("failed")]
                    )

    def test_ambiguous_create_without_uid_is_not_deleted(self) -> None:
        self.prepare_restore()
        self.respond(
            self.source_store,
            TimeoutError("create response lost"),
            {"metadata": {"annotations": {RUN_ANNOTATION: RUN.hex}}},
        )
        with self.assertRaisesRegex(RuntimeError, "validation cleanup failed") as caught:
            VALIDATOR.main()
        self.assertIn("no UID", str(caught.exception.__cause__))
        self.assertEqual(
            self.api.call_args_list,
            [
                call(SOURCE_STORE),
                call(STORE, "POST", ANY),
                call(STORE_PATH, allowed=(404,), timeout=ANY),
            ],
        )
        self.lease.assert_called_with("failed-cleanup")

    def test_ambiguous_create_missing_at_deadline_reports_unresolved(self) -> None:
        self.prepare_restore()
        error = TimeoutError("create response lost")
        missing = [
            HTTPError(CLUSTER_PATH, 404, "NotFound", {}, BytesIO()) for _ in range(30)
        ]
        self.respond(self.source_store, STORE_CREATED, error, *missing, {})
        with redirect_stderr(StringIO()) as stderr, self.assertRaisesRegex(
            RuntimeError, "validation cleanup failed"
        ) as caught:
            VALIDATOR.main()
        self.assertIn("ambiguous create unresolved", str(caught.exception.__cause__))
        self.assertIn("ambiguous create unresolved", stderr.getvalue())
        self.assertIn(CLUSTER_PATH, stderr.getvalue())
        self.assertIn(RUN.hex, stderr.getvalue())
        self.assertEqual(
            self.api.call_args_list[3:],
            [
                call(CLUSTER_PATH, allowed=(404,), timeout=min(30, remaining))
                for remaining in range(60, 0, -2)
            ] + [call(STORE_PATH, "DELETE", delete_options("store-uid"), (404,))],
        )
        self.assertEqual(self.urlopen.call_args_list[-2].kwargs["timeout"], 2)
        self.assertTrue(self.cursor.execute.call_args.args[0].startswith("DELETE FROM "))
        self.assertEqual(
            self.lease.call_args_list,
            [call("running"), call("failed"), call("failed-cleanup")],
        )

    def test_success_is_published_after_uid_deletes_and_marker_commit(self) -> None:
        self.prepare_restore()
        events = Mock()
        events.attach_mock(self.api, "api")
        events.attach_mock(self.cursor.execute, "execute")
        events.attach_mock(self.db.__exit__, "db_exit")
        events.attach_mock(self.lease, "lease")
        VALIDATOR.main()
        self.assertEqual(
            [c.args[:2] for c in self.api.call_args_list],
            [
                (SOURCE_STORE,),
                (STORE, "POST"),
                (CLUSTER, "POST"),
                (CLUSTER_PATH, "DELETE"),
                (STORE_PATH, "DELETE"),
            ],
        )
        for post in self.api.call_args_list[1:3]:
            self.assertEqual(
                post.args[2]["metadata"]["annotations"][RUN_ANNOTATION], RUN.hex
            )
        self.assertEqual(
            events.mock_calls[-5:],
            [
                call.api(CLUSTER_PATH, "DELETE", delete_options("cluster-uid"), (404,)),
                call.api(STORE_PATH, "DELETE", delete_options("store-uid"), (404,)),
                call.execute(
                    "DELETE FROM public.platform_backup_validation WHERE run_id IN (%s,%s)",
                    (f"{RUN.hex}-before", f"{RUN.hex}-after"),
                ),
                call.db_exit(None, None, None),
                call.lease("success", restore_target_verified=True, mirror_verified=True),
            ],
        )

    def test_cleanup_error_is_reported_and_other_cleanup_continues(self) -> None:
        self.prepare_restore()
        error = HTTPError(CLUSTER_PATH, 409, "UID precondition failed", {}, BytesIO())
        self.respond(self.source_store, STORE_CREATED, CLUSTER_CREATED, error, {})
        with self.assertRaisesRegex(
            RuntimeError, "validation cleanup failed"
        ) as caught:
            VALIDATOR.main()
        self.assertIs(caught.exception.__cause__.__cause__, error)
        self.assertEqual(
            self.api.call_args_list[-2:],
            [
                call(CLUSTER_PATH, "DELETE", delete_options("cluster-uid"), (404,)),
                call(STORE_PATH, "DELETE", delete_options("store-uid"), (404,)),
            ],
        )
        self.assertTrue(
            self.cursor.execute.call_args.args[0].startswith("DELETE FROM ")
        )
        self.assertEqual(
            self.lease.call_args_list, [call("running"), call("failed-cleanup")]
        )
