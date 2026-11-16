"""Public workload interfaces: repository confinement and rendered contracts."""

import copy
from dataclasses import dataclass, field
import os
from pathlib import Path
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

    def _run(self, argv: list[str], values: Document | None = None) -> list[Document]:
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
                documents += self._run(command, self.values(source))
            elif (path / "kustomization.yaml").is_file():
                config = yaml.safe_load((path / "kustomization.yaml").read_text())
                for field in ("resources", "bases", "components"):
                    for resource in config.get(field, []):
                        public_path(path, resource)
                documents += self._run(["kustomize", "build", str(path)])
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


def public_path(root: Path, relative: str) -> Path:
    """Resolve only a relative source confined to the public repository."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Public source escapes repository")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
        raise ValueError("Public source is missing or redirected outside repository")
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
