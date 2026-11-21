import copy
import json
import secrets
import unittest
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch
from urllib.parse import parse_qs, urlsplit

import yaml

ROOT = Path(__file__).parents[1] / "08-backup-validation/resources"
SPEC = spec_from_file_location("validator", ROOT / "postgresql_restore_test.py")
V = module_from_spec(SPEC)
SPEC.loader.exec_module(V)


class CompletedBackupTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 3, tzinfo=timezone.utc)
        self.stopped = self.now - timedelta(minutes=15)
        self.backup_id = "20261120T020705"
        self.baseline = self.stopped - timedelta(minutes=5)
        self.ledger = {"metadata": {"name": "postgresql-restore-ledger", "namespace": V.VALIDATION_NS,
                                    "resourceVersion": "12", "creationTimestamp": self.baseline.isoformat()},
                       "data": {"20261120T020704": "claimed"}}
        self.enterContext(patch.object(V, "datetime", wraps=datetime)).now.return_value = self.now
        self.api = self.enterContext(patch.object(V, "api"))

    def confirmed_claim(self):
        return {**self.ledger, "metadata": {**self.ledger["metadata"], "resourceVersion": "15"},
                "data": {**self.ledger["data"], self.backup_id: "unverified"}}

    def backup(self, backup_id=None, stopped=None, phase="completed"):
        backup_id = backup_id or self.backup_id
        return {"metadata": {"name": f"manual-{backup_id.lower()}", "uid": f"uid-{backup_id}",
                             "creationTimestamp": "2026-11-20T02:07:00Z"},
                "spec": {"cluster": {"name": V.SOURCE}, "method": "plugin", "pluginConfiguration": {"name": V.PLUGIN}},
                "status": {"phase": phase, "method": "plugin", "backupId": backup_id,
                           "startedAt": "2026-11-20T02:07:05Z", "stoppedAt": (stopped or self.stopped).isoformat()}}

    def test_list_pages_before_choosing_oldest_unclaimed_completion(self):
        later = self.backup("20261120T021001", self.stopped + timedelta(minutes=1))
        first_page = {"items": [later], "metadata": {"continue": "page+/="}}
        self.api.side_effect = [first_page, {"items": [self.backup()], "metadata": {}}]
        self.assertEqual(V.next_backup(self.ledger), (self.backup_id, self.stopped))
        first, second = self.api.call_args_list
        self.assertEqual(first, call(f"{V.SOURCE_API}/backups?limit=200"))
        second_url = urlsplit(second.args[0])
        self.assertEqual(second_url.path, f"{V.SOURCE_API}/backups")
        self.assertEqual(parse_qs(second_url.query), {"limit": ["200"], "continue": ["page+/="]})

    def test_no_work_for_old_claimed_unfinished_or_foreign_backups(self):
        foreign = self.backup()
        foreign['spec']['cluster']['name'] = 'another-cluster'
        for item in (self.backup(stopped=self.baseline), self.backup(stopped=self.baseline - timedelta(seconds=1)),
                     self.backup(next(iter(self.ledger['data']))), self.backup(phase='pending'),
                     self.backup(phase='failed'), foreign):
            with self.subTest(item=item):
                self.api.return_value = {"metadata": {}, "items": [item]}
                self.assertIsNone(V.next_backup(self.ledger))

    def test_historical_completion_needs_only_a_known_stop_time(self):
        historical = self.backup()
        historical['status'] = {'phase': 'completed', 'stoppedAt': self.baseline.isoformat()}
        historical['metadata'] = {}
        self.api.return_value = {'metadata': {}, 'items': [historical, self.backup()]}
        self.assertEqual(V.next_backup(self.ledger), (self.backup_id, self.stopped))

    def test_malformed_completed_backup_fails_closed(self):
        for field, value in (("backupId", ""), ("backupId", "not-native"), ("stoppedAt", "not a timestamp"),
                             ("stoppedAt", (self.now + timedelta(minutes=1)).isoformat())):
            with self.subTest(field=field, value=value):
                malformed = self.backup("20261120T021001")
                malformed["status"][field] = value
                self.api.return_value = {"metadata": {}, "items": [self.backup(), malformed]}
                with self.assertRaises((RuntimeError, ValueError)):
                    V.next_backup(self.ledger)

    def test_claim_precedes_restore_and_success_reports_actual_mirror_state(self):
        for mirrored in (False, True):
            with (self.subTest(mirrored=mirrored), patch.object(V, "next_backup", return_value=(self.backup_id, self.stopped)),
                  patch.object(V, "put_lease") as lease, patch.object(V, "validate") as validate,
                  patch.object(V, "mirror_matches_catalog", return_value=mirrored) as mirror):
                order = Mock()
                for name, mock in (("api", self.api), ("lease", lease), ("validate", validate), ("mirror", mirror)):
                    order.attach_mock(mock, name)
                self.api.side_effect = [copy.deepcopy(self.ledger), None, None, self.confirmed_claim(), {}]
                V.main()
                read, running, store, cluster, claim, *after = order.mock_calls
                self.assertEqual(running, call.lease("running", self.backup_id))
                self.assertEqual(store, call.api(f"{V.STORE_PATH}/{V.STORE}", allowed=(404,)))
                self.assertEqual(cluster, call.api(f"{V.CLUSTER_PATH}/{V.RESTORE}", allowed=(404,)))
                path = f"/api/v1/namespaces/{V.VALIDATION_NS}/configmaps/postgresql-restore-ledger"
                self.assertEqual(read, call.api(path))
                self.assertEqual(claim.args[:2], (path, "PUT"))
                self.assertEqual(claim.args[2]["metadata"]["resourceVersion"], "12")
                self.assertEqual(claim.args[2]["data"], {**self.ledger["data"], self.backup_id: "unverified"})
                success = self.confirmed_claim()
                success['data'][self.backup_id] = 'success'
                self.assertEqual(after, [call.validate(self.backup_id, self.stopped), call.api(path, 'PUT', success),
                                         call.mirror(self.backup_id),
                                         call.lease("success", self.backup_id, True, mirrored, owned_version=lease.return_value)])

    def test_claim_conflict_reports_failure_without_validation(self):
        self.api.side_effect = [copy.deepcopy(self.ledger), None, None, RuntimeError("conflict")]
        with (patch.object(V, "next_backup", return_value=(self.backup_id, self.stopped)),
              patch.object(V, "put_lease") as lease, patch.object(V, "validate") as validate):
            with self.assertRaisesRegex(RuntimeError, "conflict"):
                V.main()
            self.assertEqual(lease.call_args_list, [call("running", self.backup_id),
                             call("failed", self.backup_id, owned_version=lease.return_value)])
            validate.assert_not_called()

    def test_failure_leaves_permanent_tombstone(self):
        self.api.side_effect = [copy.deepcopy(self.ledger), None, None, self.confirmed_claim(), {}]
        with (patch.object(V, "next_backup", return_value=(self.backup_id, self.stopped)),
              patch.object(V, "put_lease") as lease, patch.object(V, "validate", side_effect=RuntimeError("restore failed"))):
            with self.assertRaisesRegex(RuntimeError, "restore failed"):
                V.main()
        self.assertEqual(self.api.call_count, 5)
        self.assertEqual(self.api.call_args.args[2]["metadata"]["resourceVersion"], "15")
        self.assertEqual(self.api.call_args.args[2]["data"][self.backup_id], "failed")
        self.assertEqual(lease.call_args_list, [call("running", self.backup_id),
                         call("failed", self.backup_id, owned_version=lease.return_value)])

    def test_failed_a_then_successful_b_preserves_exact_results(self):
        other_id = '20261120T021001'
        page = {'metadata': {}, 'items': [self.backup(), self.backup(other_id, self.stopped + timedelta(minutes=1))]}
        durable = copy.deepcopy(self.ledger)

        def cluster_api(path, method='GET', body=None, **kwargs):
            nonlocal durable
            if path == V.LEDGER_PATH:
                if method == 'PUT':
                    version = durable['metadata']['resourceVersion']
                    self.assertEqual(body['metadata']['resourceVersion'], version)
                    durable = copy.deepcopy(body)
                    durable['metadata']['resourceVersion'] = str(int(version) + 1)
                return copy.deepcopy(durable)
            return page if path.startswith(V.SOURCE_API) else None

        self.api.side_effect = cluster_api
        with (patch.object(V, 'put_lease'), patch.object(V, 'validate', side_effect=[RuntimeError('A failed'), None]) as validate,
              patch.object(V, 'mirror_matches_catalog', return_value=True)):
            with self.assertRaisesRegex(RuntimeError, 'A failed'):
                V.main()
            V.main()
            self.assertEqual([c.args[0] for c in validate.call_args_list], [self.backup_id, other_id])
            self.assertIsNone(V.next_backup(durable))
        self.assertEqual(durable['data'], {**self.ledger['data'], self.backup_id: 'failed', other_id: 'success'})

    def test_result_cas_conflict_does_not_publish_success(self):
        self.api.side_effect = [self.ledger, None, None, self.confirmed_claim(), RuntimeError('conflict')]
        with (patch.object(V, 'next_backup', return_value=(self.backup_id, self.stopped)),
              patch.object(V, 'put_lease') as lease, patch.object(V, 'validate')):
            with self.assertRaisesRegex(RuntimeError, 'conflict'):
                V.main()
        self.assertEqual(lease.call_args.args[:2], ('failed', self.backup_id))

    def test_losing_lease_race_neither_claims_nor_publishes_failure(self):
        page = {"metadata": {}, "items": [self.backup()]}
        idle = {"metadata": {"resourceVersion": "10", "annotations": {}}}
        for prior, method in ((None, "POST"), (idle, "PUT")):
            with self.subTest(method=method), patch.object(V, "validate") as validate:
                self.api.reset_mock()
                self.api.side_effect = [self.ledger, page, prior, RuntimeError("lease conflict")]
                with self.assertRaisesRegex(RuntimeError, "lease conflict"):
                    V.main()
                writes = [c.args for c in self.api.call_args_list if len(c.args) > 1]
                self.assertEqual(len(writes), 1)
                path, actual_method, body = writes[0]
                self.assertEqual(path, V.LEASE_PATH if prior else V.LEASE_PATH.rsplit("/", 1)[0])
                self.assertEqual(actual_method, method)
                self.assertEqual(body["metadata"]["annotations"]["k3s-on-omv.io/result"], "running")
                self.assertNotIn(self.backup_id, self.ledger["data"])
                validate.assert_not_called()

    def test_orphan_or_deleting_resource_blocks_claim(self):
        orphan = {"metadata": {"uid": "old"}}
        deleting = {"metadata": {"deletionTimestamp": self.now.isoformat()}}
        for checks in ([orphan], [None, deleting]):
            with (self.subTest(checks=checks), patch.object(V, "put_lease", return_value="13") as lease,
                  patch.object(V, "validate") as validate):
                self.api.reset_mock()
                self.api.side_effect = [self.ledger, {"metadata": {}, "items": [self.backup()]}, *checks]
                with self.assertRaisesRegex(RuntimeError, "resources still exist"):
                    V.main()
                self.assertNotIn(self.backup_id, self.ledger['data'])
                self.assertTrue(all(len(c.args) == 1 for c in self.api.call_args_list))
                self.assertEqual(lease.call_args_list, [call('running', self.backup_id),
                                                       call('failed', self.backup_id, owned_version='13')])
                validate.assert_not_called()

    def test_interrupted_before_claim_retries_only_after_running_lease_timeout(self):
        page = {"metadata": {}, "items": [self.backup()]}
        updated = {"metadata": {"resourceVersion": "14"}}
        self.api.side_effect = [copy.deepcopy(self.ledger), page, None, updated, KeyboardInterrupt()]
        with (patch.object(V, "validate") as validate, patch.object(V, "mirror_matches_catalog", return_value=True)):
            with self.assertRaises(KeyboardInterrupt):
                V.main()
            running = copy.deepcopy(self.api.call_args_list[-2].args[2])
            running['metadata'].update(updated['metadata'])
            for elapsed in (1, 5400):
                V.datetime.now.return_value = self.now + timedelta(seconds=elapsed)
                self.api.reset_mock()
                self.api.side_effect = [copy.deepcopy(self.ledger), page, running]
                with self.assertRaisesRegex(RuntimeError, 'another restore'):
                    V.main()
                self.assertTrue(all(len(c.args) == 1 for c in self.api.call_args_list))
            validate.assert_not_called()
            V.datetime.now.return_value = self.now + timedelta(seconds=5401)
            self.api.side_effect = [copy.deepcopy(self.ledger), page, running, updated, None, None, self.confirmed_claim(), {}, updated]
            V.main()
            validate.assert_called_once_with(self.backup_id, self.stopped)

    def test_interrupted_after_claim_never_replays_or_burns_next_backup(self):
        next_id = '20261120T021001'
        page = {'metadata': {}, 'items': [self.backup(), self.backup(next_id, self.stopped + timedelta(minutes=1))]}
        self.api.side_effect = [copy.deepcopy(self.ledger), page, None, {'metadata': {'resourceVersion': '14'}}, None, None, {}]
        with patch.object(V, 'validate', side_effect=KeyboardInterrupt()) as validate:
            with self.assertRaises(KeyboardInterrupt):
                V.main()
            running = self.api.call_args_list[3].args[2]
            durable = self.api.call_args.args[2]
            self.assertEqual(durable['data'][self.backup_id], 'unverified')
            self.api.reset_mock()
            self.api.side_effect = [copy.deepcopy(durable), page, running]
            with self.assertRaisesRegex(RuntimeError, 'another restore'):
                V.main()
            self.assertTrue(all(len(c.args) == 1 for c in self.api.call_args_list))
            validate.assert_called_once_with(self.backup_id, self.stopped)
            self.api.side_effect = [page]
            self.assertEqual(V.next_backup(durable)[0], next_id)

    def test_success_lease_preserves_retention_annotations(self):
        lease = {"metadata": {"resourceVersion": "12", "annotations": {}}}
        self.api.side_effect = [lease, {"metadata": {"resourceVersion": "13"}}, {"metadata": {"resourceVersion": "14"}}]
        version = V.put_lease("running", self.backup_id)
        path, method, running = self.api.call_args.args
        self.assertTrue(path.endswith("/leases/postgresql-restore-status"))
        self.assertEqual(method, "PUT")
        self.assertEqual(running["metadata"]["resourceVersion"], "12")
        V.put_lease("success", self.backup_id, True, True, owned_version=version)
        self.assertEqual(self.api.call_args.args[2]["metadata"]["resourceVersion"], "13")
        annotations = self.api.call_args.args[2]["metadata"]["annotations"]
        for key, value in (("result", "success"), ("restored-backup-id", self.backup_id),
                           ("pitr-verified", "true"), ("omv-mirror-verified", "true"),
                           ("restore-evidence", "post-backup-pitr-v1")):
            self.assertEqual(annotations[f"k3s-on-omv.io/{key}"], value)

    def test_idle_poll_only_refreshes_mirror(self):
        self.api.return_value = self.ledger
        with (patch.object(V, "next_backup", return_value=None), patch.object(V, "refresh_mirror") as refresh,
              patch.object(V, "put_lease") as lease, patch.object(V, "validate") as validate):
            V.main()
            refresh.assert_called_once_with()
            validate.assert_not_called()
            lease.assert_not_called()
        self.assertEqual(self.api.call_count, 1)

    def test_mirror_refresh_only_promotes_prior_success_without_renewing_it(self):
        for result, verified, matched, writes in (("failed", "false", True, False), ("running", "false", True, False),
                                                  ("success", "true", True, False), ("success", "false", False, False),
                                                  ("success", "false", True, True)):
            with self.subTest(result=result, verified=verified, matched=matched):
                lease = {"metadata": {"resourceVersion": "9", "annotations": {
                    "k3s-on-omv.io/restore-evidence": "post-backup-pitr-v1",
                    "k3s-on-omv.io/result": result, "k3s-on-omv.io/pitr-verified": "true",
                    "k3s-on-omv.io/omv-mirror-verified": verified, "k3s-on-omv.io/restored-backup-id": self.backup_id,
                }}, "spec": {"renewTime": self.baseline.isoformat()}}
                self.api.reset_mock()
                self.api.side_effect = [copy.deepcopy(lease), {}]
                with patch.object(V, "mirror_matches_catalog", return_value=matched) as mirror:
                    V.refresh_mirror()
                    if result == "success" and verified == "false":
                        mirror.assert_called_once_with(self.backup_id)
                self.assertEqual(self.api.call_count, 2 if writes else 1)
                if writes:
                    path, method, body = self.api.call_args.args
                    self.assertTrue(path.endswith("/leases/postgresql-restore-status"))
                    self.assertEqual(method, "PUT")
                    lease["metadata"]["annotations"]["k3s-on-omv.io/omv-mirror-verified"] = "true"
                    self.assertEqual(body, lease)

    def test_mirror_refresh_ignores_legacy_pitr_success(self):
        self.api.return_value = {"metadata": {"resourceVersion": "9", "annotations": {
            "k3s-on-omv.io/result": "success", "k3s-on-omv.io/pitr-verified": "true",
            "k3s-on-omv.io/omv-mirror-verified": "false", "k3s-on-omv.io/restored-backup-id": self.backup_id,
        }}}
        with patch.object(V, "mirror_matches_catalog") as mirror:
            V.refresh_mirror()
            mirror.assert_not_called()
        self.api.assert_called_once_with(V.LEASE_PATH, allowed=(404,))

    def test_catalog_requires_exact_done_id_and_mirror_covers_all_done_ids(self):
        selected = {"backup_id": self.backup_id, "status": "DONE"}
        other = {"backup_id": "20261120T030705", "status": "DONE"}
        env = {"S3_ENDPOINT": "https://example.invalid", "S3_BUCKET": "fixture", "BARMAN_SERVER_NAME": V.SOURCE}
        with patch.dict(V.os.environ, env), patch.object(V.subprocess, "run") as run, patch.object(V, "Path") as path:
            for entries in ([other], [selected, selected], [{**selected, "status": "FAILED"}]):
                run.return_value = SimpleNamespace(stdout=json.dumps({"backups_list": entries}))
                with self.assertRaisesRegex(RuntimeError, "not uniquely completed"):
                    V.catalog_ids(self.backup_id)
            run.return_value = SimpleNamespace(stdout=json.dumps({"backups_list": [selected, other]}))
            path.return_value.glob.return_value = [Path(f"/omv/{V.SOURCE}/base/{self.backup_id}/backup.info")]
            self.assertFalse(V.mirror_matches_catalog(self.backup_id))
            path.return_value.glob.return_value += [Path(f"/omv/{V.SOURCE}/base/{other['backup_id']}/backup.info")]
            self.assertTrue(V.mirror_matches_catalog(self.backup_id))

    def test_ambiguous_create_only_records_our_object_for_cleanup(self):
        path = "/api/resources"
        for annotation, expected in (("our-run", [(f"{path}/example", "uid")]), ("foreign-run", [])):
            with self.subTest(annotation=annotation):
                self.api.side_effect = [TimeoutError(), {"metadata": {
                    "uid": "uid", "annotations": {V.RUN_ANNOTATION: annotation},
                }}]
                created = []
                with self.assertRaises(TimeoutError):
                    V.create_validation_resource(path, {"metadata": {"name": "example"}}, "our-run", created)
                self.assertEqual(created, expected)

    def test_readiness_requires_ready_condition_and_reconciled_reader(self):
        def cluster(ready, reconciled):
            return {"metadata": {"uid": "restore-uid"}, "status": {
                "conditions": [{"type": "Ready", "status": ready}],
                "managedRolesStatus": {"byStatus": {"reconciled": reconciled}},
            }}

        self.api.side_effect = [cluster("True", ["other"]), cluster("False", [V.READER]),
                                cluster("True", [V.READER])]
        with patch.object(V.time, "sleep"):
            V.wait_ready("restore-uid")
        self.assertEqual(self.api.call_args_list, [call(f"{V.CLUSTER_PATH}/{V.RESTORE}")] * 3)

    def test_readiness_rejects_replaced_or_deleting_cluster(self):
        for metadata in ({"uid": "other"}, {"uid": "restore-uid", "deletionTimestamp": self.now.isoformat()}):
            with self.subTest(metadata=metadata), patch.object(V.time, "sleep") as sleep:
                self.api.side_effect = [{"metadata": metadata}]
                with self.assertRaisesRegex(RuntimeError, "cluster changed"):
                    V.wait_ready("restore-uid")
                sleep.assert_not_called()

    def exercise_restore(self, rows, app_error=None):
        store = yaml.safe_load((ROOT.parents[1] / "07-k3s-postgres/resources/objectstore.yaml").read_text())
        env = {"S3_ENDPOINT": "https://example.invalid", "S3_BUCKET": "cloudnativepg-backups",
               "BARMAN_SERVER_NAME": V.SOURCE}
        self.api.reset_mock()
        self.api.side_effect = [store, {'metadata': {'uid': 'store-uid'}},
                                {'metadata': {'uid': 'restore-uid'}}, {}, {}]
        source, restored = MagicMock(), MagicMock()
        producer_sql = source.cursor.return_value.__enter__.return_value
        producer_sql.fetchone.side_effect = [(self.stopped + timedelta(seconds=1),), (self.now,)]
        restored_sql = restored.cursor.return_value.__enter__.return_value
        restored_sql.fetchall.return_value = rows
        credentials = {'username': V.READER}
        credentials['password'] = secrets.token_urlsafe(24)
        with (patch.dict(V.os.environ, env), patch.object(V, 'source_credentials') as producer,
              patch.object(V, 'connect_source', return_value=source) as connect_source,
              patch.object(V, 'connect', return_value=restored) as connect,
              patch.object(V.uuid, 'uuid4', return_value=SimpleNamespace(hex='fixture')),
              patch.object(V.time, 'sleep'), patch.object(V, 'catalog_ids') as catalog,
              patch.object(V, 'restored_credentials', return_value=credentials) as login,
              patch.object(V, 'wait_ready') as ready,
              patch.object(V, 'check_restored_app_schemas', side_effect=app_error) as check):
            boundary_ok = rows == [('fixture-before', 'before')]
            if app_error or not boundary_ok:
                with self.assertRaisesRegex(RuntimeError, 'application data' if app_error else 'boundary'):
                    V.validate(self.backup_id, self.stopped)
            else:
                V.validate(self.backup_id, self.stopped)
        catalog.assert_called_once_with(self.backup_id)
        ready.assert_called_once_with('restore-uid')
        login.assert_called_once_with('restore-uid')
        self.assertEqual(connect_source.call_args_list, [call(producer.return_value)] * 4)
        connect.assert_called_once_with(credentials, 'app')
        self.assertEqual(restored_sql.execute.call_args.args[1], ('fixture-before', 'fixture-after'))
        target = self.api.call_args_list[2].args[2]['spec']['bootstrap']['recovery']['recoveryTarget']
        self.assertEqual(target, {'backupID': self.backup_id, 'targetTime': self.now.isoformat()})
        for deleted, uid in zip(self.api.call_args_list[3:], ('restore-uid', 'store-uid'), strict=True):
            self.assertEqual(deleted.args[1:], ('DELETE', {'preconditions': {'uid': uid}}, (404,)))
        statements = [c.args for c in producer_sql.execute.call_args_list]
        self.assertEqual([args[0].split()[0] for args in statements],
                         ['SELECT', 'CREATE', 'INSERT', 'SELECT', 'INSERT', 'DELETE'])
        self.assertEqual([args[1] for args in statements if len(args) > 1],
                         [('fixture-before',), ('fixture-after',), ('fixture-before', 'fixture-after')])
        if boundary_ok:
            check.assert_called_once_with(credentials)
        else:
            check.assert_not_called()

    def test_pinned_restore_requires_boundary_and_application_records(self):
        self.exercise_restore([('fixture-before', 'before')])
        self.exercise_restore([('fixture-before', 'before')], RuntimeError('application data check failed'))

    def test_missing_before_or_visible_after_marker_fails_boundary(self):
        for rows in ([], [('fixture-before', 'before'), ('fixture-after', 'after')]):
            with self.subTest(rows=rows):
                self.exercise_restore(rows)
