# frozen_string_literal: true

require "minitest/autorun"
require "pathname"
require "yaml"

ROOT = Pathname(__dir__).parent.parent
INFRA = ROOT / "k3s-on-omv-infra"
LIVE = ROOT / "k3s-on-omv-live"
APPS = ROOT / "k3s-on-omv-apps"
BOOTSTRAP = ROOT / "k3s-on-omv-bootstrap"

EXPECTED_INFRA = {
  "argocd-config" => 1,
  "prometheus-operator-crds" => 1,
  "cert-manager" => -5,
  "tls-certificates" => -4,
  "csi-driver-smb" => 2,
  "k8up" => 2,
  "longhorn" => 2,
  "crossplane" => 2,
  "vault-secrets-operator" => 2,
  "cloudnative-pg" => 2,
  "barman-cloud" => 2,
  "istio-core" => -3,
  "istio-ambient" => -2,
  "provider-vault" => 3,
  "openbao" => 4,
  "provider-vault-connection" => 5,
  "openbao-config" => 6,
  "longhorn-config" => 6,
  "radar-config" => 6,
  "platform-credentials" => 6,
  "openbao-backups" => 6,
  "letsencrypt-dns01" => 6,
  "k3s-postgres" => 7,
  "kube-prometheus-stack" => 8,
  "backup-validation" => 8
}.freeze

REPOSITORIES = [BOOTSTRAP, INFRA, LIVE, APPS].freeze
YAML_REPOSITORIES = [INFRA, LIVE, APPS].freeze

module ContractHelpers
  def yaml_documents(path)
    YAML.load_stream(path.read).compact.select { |document| document.is_a?(Hash) }
  rescue Psych::SyntaxError => error
    flunk("#{path.relative_path_from(ROOT)} is invalid YAML: #{error.message}")
  end

  def yaml_files(root)
    root.glob("**/*.{yml,yaml}").reject { |path| path.to_s.include?("/.git/") }
  end

  def application(path)
    yaml_documents(path).fetch(0)
  end

  def application_files(root)
    root.glob("**/app.yml").sort
  end

  def walk(value, key = nil, &block)
    yield(key, value)
    case value
    when Hash
      value.each { |child_key, child| walk(child, child_key, &block) }
    when Array
      value.each { |child| walk(child, key, &block) }
    end
  end

  def referenced_repository(source)
    value_files = source.dig("helm", "valueFiles") || []
    return INFRA if value_files.any? { |value| value.start_with?("$infra/") }
    return LIVE if value_files.any? { |value| value.start_with?("$live/") }
    return APPS if value_files.any? { |value| value.start_with?("$apps/") }

    case source["repoURL"]
    when "https://github.com/dmuiX/k3s-on-omv-infra.git" then INFRA
    when "https://github.com/dmuiX/k3s-on-omv-live.git" then LIVE
    when "https://github.com/dmuiX/k3s-on-omv-apps.git" then APPS
    end
  end
end

