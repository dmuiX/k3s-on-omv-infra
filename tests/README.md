# Offline SPEC v6.1 contract tests

Run from the Infra repository root:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

The suite uses only Python's standard library. It does not import PyYAML, contact chart repositories, connect to Kubernetes, or claim runtime acceptance.

Tests are split by the numbered areas in SPEC §11.1:

1. `test_01_repository_ownership.py` — exact 21-Application inventory, canonical paths/waves, root scope, and excluded Apps/webhook/Redis ownership.
2. `test_02_reference_integrity.py` — local paths, `$values` targets, Kustomize references, and root selector boundaries.
3. `test_03_secrets_pins_blockers.py` — basic secret/pin checks and complete machine-readable blocker annotations.
4. `test_04_render_schema_contracts.py` — checked-in schemas, Ambient namespace declarations, and fail-closed markers for validation that cannot yet be performed honestly offline.
5. `test_05_argo_contracts.py` — self-heal/no-prune, multi-source ordering, Application health, and provider-vault's exact 19-type `-20/-10/0` sequence.
6. `test_06_ansible_scope.py` — confirms that Ansible and host inventory are not owned by Infra; their substantive tests belong to Bootstrap.
7. `test_07_platform_contracts.py` — static storage, PostgreSQL, monitoring, and backup-boundary checks.
8. `test_08_adapter_contracts.py` — confirms unresolved adapter work blocks deployment and that Infra authors no direct production S3 deletion.

`deployment_gate.py` intentionally exits with status 2 while open blockers exist. While blocked, `infra.yml` also references the deliberately nonexistent `infra-deployment-blocked` AppProject, so applying the root cannot start child reconciliation. Resolving the final blocker requires changing `deploymentAllowed` to `true` and the root project to `default` in the same reviewed change. The unit suite validates both blocked and resolved gate fixtures; it does not treat a blocked deployment as a successful compatibility test.

Full Helm rendering and Kubernetes/CRD schema validation require reviewed pinned chart artifacts and synthetic Live fixtures. Missing artifacts or schemas remain blockers in `deployment-blockers.json`; they must not be skipped or reported as passing. Runtime gates, failover, backup, restore, and cluster health belong to milestone 3.
