"""Explicit repository/integration test selection; no integration-only imports."""

import importlib
import json
from pathlib import Path
import re
import sys
from typing import Any
import unittest

MODULE = re.compile(r"test_[a-z0-9_]+\Z")
METHOD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+\Z")


def load_repository_suite(directory: Path) -> tuple[unittest.TestSuite, list[str]]:
    """Load only declared repository tests and reject omissions/invalid selectors."""
    root = directory.resolve()
    catalog: dict[str, Any] = json.loads((root / "suites.json").read_text())
    if set(catalog) != {"repository", "mixed", "integration"}:
        raise ValueError("Invalid suite catalog keys")
    public = catalog["repository"]
    mixed = catalog["mixed"]
    private = catalog["integration"]
    if (
        not isinstance(public, list)
        or not isinstance(private, list)
        or not isinstance(mixed, dict)
    ):
        raise ValueError("Invalid suite catalog types")
    for name in public + private + list(mixed):
        if not isinstance(name, str) or not MODULE.fullmatch(name):
            raise ValueError("Invalid test module selector")
    if len(set(public)) != len(public) or len(set(private)) != len(private):
        raise ValueError("Duplicate suite selector")
    if (
        set(public) & set(private)
        or set(public) & set(mixed)
        or set(private) & set(mixed)
    ):
        raise ValueError("Suite scopes overlap")
    modules = {p.stem for p in root.glob("test_*.py")}
    declared = set(public) | set(private) | set(mixed)
    if modules - declared:
        raise ValueError(
            "Unclassified test modules: " + ", ".join(sorted(modules - declared))
        )
    if declared - modules:
        raise ValueError("Unknown test module selector")
    for name in modules:
        if not (root / (name + ".py")).resolve().is_relative_to(root):
            raise ValueError("Test module escapes repository")
    selectors = list(public)
    for name, methods in mixed.items():
        if (
            not isinstance(methods, list)
            or not methods
            or len(set(methods)) != len(methods)
        ):
            raise ValueError("Invalid mixed test selectors")
        for method in methods:
            if not isinstance(method, str) or not METHOD.fullmatch(method):
                raise ValueError("Invalid test method selector")
            selectors.append(name + "." + method)
    loader = unittest.TestLoader()
    sys.path.insert(0, str(root))
    try:
        importlib.invalidate_caches()
        suite = loader.loadTestsFromNames(selectors)
    finally:
        sys.path.pop(0)
    if loader.errors:
        raise ValueError(
            "Unable to load a repository test selector; check its module/class/method"
        )
    if suite.countTestCases() == 0:
        raise ValueError("Repository suite is empty")
    deferred = sorted(private) + [name + " (remaining cases)" for name in sorted(mixed)]
    return suite, deferred


def main() -> int:
    """Run the public suite with synthetic inputs, never private workspace paths."""
    directory = Path(__file__).resolve().parent
    fixtures = directory / "fixtures/public-values"
    # Set before imports: legacy public chart checks use the same fixture-only
    # values interface. Integration-only modules are never imported by selection.
    import os

    for key in ("PLATFORM_LIVE_REPO", "PLATFORM_APPS_REPO", "PLATFORM_BOOTSTRAP_REPO"):
        os.environ[key] = str(fixtures)
    os.environ["KUBECONFIG"] = os.devnull
    for key in ("VSO_TEMPLATE_SOURCE", "VSO_CHART_SOURCE", "VSO_CHART_DIR"):
        os.environ.pop(key, None)
    cache = os.environ.get("PLATFORM_CHART_AUDIT")
    if cache:
        vso = str(Path(cache) / "vault-secrets-operator-2.0.0/vault-secrets-operator")
        os.environ["VSO_CHART_SOURCE"] = vso
        os.environ["VSO_CHART_DIR"] = vso
    try:
        suite, deferred = load_repository_suite(directory)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print("Repository CI setup failed: " + str(error), file=sys.stderr)
        return 2
    print(f"Repository tests selected: {suite.countTestCases()}")
    print(f"Integration scopes NOT RUN here: {len(deferred)}")
    for name in deferred:
        print("  integration: " + name)
    if "--list" in sys.argv:
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print(
            "Mandatory repository checks were skipped; install the declared dependencies",
            file=sys.stderr,
        )
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