class RepositoryContractTest < Minitest::Test
  include ContractHelpers

  def test_exact_canonical_infra_children_and_waves
    actual = application_files(INFRA).to_h do |path|
      app = application(path)
      [app.dig("metadata", "name"), Integer(app.dig("metadata", "annotations", "argocd.argoproj.io/sync-wave"))]
    end
    assert_equal EXPECTED_INFRA, actual
  end

  def test_roots_select_only_their_children
    infra_root = application(INFRA / "infra.yml")
    apps_root = application(APPS / "apps.yml")
    assert_equal "*/*/app.yml", infra_root.dig("spec", "source", "directory", "include")
    assert_equal "*/app.yml", apps_root.dig("spec", "source", "directory", "include")
    assert_equal ["radar"], application_files(APPS).map { |path| application(path).dig("metadata", "name") }
    assert_empty LIVE.glob("**/app.yml")
    refute application_files(INFRA).any? { |path| application(path).dig("metadata", "name") == "radar" }
    radar_prerequisites = application(INFRA / "06-platform/radar/app.yml")
    refute radar_prerequisites.dig("spec", "sources").any? { |source| source.key?("chart") || source["repoURL"] == "https://github.com/dmuiX/k3s-on-omv-apps.git" }
  end

  def test_argo_automation_self_heals_without_pruning
    (application_files(INFRA) + application_files(APPS) + [INFRA / "infra.yml", APPS / "apps.yml"]).each do |path|
      automated = application(path).dig("spec", "syncPolicy", "automated")
      assert_equal true, automated["selfHeal"], path.to_s
      assert_equal false, automated["prune"], path.to_s
      assert_equal false, automated["allowEmpty"], path.to_s
    end
  end

  def test_multi_source_order_and_references
    (application_files(INFRA) + application_files(APPS)).each do |path|
      app = application(path)
      sources = app.dig("spec", "sources")
      next if app.dig("metadata", "name") == "prometheus-operator-crds"

      assert_kind_of Array, sources, path.to_s
      refs = sources.each_index.select { |index| sources[index].key?("ref") }
      expected_refs = sources.flat_map do |source|
        Array(source.dig("helm", "valueFiles")).map do |value_file|
          value_file[/^\$(infra|apps|live)\//, 1]
        end.compact
      end.uniq
      assert_equal expected_refs, refs.map { |index| sources[index]["ref"] }, path.to_s
      chart_index = sources.index { |source| source.key?("chart") }
      assert_equal 0, chart_index, path.to_s if chart_index
      resource_indices = sources.each_index.select { |index| sources[index].key?("path") }
      # Official chart-only Istio components need no companion raw resources.
      refute_empty resource_indices, path.to_s unless %w[istio-istiod istio-cni istio-ztunnel].include?(app.dig("metadata", "name"))
      assert resource_indices.all? { |index| refs.empty? || index >= refs.max }, path.to_s
    end
  end

  def test_every_local_path_and_value_file_exists
    (application_files(INFRA) + application_files(APPS)).each do |path|
      app = application(path)
      (app.dig("spec", "sources") || []).each do |source|
        repository = referenced_repository(source)
        assert (repository / source["path"]).exist?, "missing #{source['path']} referenced by #{path}" if repository && source["path"]
        (source.dig("helm", "valueFiles") || []).each do |value_file|
          match = value_file.match(/^\$(infra|live|apps)\/(.+)$/)
          next unless match
          base = {"infra" => INFRA, "live" => LIVE, "apps" => APPS}.fetch(match[1])
          assert (base / match[2]).file?, "missing #{value_file} referenced by #{path}"
        end
      end
    end
  end

  def test_live_sources_and_values_resolve_against_reorganized_tree
    references = []
    [INFRA, APPS].each do |repository|
      yaml_files(repository).each do |path|
        yaml_documents(path).select { |document| document["kind"] == "Application" }.each do |app|
          spec = app.fetch("spec")
          sources = spec["sources"] || [spec.fetch("source")]
          sources.each do |source|
            refute_match(/\AREQUIRED_(?:LIVE|INFRA|APPS)_REPOSITORY_URL\z/, source.fetch("repoURL"), path.to_s)
          end
          live_sources = sources.select { |source| source["repoURL"] == "https://github.com/dmuiX/k3s-on-omv-live.git" }
          live_sources.each do |source|
            next unless source["path"]
            references << source["path"]
            assert (LIVE / source["path"]).directory?, "missing Live source.path #{source['path']} in #{path}"
          end
          sources.each do |source|
            Array(source.dig("helm", "valueFiles")).grep(/^\$live\//).each do |value_file|
              assert live_sources.any? { |live| live["ref"] == "live" }, "missing Live ref in #{path}"
              relative = value_file.delete_prefix("$live/")
              references << relative
              assert (LIVE / relative).file?, "missing #{value_file} in #{path}"
            end
          end
        end
      end
    end
    refute_empty references
    assert references.any? { |path| path.start_with?("infra/") }
    assert references.any? { |path| path.start_with?("apps/") }
    references.each { |path| assert_match(/\A(?:infra|apps)\//, path) }
  end

  def test_resource_identity_has_one_owner
    owners = Hash.new { |hash, key| hash[key] = [] }
    [INFRA, LIVE, APPS].each do |repository|
      yaml_files(repository).each do |path|
        next if path.basename.to_s == "app.yml" || %w[infra.yml apps.yml values.yml].include?(path.basename.to_s) || path.to_s.end_with?("-values.yml")
        yaml_documents(path).each do |document|
          metadata = document["metadata"] || {}
          next unless document["apiVersion"] && document["kind"] && metadata["name"]
          identity = [document["apiVersion"], document["kind"], metadata["namespace"], metadata["name"]]
          owners[identity] << path.relative_path_from(ROOT).to_s
        end
      end
    end
    duplicates = owners.select { |_identity, paths| paths.uniq.length > 1 }
    assert_empty duplicates, duplicates.inspect
  end

  def test_no_resolved_secrets_or_mutable_latest_tags
    YAML_REPOSITORIES.each do |repository|
      yaml_files(repository).each do |path|
        text = path.read
        refute_match(/(?:image|package):\s*\S*(?::latest(?:\s|$)|@latest(?:\s|$))/, text, path.to_s)
        yaml_documents(path).each do |document|
          if document["kind"] == "Secret"
            refute document.key?("data"), path.to_s
            refute document.key?("stringData"), path.to_s
          end
          walk(document) do |key, value|
            next unless key == "image" && value.is_a?(String)
            assert(value.include?("@sha256:") || value.start_with?("REQUIRED_VERIFIED_"), "unpinned image #{value} in #{path}")
          end
        end
      end
    end
    REPOSITORIES.each do |repository|
      repository.glob("**/*").select(&:file?).reject { |path| path.to_s.include?("/.git/") || path.to_s.include?("/__pycache__/") }.each do |path|
        refute_match(/-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/, path.read, path.to_s)
      end
    end
  end

  def test_production_manifests_have_no_unresolved_markers_or_static_gate
    unresolved = [INFRA, LIVE, APPS].flat_map { |repository| yaml_files(repository) }
      .reject { |path| path.to_s.include?("/tests/") }
      .select { |path| path.read.match?(/:\s*REQUIRED_[A-Z0-9_]+/) }
    assert_empty unresolved
    refute (BOOTSTRAP / "DEPLOYMENT-GATE.yml").exist?
  end
end

class PlatformContractTest < Minitest::Test
  include ContractHelpers

  def test_provider_vault_activates_exactly_the_specified_sixteen_types
    document = yaml_documents(INFRA / "03-controllers/provider-vault/resources/provider.yaml")
      .find { |item| item["kind"] == "ManagedResourceActivationPolicy" }
    activated = document.dig("spec", "activate")
    assert_equal 16, activated.length
    assert_equal 16, activated.uniq.length
    assert_includes activated, "oidcassignments.identity.vault.upbound.io"
    assert_includes activated, "secretbackendstaticroles.database.vault.upbound.io"
    refute activated.any? { |type| type.include?(".pki.vault.upbound.io") }
  end

  def test_provider_vault_runtime_uses_default_seccomp
    runtime = yaml_documents(INFRA / "03-controllers/provider-vault/resources/provider.yaml").find { |doc| doc["kind"] == "DeploymentRuntimeConfig" }
    assert_equal "RuntimeDefault", runtime.dig("spec", "deploymentTemplate", "spec", "template", "spec", "securityContext", "seccompProfile", "type")
  end

  def test_provider_vault_resource_waves_are_minus_twenty_minus_ten_zero
    documents = yaml_documents(INFRA / "03-controllers/provider-vault/resources/provider.yaml")
    waves = documents.to_h { |document| [document["kind"], document.dig("metadata", "annotations", "argocd.argoproj.io/sync-wave")] }
    assert_equal "-20", waves["ManagedResourceActivationPolicy"]
    assert_equal "-10", waves["DeploymentRuntimeConfig"]
    assert_equal "0", waves["Provider"]
  end

  def test_storage_classes_encode_replication_encryption_and_locality
    documents = yaml_documents(INFRA / "02-controllers/longhorn/resources/storageclasses.yaml")
    classes = documents.to_h { |document| [document.dig("metadata", "name"), document] }
    assert_equal %w[longhorn longhorn-local longhorn-monitoring], classes.keys.sort
    assert_equal "Retain", classes["longhorn"]["reclaimPolicy"]
    assert_equal "true", classes["longhorn"].dig("parameters", "encrypted")
    assert_equal "3", classes["longhorn"].dig("parameters", "numberOfReplicas")
    %w[longhorn-local longhorn-monitoring].each do |name|
      storage_class = classes[name]
      assert_equal "Retain", storage_class["reclaimPolicy"]
      assert_equal "WaitForFirstConsumer", storage_class["volumeBindingMode"]
      assert_equal "true", storage_class.dig("parameters", "encrypted")
      assert_equal "1", storage_class.dig("parameters", "numberOfReplicas")
      assert_equal "strict-local", storage_class.dig("parameters", "dataLocality")
    end
    assert_equal "monitoring-storage", classes["longhorn-monitoring"].dig("parameters", "nodeSelector")
    classes.each_value do |storage_class|
      %w[node-stage node-publish node-expand].each do |operation|
        assert_equal "longhorn-crypto", storage_class.dig("parameters", "csi.storage.k8s.io/#{operation}-secret-name")
        assert_equal "longhorn", storage_class.dig("parameters", "csi.storage.k8s.io/#{operation}-secret-namespace")
      end
    end
  end

  def test_longhorn_manager_and_system_components_tolerate_storage_pi
    values = YAML.load_file(INFRA / "02-controllers/longhorn/values.yml")
    assert_equal "CriticalAddonsOnly=true:NoSchedule", values.dig("defaultSettings", "taintToleration")
    assert_includes values.dig("longhornManager", "tolerations") || [],
      {"key" => "CriticalAddonsOnly", "operator" => "Equal", "value" => "true", "effect" => "NoSchedule"}
  end

  def test_openbao_hcl_uses_newline_separated_attributes
    config = YAML.load_file(INFRA / "04-core/openbao/values.yml").dig("server", "ha", "raft", "config")
    refute_includes config, ";", "HCL attributes must be separated by newlines, not semicolons"
    assert_includes config, 'current_key = "file:///openbao/seal/key"'
  end

  def test_longhorn_has_one_storageclass_owner_and_canonical_replica_setting
    values = YAML.load_file(INFRA / "02-controllers/longhorn/values.yml")
    assert_equal false, values.dig("persistence", "createStorageClass"),
      "The chart ConfigMap must not recreate the Git-owned encrypted StorageClass"
    assert_equal '{"v1":"3","v2":"3"}', values.dig("defaultSettings", "defaultReplicaCount"),
      "Use Longhorn 1.13's persisted form to avoid repeated scalar-to-JSON setting updates"
  end

  def test_openbao_uses_server_side_diff_for_api_defaults
    app = application(INFRA / "04-core/openbao/app.yml")
    assert_equal "ServerSideDiff=true", app.dig("metadata", "annotations", "argocd.argoproj.io/compare-options")
  end

  def test_openbao_server_drops_all_capabilities
    values = YAML.load_file(INFRA / "04-core/openbao/values.yml")
    security = values.dig("server", "statefulSet", "securityContext", "container") || {}
    assert_equal false, security["allowPrivilegeEscalation"]
    assert_equal ["ALL"], security.dig("capabilities", "drop")
  end

  def test_ha_replica_and_storage_contracts_are_present
    openbao = YAML.load_file(INFRA / "04-core/openbao/values.yml")
    assert_equal 3, openbao.dig("server", "ha", "replicas")
    assert_equal "1Gi", openbao.dig("server", "dataStorage", "size")
    assert_equal "longhorn", openbao.dig("server", "dataStorage", "storageClass")

    postgresql = yaml_documents(INFRA / "07-data/k3s-postgres/resources/cluster.yaml").find { |document| document["kind"] == "Cluster" }
    assert_equal 3, postgresql.dig("spec", "instances")
    assert_equal "longhorn-local", postgresql.dig("spec", "storage", "storageClass")
    assert_equal "5Gi", postgresql.dig("spec", "storage", "size")
    assert_equal 1, postgresql.dig("spec", "postgresql", "synchronous", "number")
    assert_equal true, postgresql.dig("spec", "postgresql", "synchronous", "failoverQuorum")
    refute postgresql.key?("certificates")
    refute_match(/serverTLSSecret|sslrootcert|verify-full/, (INFRA / "07-data/k3s-postgres/resources/cluster.yaml").read)
    assert_includes (INFRA / "07-data/k3s-postgres/resources/network-policy.yaml").read, "mode: STRICT"

    monitoring = YAML.load_file(INFRA / "08-observability/kube-prometheus-stack/values.yml")
    assert_equal 2, monitoring.dig("prometheus", "prometheusSpec", "replicas")
    assert_equal 2, monitoring.dig("alertmanager", "alertmanagerSpec", "replicas")
    assert_equal 2, monitoring.dig("grafana", "replicas")
  end

  def test_static_containers_have_resources_and_hardened_security_contexts
    YAML_REPOSITORIES.each do |repository|
      yaml_files(repository).each do |path|
        yaml_documents(path).each do |document|
          walk(document) do |key, value|
            next unless key == "containers" && value.is_a?(Array)
            value.each do |container|
              next unless container.is_a?(Hash)
              assert container.dig("resources", "requests"), "missing requests for #{container['name']} in #{path}"
              assert container.dig("resources", "limits"), "missing limits for #{container['name']} in #{path}"
              security = container["securityContext"] || {}
              assert_equal false, security["allowPrivilegeEscalation"], path.to_s
              assert_includes security.dig("capabilities", "drop") || [], "ALL", path.to_s
            end
          end
        end
      end
    end
  end

  def test_pi_tolerations_are_limited_to_approved_workloads
    allowed = %w[longhorn openbao k3s-postgres istio-ambient kube-prometheus-stack]
    offenders = []
    YAML_REPOSITORIES.each do |repository|
      yaml_files(repository).each do |path|
        offenders << path.relative_path_from(ROOT).to_s if path.read.include?("CriticalAddonsOnly") && allowed.none? { |name| path.to_s.include?(name) }
      end
    end
    assert_empty offenders
  end

  def test_openbao_policy_boundaries_are_narrow
    documents = application_files(INFRA).flat_map do |path|
      app = application(path)
      (app.dig("spec", "sources") || []).select { |source| source["repoURL"] == "https://github.com/dmuiX/k3s-on-omv-infra.git" && source["path"] }.flat_map do |source|
        yaml_files(INFRA / source["path"]).flat_map { |file| yaml_documents(file) }
      end
    end
    policies = documents.select { |document| document["kind"] == "Policy" }.to_h do |document|
      [document.dig("metadata", "name"), document.dig("spec", "forProvider", "policy")]
    end
    %w[openbao-snapshot postgresql-vso-read grafana-vso-read radar-vso-read cert-manager-dns01-read platform-r2-read smb-vso-read].each do |name|
      assert policies.key?(name), name
    end
    all_policy_text = policies.values.join("\n")
    refute_match(%r{path\s+"\*"}, all_policy_text)
    refute_match(/capabilities\s*=\s*\[[^\]]*"delete"/, all_policy_text)
    assert_includes policies["openbao-snapshot"], 'path "sys/storage/raft/snapshot"'
    refute_includes policies["openbao-snapshot"], 'path "kv/data/platform/r2"'
    refute_includes policies["postgresql-vso-read"], 'path "kv/data/platform/r2"'
    assert_includes policies["platform-r2-read"], 'path "kv/data/platform/r2"'
    assert_includes policies["smb-vso-read"], 'path "kv/data/platform/smb"'
    refute_match(/pki-(?:services|clients)/, all_policy_text)
  end

  def test_istio_is_the_only_gateway_controller_and_live_owns_private_gateway
    bootstrap_corpus = BOOTSTRAP.glob("**/*.{yml,yaml,j2,md}").select(&:file?).map(&:read).join("\n")
    refute_match(/kind:\s*GatewayClass/, bootstrap_corpus)
    refute_match(/Install pinned Traefik/, bootstrap_corpus)
    gateway = yaml_documents(LIVE / "infra/istio-ambient/gateway.yaml").find { |document| document["kind"] == "Gateway" }
    assert_equal "istio-gateway", gateway.dig("metadata", "name")
    assert_equal "istio-gateway", gateway.dig("metadata", "namespace")
    assert_equal "istio", gateway.dig("spec", "gatewayClassName")
    assert_equal({"group" => "", "kind" => "ConfigMap", "name" => "istio-gateway-options"}, gateway.dig("spec", "infrastructure", "parametersRef"))
    options = yaml_documents(INFRA / "01-bootstrap/istio-ambient/resources/gateway-config.yaml").find { |document| document["kind"] == "ConfigMap" }
    deployment = YAML.safe_load(options.dig("data", "deployment"))
    assert_equal 3, deployment.dig("spec", "replicas")
    pod = deployment.dig("spec", "template", "spec")
    assert_equal "kubernetes.io/hostname", pod.dig("affinity", "podAntiAffinity", "preferredDuringSchedulingIgnoredDuringExecution", 0, "podAffinityTerm", "topologyKey")
    assert_equal "RuntimeDefault", pod.dig("securityContext", "seccompProfile", "type")
    assert_equal "ScheduleAnyway", pod.dig("topologySpreadConstraints", 0, "whenUnsatisfiable")
    assert_equal "CriticalAddonsOnly", pod.dig("tolerations", 0, "key")
    pdb = YAML.safe_load(options.dig("data", "podDisruptionBudget"))
    assert_equal 2, pdb.dig("spec", "minAvailable")
    assert_equal "istio-gateway", pdb.dig("spec", "selector", "matchLabels", "gateway.networking.k8s.io/gateway-name")
  end

  def test_official_mesh_chart_pins
    core = application(INFRA / "01-bootstrap/istio-core/app.yml")
    charts = core.dig("spec", "sources").select { |source| source["chart"] }
    assert_equal %w[base istiod cni ztunnel], charts.map { |source| source["chart"] }
    charts.each do |source|
      assert_equal "https://blob.istio.io/istio-release/charts", source["repoURL"]
      assert_equal "1.31.2", source["targetRevision"]
    end
    assert_equal ["$infra/01-bootstrap/istio-ambient/istiod-values.yml"], charts[1].dig("helm", "valueFiles")
    assert_equal %w[CreateNamespace=true ServerSideApply=true], core.dig("spec", "syncPolicy", "syncOptions")
    assert_equal %w[CreateNamespace=true ServerSideApply=true], application(INFRA / "01-bootstrap/istio-ambient/app.yml").dig("spec", "syncPolicy", "syncOptions")
    %w[istiod-values.yml cni-values.yml].each do |file|
      assert_equal "ambient", YAML.load_file(INFRA / "01-bootstrap/istio-ambient/#{file}")["profile"]
    end
    assert_equal "k3s", YAML.load_file(INFRA / "01-bootstrap/istio-ambient/cni-values.yml").dig("global", "platform")
  end

  def test_prometheus_snapshot_first_k8up_support_uses_reviewed_standard_library_tooling
    refute (INFRA / "08-observability/fluent-bit").exist?
    refute (LIVE / "infra/fluent-bit-values.yml").exist?
    assert_equal false, YAML.load_file(INFRA / "08-observability/kube-prometheus-stack/values.yml").dig("crds", "enabled")

    monitoring = YAML.load_file(INFRA / "08-observability/kube-prometheus-stack/values.yml")
    assert_equal true, monitoring.dig("prometheus", "prometheusSpec", "enableAdminAPI")
    documents = yaml_documents(INFRA / "08-observability/kube-prometheus-stack/resources/prometheus-snapshot.yaml")
    contract = documents.find { |document| document["kind"] == "ConfigMap" }
    pre_backup = documents.find { |document| document["kind"] == "PreBackupPod" }
    assert contract
    assert pre_backup
    assert_equal "/usr/local/bin/python /tools/snapshot.py", pre_backup.dig("spec", "backupCommand")
    assert_includes contract.dig("data", "snapshot.py"), "/api/v1/admin/tsdb/snapshot"
    assert_includes contract.dig("data", "snapshot.py"), "tarfile.open"
    refute_includes contract.dig("data", "snapshot.py"), "shutil.rmtree"
    assert_match(/@sha256:[0-9a-f]{64}\z/, pre_backup.dig("spec", "pod", "spec", "containers", 0, "image"))
    runbook = (INFRA / "docs/prometheus-tsdb-backup-runbook.md").read
    assert_includes runbook, "Completed=True"
    assert_includes runbook, "reason=Succeeded"
    assert_includes runbook, "keepDaily: 7"
    assert_includes runbook, "keepWeekly: 4"
    assert_equal %w[credential-custody openbao-snapshot postgresql-size-retention], (INFRA / "adapters").children.select(&:directory?).map { |path| path.basename.to_s }.sort
    assert_equal 2, (INFRA / "adapters").glob("**/*.py").length
  end

  def test_native_chart_routes_are_preferred_over_duplicate_live_manifests
    monitoring = YAML.load_file(LIVE / "infra/kube-prometheus-stack-values.yml")
    assert_equal true, monitoring.dig("grafana", "route", "main", "enabled")
    assert_equal "HTTPRoute", monitoring.dig("grafana", "route", "main", "kind")
    refute (LIVE / "infra/kube-prometheus-stack/route.yaml").exist?

    radar = YAML.load_file(LIVE / "apps/radar-values.yml")
    assert_equal true, radar.dig("httpRoute", "enabled")
    assert_equal "gateway.networking.k8s.io/v1", radar.dig("httpRoute", "apiVersion")
    refute (LIVE / "apps/radar/route.yaml").exist?
  end

  def test_alertmanager_and_longhorn_proxy_use_vso_and_pinned_proxy_image
    alertmanager = (INFRA / "08-observability/kube-prometheus-stack/resources/alertmanager-email.yaml").read
    assert_includes alertmanager, "path: monitoring/alertmanager-config"
    assert_includes alertmanager, "name: alertmanager-platform-config"
    assert_equal "alertmanager-platform-config", YAML.load_file(INFRA / "08-observability/kube-prometheus-stack/values.yml").dig("alertmanager", "alertmanagerSpec", "configSecret")
    proxy = (INFRA / "06-platform/longhorn/resources/oidc-proxy.yaml").read
    assert_includes proxy, "quay.io/oauth2-proxy/oauth2-proxy:v7.15.5@sha256:8498b0d0ef0a7b29686414000a08aee467f02d0299c9ed1e006a8f33fc017916"
    assert_includes proxy, "upstream=http://longhorn-frontend.longhorn.svc:80"
    assert_includes proxy, "rolloutRestartTargets"
    refute_includes (INFRA / "02-controllers/longhorn/app.yml").read, "path: infra/longhorn"
    assert_includes (INFRA / "06-platform/longhorn/app.yml").read, "path: infra/longhorn"
    refute_includes (INFRA / "06-platform/openbao-config/app.yml").read, "infra/longhorn"
    route = yaml_documents(LIVE / "infra/longhorn/route.yaml").find { |document| document["kind"] == "HTTPRoute" }
    assert_equal "longhorn-oidc-proxy", route.dig("spec", "rules", 0, "backendRefs", 0, "name")
  end

  def test_vso_transformations_fail_closed_on_missing_required_keys
    files = [
      "06-platform/platform-credentials/resources/credentials.yaml",
      "06-platform/openbao-backups/resources/snapshot.yaml",
      "06-platform/longhorn/resources/oidc-proxy.yaml",
      "07-data/k3s-postgres/resources/credentials.yaml",
      "08-observability/backup-validation/resources/credentials.yaml",
      "08-observability/kube-prometheus-stack/resources/alertmanager-email.yaml",
      "08-observability/kube-prometheus-stack/resources/prometheus-k8up-credentials.yaml",
      "08-observability/kube-prometheus-stack/resources/grafana.yaml"
    ]
    files.each do |relative|
      manifest = (INFRA / relative).read
      assert_includes manifest, "excludeRaw: true", relative
      assert_includes manifest, "fail \"missing required OpenBao key:", relative
    end
  end

  def test_default_deny_is_targeted_to_sensitive_namespaces
    namespaces = (YAML_REPOSITORIES.flat_map { |repository| yaml_files(repository) }).flat_map do |path|
      yaml_documents(path).map do |document|
        document.dig("metadata", "namespace") if document["kind"] == "NetworkPolicy" && document.dig("metadata", "name") == "default-deny"
      end.compact
    end.sort
    assert_equal %w[backup-validation cloudnative-pg openbao radar], namespaces
  end

  def test_official_smb_driver_and_dynamic_local_k8up_repositories
    source = application(INFRA / "02-controllers/csi-driver-smb/app.yml").dig("spec", "sources", 0)
    assert_equal "https://raw.githubusercontent.com/kubernetes-csi/csi-driver-smb/master/charts", source["repoURL"]
    assert_equal "csi-driver-smb", source["chart"]
    assert_equal "1.20.3", source["targetRevision"]
    assert_includes application(INFRA / "02-controllers/csi-driver-smb/app.yml").dig("spec", "syncPolicy", "syncOptions"), "SkipDryRunOnMissingResource=true"
    assert_equal false, YAML.load_file(INFRA / "02-controllers/csi-driver-smb/values.yml").dig("windows", "enabled")

    storage_class = YAML.load_file(LIVE / "infra/csi-driver-smb/storageclass.yaml")
    assert_equal "smb.csi.k8s.io", storage_class["provisioner"]
    assert_equal "//192.168.1.5/k3s-cluster", storage_class.dig("parameters", "source")
    assert_equal "${pvc.metadata.namespace}/${pvc.metadata.name}", storage_class.dig("parameters", "subDir")
    assert_equal "retain", storage_class.dig("parameters", "onDelete")
    %w[provisioner node-stage].each do |stage|
      assert_equal "smb-credentials", storage_class.dig("parameters", "csi.storage.k8s.io/#{stage}-secret-name")
      assert_equal "kube-system", storage_class.dig("parameters", "csi.storage.k8s.io/#{stage}-secret-namespace")
    end
    assert_equal "Retain", storage_class["reclaimPolicy"]

    targets = yaml_documents(LIVE / "infra/backup-validation/targets.yaml")
    schedule = targets.find { |doc| doc["kind"] == "Schedule" }.fetch("spec")
    assert_equal({"mountPath" => "/repository"}, schedule.dig("backend", "local"))
    assert_equal %w[local repoPasswordSecretRef volumeMounts], schedule.fetch("backend").keys.sort
    assert_equal({"keepDaily" => 7, "keepWeekly" => 4, "keepMonthly" => 6}, schedule.dig("prune", "retention"))

    ["infra/backup-validation/k8up-omv.yaml", "infra/kube-prometheus-stack/prometheus-k8up-omv.yaml"].each do |relative|
      docs = yaml_documents(LIVE / relative)
      pvc = docs.find { |doc| doc["kind"] == "PersistentVolumeClaim" }
      pod = docs.find { |doc| doc["kind"] == "PodConfig" }
      assert_equal "smb", pvc.dig("spec", "storageClassName")
      assert_equal ["ReadWriteMany"], pvc.dig("spec", "accessModes")
      assert_equal "false", pvc.dig("metadata", "annotations", "k8up.io/backup")
      assert_equal pvc.dig("metadata", "name"), pod.dig("spec", "template", "spec", "volumes", 0, "persistentVolumeClaim", "claimName")
      refute docs.any? { |doc| doc["kind"] == "PersistentVolume" }
    end

    corpus = [INFRA, LIVE].flat_map { |repo| yaml_files(repo) }.map(&:read).join("\n")
    refute_match(/kind:\s*PersistentVolume\s.*\bnfs:/m, corpus)
    refute_match(/k8up(?:-r2|-bucket|-endpoint|\/r2-credentials)|k8up-volumes/, corpus)
  end

  def test_shared_r2_source_has_isolated_vso_identities_including_restore_validation
    credential_files = [
      INFRA / "06-platform/platform-credentials/resources/credentials.yaml",
      INFRA / "06-platform/openbao-backups/resources/snapshot.yaml",
      INFRA / "07-data/k3s-postgres/resources/credentials.yaml",
      INFRA / "08-observability/backup-validation/resources/credentials.yaml"
    ]
    static_secrets = credential_files.flat_map { |path| yaml_documents(path) }
      .select { |document| document["kind"] == "VaultStaticSecret" && document.dig("spec", "path") == "platform/r2" }
    git_owned_object_stores = YAML_REPOSITORIES.flat_map { |repository| yaml_files(repository) }
      .flat_map { |path| yaml_documents(path) }
      .select { |document| document["kind"] == "ObjectStore" }
    assert_empty git_owned_object_stores
    bootstrap_ceremony = (BOOTSTRAP / "ansible/roles/openbao_setup/tasks/platform-backups.yml").read
    assert_includes bootstrap_ceremony, "kind: ObjectStore"
    assert_includes bootstrap_ceremony, 'endpointURL: {{ platform_r2_endpoint.user_input | to_json }}'
    assert_includes bootstrap_ceremony, "k3s-on-omv.io/create-once-owner: protected-ceremonies/platform-r2"
    refute_match(/endpointURL:\s*https:\/\//, bootstrap_ceremony)
    assert_equal %w[backup-validation cloudnative-pg kube-system openbao], static_secrets.map { |document| document.dig("metadata", "namespace") }.sort
    assert_equal %w[etcd-s3-config-secret openbao-snapshot-r2 postgresql-r2-credentials postgresql-validation-r2], static_secrets.map { |document| document.dig("spec", "destination", "name") }.sort
    etcd_destination = static_secrets.find { |document| document.dig("metadata", "namespace") == "kube-system" }
    assert_equal "etcd.k3s.cattle.io/s3-config-secret", etcd_destination.dig("spec", "destination", "type")
    assert_equal %w[etcd-s3-access-key etcd-s3-bucket etcd-s3-endpoint etcd-s3-folder etcd-s3-insecure etcd-s3-region etcd-s3-retention etcd-s3-secret-key etcd-s3-skip-ssl-verify], etcd_destination.dig("spec", "destination", "transformation", "templates").keys.sort
    openbao_destination = static_secrets.find { |document| document.dig("metadata", "namespace") == "openbao" }
    assert_equal %w[AWS_ACCESS_KEY_ID AWS_REGION AWS_SECRET_ACCESS_KEY S3_ENDPOINT], openbao_destination.dig("spec", "destination", "transformation", "templates").keys.sort
    snapshot = yaml_documents(INFRA / "06-platform/openbao-backups/resources/snapshot.yaml").find { |document| document["kind"] == "CronJob" }
    assert_equal "openbao-snapshot", snapshot.dig("spec", "jobTemplate", "spec", "template", "metadata", "labels", "app.kubernetes.io/name")
    snapshot_egress = yaml_documents(INFRA / "04-core/openbao/resources/security.yaml")
      .find { |document| document.dig("metadata", "name") == "allow-openbao-snapshot-egress" }
    assert snapshot_egress
    assert snapshot_egress.dig("spec", "egress").any? { |rule| Array(rule["ports"]).any? { |port| port["port"] == 443 } }
    openbao_network = yaml_documents(INFRA / "04-core/openbao/resources/security.yaml")
      .find { |document| document.dig("metadata", "name") == "allow-openbao-raft-and-clients" }
    assert_includes openbao_network.dig("spec", "ingress").to_s, "istio-gateway"
    assert openbao_network.dig("spec", "egress").any? { |rule| Array(rule["ports"]).any? { |port| port["port"] == 6443 } }

    validation = (INFRA / "08-observability/backup-validation/resources").glob("*.yaml").map(&:read).join("\n")
    assert_includes validation, "path: platform/r2"
    assert_includes validation, "name: postgresql-validation-r2"
    refute_match(/backup-capacity/, validation)
    refute (INFRA / "08-observability/backup-validation/resources/capacity.yaml").exist?
    egress = yaml_documents(INFRA / "08-observability/backup-validation/resources/network-policy.yaml")
      .find { |document| document.dig("metadata", "name") == "allow-backup-validation-egress" }
    assert egress.dig("spec", "egress").any? { |rule| !rule.key?("to") && Array(rule["ports"]).any? { |port| port["port"] == 443 } }
  end

  def test_restore_lease_and_no_direct_barman_s3_delete
    retention = (INFRA / "07-data/k3s-postgres/resources/retention.yaml").read
    role = yaml_documents(INFRA / "08-observability/backup-validation/resources/postgresql-retention-access.yaml").find { |document| document["kind"] == "Role" }
    rule = role.fetch("rules").find { |item| item.fetch("resources").include?("leases") }
    assert_equal ["postgresql-restore-status"], rule.fetch("resourceNames")
    assert_equal ["get"], rule.fetch("verbs")
    refute_match(/aws\s+.*s3.*delete|delete-object/, retention)
    assert_includes (INFRA / "adapters/postgresql-size-retention/retention.py").read, "--minimum-redundancy"
  end
end
