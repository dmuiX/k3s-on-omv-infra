"""Public workload interfaces: repository confinement and rendered contracts."""

import copy
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import yaml

Document = dict[str, Any]


class KubernetesLoader(yaml.CSafeLoader):
    """Safe YAML with Kubernetes' literal '=' operator enum compatibility."""


def _value_scalar(loader: KubernetesLoader, node: yaml.nodes.Node) -> str:
    if not isinstance(node, yaml.nodes.ScalarNode):
        raise ValueError("Operator enum must be scalar")
    return str(loader.construct_scalar(node))


KubernetesLoader.add_constructor("tag:yaml.org,2002:value", _value_scalar)


def parse_documents(text: str) -> list[Document]:
    """Read Kubernetes documents without enabling unsafe YAML constructors."""
    return [
        d
        for d in yaml.load_all(text, Loader=KubernetesLoader)
        if isinstance(d, dict) and d.get("kind")
    ]


OWN_REPO = "https://github.com/dmuiX/k3s-on-omv-infra.git"
NAMESPACED = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Service",
    "ServiceAccount",
    "ConfigMap",
    "Secret",
    "Role",
    "RoleBinding",
    "NetworkPolicy",
    "PodDisruptionBudget",
    "Job",
    "CronJob",
    "Pod",
    "HTTPRoute",
    "Gateway",
    "ServiceMonitor",
    "PodMonitor",
    "Prometheus",
    "PrometheusRule",
    "Alertmanager",
}


