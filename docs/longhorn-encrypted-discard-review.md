# Encrypted discard PR: implementation and differential review

Date: 16 November 2026. Baseline: Infra `cfcfcaa35c989043c5622ccda3df1b1d49391977`, fetched from GitHub main before final checks. Review scope is this feature, not the entire platform. No background AI workers or independent reviewer were used.

## Executive summary

**Risk: HIGH (privileged node administration). Recommendation: DRAFT; do not treat these checks as production release approval.**

The agreed three parts are implemented: persistent flags for existing encrypted Longhorn v1 volumes, the same policy on future attachments, and Longhorn's native filesystem-trim RecurringJob. No StorageClass, PVC, encryption format, volume size, image pin of Longhorn itself, or bootstrap key workflow is changed. Five namespaced resources are introduced under the existing `longhorn-config` Application source; the existing Bootstrap prerequisites already create the required Secret before GitOps installation.

## Changes and blast radius

- `06-platform/longhorn-config/resources/encrypted-discard.yaml`: five resources, containing the host helper and daily trim job. All eligible Linux storage nodes, all attached v1/LUKS2 Longhorn volumes with the existing Bootstrap key; new attachments are checked every 60 seconds. Native trim applies to the default recurring-job group (all 13 inspected existing volumes are members), without changing group membership or detached-volume attachment policy.
- `tests/test_longhorn_discard.py`: 30 focused tests execute the actual ConfigMap code, not a separate implementation.
- `tests/verify-contracts.rb`: changes the blanket application hardening assertion into an explicitly named exception only for DaemonSet `longhorn/longhorn-encrypted-discard`, container `reconcile`. All other containers retain the existing assertions. This is a deliberate privilege-policy change, not a cosmetic test adjustment.
- `tests/README.md`: documents the new test scope.
- `docs/longhorn-encrypted-discard.md`: exact live plan, bootstrap ownership, key handling, snapshot limits and rollback.
- `docs/postgresql-wal-recovery.md`: links the separate proposed default without rewriting the incident as already resolved.
- This report records results and remaining gates.

Relevant baseline owners and prerequisites were read: `06-platform/longhorn-config/app.yml`, existing Longhorn StorageClasses, Bootstrap `gitops_bootstrap/tasks/main.yml` and `longhorn-key.yml`. Available git history is shallow (boundary `62c31a6`), so exhaustive security-history regression analysis is not claimed. The source directory, namespace privilege model and shared key already exist. No audit-context-building tool was available; dependencies and invariants were traced manually.

## Findings addressed during implementation

1. **Resource budget could exclude or kill real-volume activation.** The first real read-only audit found two KDF requirements larger than the initial budget (one 1GiB, another ~861MiB). The limit is now 2GiB, KDF demand must be <=1536MiB, and host MemAvailable must cover that demand plus 256MiB before authentication. Tests cover large valid KDFs, excessive KDFs, low memory and already-configured volumes not needing authentication.
2. **Persistent refresh must not overwrite unrelated activation flags.** The helper refuses a needed refresh if unexpected active or persistent flags are present. Tests cover both cases, with no authenticated operation issued. Unsupported mappings require a separate policy decision rather than silently losing flags.
3. **ConfigMap updates alone would not restart an existing process.** The pod template contains the source SHA-256; the test verifies it against the actual ConfigMap program. Health was simplified to a real read-only readiness audit rather than an extra state-file/heartbeat subsystem.

These are addressed implementation findings, not claims that the host mutation path has been production-tested.

## Threat model and adversarial checks

- A workload user can indirectly cause PVC attachment but cannot normally write host `/dev/longhorn` or kernel iSCSI sessions. Names are strictly validated and passed as argv, never shell text. Block major/minor identity and the exact kernel target `iqn.2019-10.io.longhorn:<name>` must match before acting. Tests reject path/option/shell injection, foreign iSCSI targets, wrong backing devices, non-block and non-iSCSI sources.
- An actor able to change this privileged DaemonSet or its ConfigMap can gain node-root access and use the shared volume key. This is an explicit HIGH-risk trusted-administrator boundary, not prevented by the pod NetworkPolicy. There are no API tokens, API RBAC additions, hostPath mounts or hostNetwork requests, but privileged hostPID access is still effectively host-root authority.
- The projected key is consumed only through a file descriptor as cryptsetup stdin. No production key was retrieved, decoded, copied or printed. Raw command stderr/stdout and LUKS metadata never enter logs. Failure/timeout tests use synthetic sentinels and verify output suppression. The read-only audit ran with no key mount.
- A pre-operation identity check and local flock reduce accidental overlap. They are not an atomic lock shared with CSI; no protection against an already malicious host-root administrator is claimed. cryptsetup errors or a changed/missing device fail readiness and retry.
- The process does not format devices, delete snapshots, reset WAL, change image/StorageClass/PVC configuration, or deliberately change host-system disks. Discard can nevertheless make already-free block history unrecoverable and reveal allocation patterns; that is the requested policy effect.

## Initial executed evidence (first PR head `4e2856b`)

