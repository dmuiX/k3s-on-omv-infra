import base64
import secrets
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import call, patch

SCRIPT = Path(__file__).parents[1] / "08-backup-validation/resources/postgresql_restore_test.py"
SPEC = spec_from_file_location("restore_validator", SCRIPT)
V = module_from_spec(SPEC)
SPEC.loader.exec_module(V)


def fixture_login(username):
    login = {"username": username}
    login["password"] = secrets.token_urlsafe(24)
    return login


class RestoredAppSchemaTest(unittest.TestCase):
    def test_only_cluster_owned_login_secret(self):
        owner = {"kind": "Cluster", "name": V.RESTORE, "uid": "this", "controller": True}
        login = fixture_login(V.READER)
        item = {"metadata": {"ownerReferences": [owner]}, "data": {
            k: base64.b64encode(v.encode()).decode() for k, v in {**login, "host": "untrusted"}.items()}}
        with patch.object(V, "api", return_value=item) as api:
            self.assertEqual(V.restored_credentials("this"), login)
            api.assert_called_once_with(f"/api/v1/namespaces/backup-validation/secrets/{V.RESTORE}-app")
            with self.assertRaisesRegex(RuntimeError, "do not belong"):
                V.restored_credentials("other")
            item["data"]["username"] = base64.b64encode(b"postgres").decode()
            with self.assertRaisesRegex(RuntimeError, "unexpected restore login"):
                V.restored_credentials("this")

    def test_marker_writer_uses_only_the_source_application_login(self):
        login = fixture_login('app')
        data = {k: base64.b64encode(v.encode()).decode() for k, v in {**login, 'host': 'untrusted'}.items()}
        with patch.object(V, 'api', return_value={'data': data}) as api:
            self.assertEqual(V.source_credentials(), login)
            api.assert_called_once_with(f'/api/v1/namespaces/{V.SOURCE_NS}/secrets/{V.SOURCE}-app')
            data['username'] = base64.b64encode(b'postgres').decode()
            with self.assertRaisesRegex(RuntimeError, 'unexpected source marker login'):
                V.source_credentials()

    def test_recovery_configures_generated_login_and_read_privileges(self):
        spec = V.validation_cluster("2026-11-20T03:30:00+00:00", "20261120T030705")["spec"]
        recovery = spec["bootstrap"]["recovery"]
        self.assertEqual(recovery["owner"], V.READER)
        self.assertEqual(recovery["database"], V.READER)
        self.assertNotIn("secret", recovery)
        self.assertFalse(spec["enableSuperuserAccess"])
        self.assertEqual(spec["managed"]["roles"], [{"name": V.READER, "login": True,
            "inRoles": ["pg_read_all_data"], "passwordSecret": {"name": f"{V.RESTORE}-app"}}])

    def test_boolean_results_and_schema_ownership_are_required(self):
        credentials = fixture_login(V.READER)
        with patch.object(V, "connect") as connect:
            cursor = connect.return_value.cursor.return_value.__enter__.return_value
            tables = [[("resource",), ("data_source",)], [("radar_timeline_events",), ("radar_timeline_seen_resources",)]]
            cursor.fetchall.side_effect = tables
            cursor.fetchone.side_effect = [(True,), (False,)]
            with self.assertRaisesRegex(RuntimeError, "restored radar application data"):
                V.check_restored_app_schemas(credentials)
            self.assertEqual(connect.call_args_list, [call(credentials, db) for db in ("grafana", "radar")])
            cursor.fetchall.side_effect = [[("resource",)]]
            with self.assertRaisesRegex(RuntimeError, "restored grafana application schema"):
                V.check_restored_app_schemas(credentials)