def merge(base: Document, override: Document) -> Document:
    """Apply Helm value precedence without mutating either public input."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else copy.deepcopy(value)
        )
    return result


def _render_documents(
    argv: list[str], values: Document | None = None
) -> list[Document]:
    result = subprocess.run(
        argv,
        input=yaml.safe_dump(values) if values is not None else None,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "KUBECONFIG": os.devnull},
    )
    if result.returncode:
        raise ValueError(
            "Public render failed; tool output withheld (may contain generated Secret data)"
        )
    try:
        documents = parse_documents(result.stdout)
    except yaml.YAMLError:
        raise ValueError(
            "Public render YAML is invalid; generated contents withheld"
        ) from None
    for document in documents:
        if document["kind"] == "Secret":
            # Only identity is needed to test that VSO, not Helm, owns a Secret.
            document.pop("data", None)
            document.pop("stringData", None)
    return documents


@dataclass
class Repository:
    """Render real public Application sources with synthetic site interfaces."""

    root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])
    apps: dict[str, Document] = field(init=False)
    _cache: dict[tuple[str, bool], list[Document]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.apps = {}
        for path in self.root.rglob("app.yml"):
            app = yaml.safe_load(
                public_path(self.root, str(path.relative_to(self.root))).read_text()
            )
            name = app["metadata"]["name"]
            if name in self.apps:
                raise ValueError("Duplicate Application identity")
            self.apps[name] = app

    def values(self, source: Document) -> Document:
        """Read only synthetic $live inputs, then canonical inline overrides."""
        helm = source.get("helm", {})
        if set(helm) - {"releaseName", "valueFiles", "valuesObject", "skipCrds"}:
            raise ValueError(
                "Unsupported Helm input; add explicit public-interface support"
            )
        values: Document = {}
        fixtures = self.root / "tests/fixtures/public-values"
        for name in helm.get("valueFiles", []):
            if not name.startswith("$live/"):
                raise ValueError("Unknown external values interface")
            file = public_path(fixtures, name.removeprefix("$live/"))
            values = merge(values, yaml.safe_load(file.read_text()))
        return merge(values, helm.get("valuesObject", {}))

    def render(self, name: str, *, capabilities: bool = True) -> list[Document]:
        """Use actual Helm/Kustomize engines, never Kubernetes or private files."""
        key = (name, capabilities)
        if key in self._cache:
            return copy.deepcopy(self._cache[key])
        app = self.apps[name]
        namespace = app["spec"]["destination"]["namespace"]
        documents: list[Document] = []
        for source in app["spec"].get("sources", [app["spec"].get("source", {})]):
            remote_chart = "chart" in source
            if remote_chart:
                cache = os.environ.get("PLATFORM_CHART_AUDIT")
                if not cache:
                    raise ValueError("Prepare the mandatory chart cache first")
                path = (
                    Path(cache)
                    / (source["chart"] + "-" + str(source["targetRevision"]))
                    / source["chart"]
                )
                if not (path / "Chart.yaml").is_file():
                    raise ValueError(
                        "Exact pinned chart is missing: " + source["chart"]
                    )
            elif source.get("repoURL") == OWN_REPO and source.get("path"):
                path = public_path(self.root, source["path"])
                for nested in path.rglob("*"):
                    if nested.is_symlink():
                        public_path(self.root, str(nested.relative_to(self.root)))
            else:
                # External raw directories are private integration interfaces,
                # not fake resources or an excuse to import their configuration.
                continue
            if (path / "Chart.yaml").is_file():
                release = source.get("helm", {}).get("releaseName", name)
                command = [
                    "helm",
                    "template",
                    release,
                    str(path),
                    "--namespace",
                    namespace,
                    "--kube-version",
                    "1.37.1",
                    "-f",
                    "-",
                ]
                if not source.get("helm", {}).get("skipCrds", False):
                    command.append("--include-crds")
                if capabilities:
                    for version in (
                        "monitoring.coreos.com/v1",
                        "gateway.networking.k8s.io/v1",
                        "gateway.networking.k8s.io/v1/HTTPRoute",
                    ):
                        command += ["--api-versions", version]
                documents += _render_documents(command, self.values(source))
            elif any((path / name).exists() for name in KUSTOMIZATION_NAMES):
                documents += render_kustomization(self.root, path)
            else:
                for file in sorted(path.iterdir()):
                    if file.suffix in {".yaml", ".yml"}:
                        safe = public_path(self.root, str(file.relative_to(self.root)))
                        raw = parse_documents(safe.read_text())
                        if any(
                            d["kind"] == "Secret" and ("data" in d or "stringData" in d)
                            for d in raw
                        ):
                            raise ValueError(
                                "Public raw sources must not contain credential Secret values"
                            )
                        documents += raw
        for document in documents:
            if document["kind"] in NAMESPACED:
                document["metadata"].setdefault("namespace", namespace)
        self._cache[key] = documents
        return copy.deepcopy(documents)


KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
PRIVATE_DIRECTORIES = {
    "private",
    "secrets",
    "credentials",
    "k3s-on-omv-apps",
    "k3s-on-omv-live",
    "k3s-on-omv-bootstrap",
}


def _public_parts(path: Path) -> bool:
    return all(
        not p.startswith(".") and p.lower() not in PRIVATE_DIRECTORIES
        for p in path.parts
    )


def _kustomize_input(root: Path, base: Path, name: str) -> Path:
    # Accept simple local names and leading ../ for sibling bases. Never pass a
    # URL, Git shorthand, glob or ambiguous symlink/.. path on to the renderer.
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[\w./-]+", name, re.ASCII)
        or "//" in name
        or name.lower().startswith("github.com/")
        or Path(name).is_absolute()
    ):
        raise ValueError("Expected a relative local Kustomize input")
    parts = list(Path(name).parts)
    while parts and parts[0] == "..":
        if base == root:
            raise ValueError("Kustomize source escapes repository")
        base = base.parent
        parts.pop(0)
    return public_path(root, str(base.relative_to(root).joinpath(*parts)))


def _require_local_file(base: Path, path: Path) -> Path:
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError("Expected a regular file inside its Kustomization directory")
    return path


def _fields(value: Any, allowed: set[str]) -> Document:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unsupported Kustomize field; review offline inputs first")
    return value


def _entries(config: Document, key: str) -> list[Any]:
    items = config.get(key, [])
    if not isinstance(items, list):
        raise ValueError("Expected a Kustomize input list")
    return items


def validate_kustomization(root: Path, directory: Path) -> None:
    """Preflight the entire local graph, failing closed on unreviewed loaders."""
    relative = directory.absolute().relative_to(root.absolute())
    root = root.resolve()
    pending = [public_path(root, str(relative))]
    seen: set[Path] = set()
    while pending:
        base = pending.pop()
        if base in seen:
            continue
        seen.add(base)
        names = [n for n in KUSTOMIZATION_NAMES if (base / n).exists()]
        if len(names) != 1:
            raise ValueError("Expected one local Kustomization file")
        config_path = _require_local_file(base, _kustomize_input(root, base, names[0]))
        config = _fields(
            yaml.safe_load(config_path.read_text()),
            {
                "apiVersion",
                "kind",
                "namespace",
                "resources",
                "bases",
                "components",
                "patches",
                "generatorOptions",
                "configMapGenerator",
            },
        )
        for key in ("resources", "bases", "components"):
            for resource in _entries(config, key):
                target = _kustomize_input(root, base, resource)
                if target.is_dir():
                    pending.append(target)
                else:
                    _require_local_file(base, target)

        files = []
        for patch in _entries(config, "patches"):
            _fields(patch, {"path", "patch", "target", "options"})
            if "path" in patch:
                files.append(patch["path"])
        for generator in _entries(config, "configMapGenerator"):
            _fields(generator, {"name", "namespace", "options", "files"})
            for item in _entries(generator, "files"):
                if not isinstance(item, str):
                    raise ValueError("Expected a Kustomize generator filename")
                files.append(item.split("=", 1)[-1])
        for name in files:
            _require_local_file(base, _kustomize_input(root, base, name))


def render_kustomization(root: Path, directory: Path) -> list[Document]:
    """Render only after every transitive input has passed preflight."""
    validate_kustomization(root, directory)
    command = (
        ["kustomize", "build"]
        if shutil.which("kustomize")
        else ["kubectl", "kustomize"]
    )
    return _render_documents([*command, str(directory.resolve())])


def public_path(root: Path, relative: str) -> Path:
    """Resolve only a relative source confined to the public repository."""
    path = Path(relative)
    if path.is_absolute() or not _public_parts(path):
        raise ValueError(
            "Public source escapes repository or references a private path"
        )
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Expected a repository directory")
    resolved = (root / path).resolve()
    if (
        not resolved.is_relative_to(root)
        or not _public_parts(resolved.relative_to(root))
        or not resolved.exists()
    ):
        raise ValueError("Public source is missing or redirected outside repository")
    # A private checkout can also be nested under an arbitrary directory name.
    parent = resolved if resolved.is_dir() else resolved.parent
    while parent != root:
        if (parent / ".git").exists():
            raise ValueError("Public source enters a nested repository")
        parent = parent.parent
    return resolved


def selector_matches(selector: Document, labels: dict[str, str]) -> bool:
    """Evaluate the label-selector operators used by Kubernetes monitors."""
    if any(labels.get(k) != v for k, v in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values", [])
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and labels.get(key) in values:
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
        if operator not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            return False
    return True


def monitor_has_target(monitor: Document, documents: list[Document]) -> bool:
    """Require each ServiceMonitor endpoint to resolve to a selected named port."""
    spec = monitor["spec"]
    namespaces = spec.get("namespaceSelector", {})
    selected = []
    for service in documents:
        if service.get("kind") != "Service":
            continue
        metadata = service["metadata"]
        if not namespaces.get("any"):
            allowed = namespaces.get(
                "matchNames", [monitor["metadata"].get("namespace")]
            )
            if metadata.get("namespace") not in allowed:
                continue
        if selector_matches(spec["selector"], metadata.get("labels", {})):
            selected.append(service)
    return (
        bool(selected)
        and bool(spec["endpoints"])
        and all(
            endpoint.get("port") is not None
            and any(
                endpoint.get("port") == port.get("name")
                for service in selected
                for port in service["spec"]["ports"]
            )
            for endpoint in spec["endpoints"]
        )
    )