- TDD baseline: all 13 initial feature tests failed before the resource existed; subsequent implementation/resource-boundary findings gained focused tests. Final focused suite: **30 passed**.
- Runtime source coverage: **98% statement coverage (128 statements, 3 uncovered)**. The coverage harness maps the test imports to the same stable extraction of the checksum-verified ConfigMap source; host operations remain simulated. This is not kernel/CSI integration coverage.
- **mypy --strict, black --check and ruff check pass** for the extracted program and committed Python tests. `git diff --check` passes.
- Exact image `python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641` was inspected as a multiarchitecture index (amd64/arm64); Python 3.13 and nsenter availability were checked in that image.
- The exact checksum-verified entrypoint was executed from that image on OMV in **audit-only** mode, with no key mount. It identified all five attached Longhorn LUKS mappings and returned the expected exit 1 / `Discard not enabled` messages. The container was removed; the sampled live mapping still had no discard flag. No feature DaemonSet exists live.
- Kubernetes **server dry-run accepted all five resources**. It reported expected restricted-PodSecurity warnings for privileged root/hostPID; Longhorn's namespace allows privileged storage administration. Dry-run is not deployment.
- Full Python suite: **140 tests, 18 failures, 4 errors, 11 skips**, exactly the same named failures/errors as the unchanged pre-feature baseline (110 tests). No new test failure.
- Full Ruby contracts: **33 tests, 1853 assertions, 3 failures, 5 errors**, same named pre-existing failures/errors after normalizing source line shifts. The focused helper/manifest checks pass, but the repository-wide suite is **not green**. No unrelated contract was weakened to hide baseline failures.

## Open gates and limitations

**Blocking before production acceptance:** real authenticated refresh on reviewed target nodes; live and persistent flag verification; reattach/reboot persistence; native recurring-job execution; actual physical reclamation; continued database streaming and unchanged host/system mappings. None has been inferred from mocks, image inspection, audit success or a server dry-run. The PR remains a draft until the activation path is explicitly approved and accepted.

No real mutating stage test was run because the user requested a PR rather than immediate activation. No key was mounted into the test container. A new synthetic CSI/iSCSI cluster, architecture-specific mutating test, complete Bootstrap/Ansible run, general backup/restore test or adversarial host-root test was not performed. Existing full-suite failures remain deployment-review items. Removing the workload does not reset persistent flags, and Git revert alone does not delete live resources because this Application has prune disabled.

Code-reviewer, differential-review and Python-pro checks were applied to the changed scope. SQL performance/index analysis is not applicable: this feature contains no database mutation or SQL implementation. Remaining release gates above prevent a `NO_FINDINGS` or unconditional release-clearance claim.

## Follow-up: second-review P2 process containment (16 November 2026)

The [second PR review](https://github.com/dmuiX/k3s-on-omv-infra/pull/79#pullrequestreview-5922030394) reproduced a P2: `subprocess.run` timed out its direct `flock` child but left the descendant running and holding its lock. This follow-up changes only command lifecycle handling, regression tests, source checksum and documentation; no additional Kubernetes resources, StorageClasses, permissions or deployment steps are added.

The host wrapper now starts each operation in its own session/group, checks timeout or SIGTERM/SIGINT cancellation every 250ms, then sends SIGTERM and bounded SIGKILL to that owned group before reaping. It does not poll/reap the leader before both signals, preventing PID/PGID reuse; an already-reaped leader is not signalled. Signal/reap failure stops the helper rather than allowing an unsafe retry. Cancellation handlers also cover one-off/audit entrypoints. The direct child, streams and key-file descriptor are cleaned up on the failure path.

`tests/test_longhorn_discard_processes.py` tests the actual ConfigMap code with real flock/child processes and a temporary exec-through nsenter shim. The identical timeout regression fails against old head `4e2856b` (surviving descendant) and passes against this fix. It checks no delayed action, released flock, completed descendant and an unrelated process left running. Another test sends real SIGTERM through the actual main entrypoint; the synthetic descendant ignores SIGTERM to exercise SIGKILL escalation. Additional fault tests cover unconfirmed cleanup, signal errors and an already-reaped leader. A subreaper fixture reaps synthetic descendants even when the baseline test intentionally fails.

The five lifecycle tests also pass inside the exact pinned image as the unprivileged checkout owner, with all capabilities dropped, no network, no host PID namespace, no key/device mount, read-only root and a small executable temporary filesystem for the synthetic shim. The disposable test container is removed afterward. This is real process/entrypoint evidence, **not** a real encrypted-volume/CSI activation test.

| Repeat review | Result / boundary |
|---|---|
| Code-reviewer | Timeout/stop paths and process ownership rechecked; the reproduced P2 is addressed by the real-process regression. |
| Differential-review | Fix delta reviewed against `4e2856b`; group signalling is restricted to the newly created, unreaped operation leader. No host-wide group signal, new key exposure, RBAC or storage-policy change. |
| Python-pro | **35 focused tests pass; 96% runtime statement coverage (171 statements, 6 uncovered); mypy strict, black and ruff pass** on the runtime extraction and both test files. |
| Kubernetes-specialist | All five manifests pass fresh server dry-run; source checksum updated. Command deadline + 1s TERM grace + 5s reap budget fit within the unchanged 150s pod termination grace. Expected privileged-storage PodSecurity warnings remain; rollout and real TRIM are still untested. |

The full Python suite now runs **145 tests with the same 18 failures, 4 errors and 11 skips** as the pre-feature baseline; Ruby runs **33 tests, 1857 assertions, 3 failures and 5 errors**, again the same named baseline failures/errors. The full platform suite is not green. No independent/background reviewer was started. Reviews and tests do not close the real-key, mapping persistence, physical reclamation or unchanged-host-state gates above. **Keep the PR draft; no merge or live rollout has been performed.**
