#!/usr/bin/env ruby
# Offline desired-state checks; not a proof of host prerequisites or working PVCs.
require 'yaml'
require 'json'
require 'pathname'

ROOT = File.expand_path('..', __dir__)
INFRA_REPOSITORY = 'https://github.com/dmuiX/k3s-on-omv-infra.git'
LIVE_REPOSITORY = 'https://github.com/dmuiX/k3s-on-omv-live.git'
INFRA_REVISION = 'main'
LIVE_REVISION = 'main'

def docs(path)
  YAML.load_stream(File.read(File.join(ROOT, path))).compact
end

def check(condition, message)
  raise message unless condition
end

def wave(app)
  Integer(app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') || '0')
end

root = docs('infra.yml').first
source = root.fetch('spec').fetch('source')
check(source['repoURL'] == INFRA_REPOSITORY && source['targetRevision'] == INFRA_REVISION,
      'The root Application must track the Infra main branch')
directory = source.fetch('directory')
include_pattern = directory.fetch('include')
check(directory['exclude'].nil?, 'The steady-state root must not exclude PostgreSQL')
root_selects = lambda do |path|
  File.fnmatch(include_pattern, path, File::FNM_EXTGLOB)
end
app_files = Dir.glob(File.join(ROOT, '*', '*', '{*app.yml,application.yml}'), File::FNM_EXTGLOB).select do |path|
  root_selects.call(path.delete_prefix(ROOT + '/'))
end
applications = app_files.flat_map { |path| YAML.load_stream(File.read(path)).compact }
check(applications.all? { |app| app['kind'] == 'Application' }, 'Root discovers unexpected resources')
apps = applications.to_h { |app| [app.dig('metadata', 'name'), app] }
check(apps.size == applications.size, 'Duplicate Application names')

pki_path = '05-platform/openbao-pki/application.yml'
postgresql_path = '06-data/cloudnative-pg/app.yml'
check(root_selects.call(pki_path) && root_selects.call(postgresql_path),
      'Root must discover both OpenBao PKI and PostgreSQL in steady state')
pki_app = apps.fetch('openbao-pki')
postgresql_app = apps.fetch('cloudnative-pg')
check(pki_app.dig('spec', 'source') == {
        'repoURL' => source['repoURL'], 'targetRevision' => INFRA_REVISION,
        'path' => '05-platform/openbao-pki/workload'
      }, 'OpenBao PKI must render its branch-tracked steady-state Kubernetes resources')
cert_manager_sources = apps.fetch('cert-manager').dig('spec', 'sources')
cert_manager_network_source = cert_manager_sources.find do |candidate|
  candidate['path'] == '02-controllers/cert-manager/network-policy'
end
cert_manager_private_source = cert_manager_sources.find { |candidate| candidate['ref'] == 'private' }
check(cert_manager_network_source && cert_manager_private_source &&
      cert_manager_network_source.dig('helm', 'valueFiles') == ['$private/clusters/omv/values.yml'] &&
      cert_manager_network_source['targetRevision'] == INFRA_REVISION &&
      cert_manager_private_source['targetRevision'] == LIVE_REVISION &&
      !cert_manager_private_source.key?('path'),
      'cert-manager must own its branch-tracked private-endpoint NetworkPolicy source')
postgres_infra_sources = postgresql_app.fetch('spec').fetch('sources').select do |candidate|
  candidate['repoURL'] == source['repoURL']
end
check(postgres_infra_sources.length == 3 &&
      postgres_infra_sources.all? { |candidate| candidate['targetRevision'] == INFRA_REVISION },
      'All same-repository PostgreSQL sources must track the Infra main branch')
# Public Applications own all resources; only their real value overrides are private.
app_files.group_by { |path| File.dirname(path) }.each do |dir, paths|
  relative = Pathname.new(dir).relative_path_from(Pathname.new(ROOT)).each_filename.to_a
  check(relative.length == 2, "Application must live under wave/component: #{dir}")
  wave_dir, component_dir = relative
  prefix = wave_dir[/\A(\d{2})-/, 1]
  check(!prefix.nil?, "Wave directory needs a numeric prefix: #{wave_dir}")
  check(!component_dir.match?(/\A\d{2}-/), "Component directory must not duplicate its wave: #{component_dir}")
  first_wave = paths.flat_map { |path| YAML.load_stream(File.read(path)).compact }.map { |app| wave(app) }.min
  check(prefix.to_i == first_wave, "Wave directory disagrees with Application annotation: #{dir}")
  paths.each do |path|
    check(YAML.load_stream(File.read(path)).compact.length == 1, "Keep later config in config-app.yml: #{path}")
  end
end

# Upstream controllers stay as Argo multi-source Helm Applications. Values are
# reviewed in Git; generated chart output is deliberately not committed.
helm_apps = %w[monitoring-crds kube-prometheus-stack cert-manager k8up longhorn openbao vault-secrets-webhook]
helm_apps.each do |name|
  app = apps.fetch(name)
  spec = app.fetch('spec')
  check(spec.key?('sources') && !spec.key?('source'), "#{name} must remain a multi-source Helm Application")
  sources = spec.fetch('sources')
  chart = sources.find { |candidate| candidate['chart'] }
  values_source = sources.find { |candidate| candidate['ref'] == 'values' }
  check(chart && chart['targetRevision'].to_s.match?(/\Av?\d+\.\d+\.\d+(?:[-+][\w.-]+)?\z/),
        "#{name} chart version is not pinned")
  check(values_source && values_source['repoURL'] == source['repoURL'] &&
        values_source['targetRevision'] == INFRA_REVISION,
        "#{name} values source must track the Infra main branch")
  chart.fetch('helm', {}).fetch('valueFiles', []).each do |path|
    if path.start_with?('$snapshot-values/')
      private_source = sources.find { |candidate| candidate['ref'] == 'snapshot-values' }
      check(name == 'openbao' && path == '$snapshot-values/clusters/omv/openbao-values.yml' &&
            private_source && private_source['targetRevision'] == LIVE_REVISION,
            'OpenBao snapshot values must use the protected Live main source')
    else
      check(path.start_with?('$values/') && File.file?(File.join(ROOT, path.delete_prefix('$values/'))),
            "#{name} references a missing Git values file")
    end
  end
end
upstream_dirs = %w[01-bootstrap/monitoring-crds 02-controllers/cert-manager 02-controllers/k8up 02-controllers/longhorn
                   03-core/kube-prometheus-stack 03-core/openbao 04-secrets/vault-secrets-webhook]
check(upstream_dirs.none? { |dir| File.directory?(File.join(ROOT, dir, 'manifests')) },
      'Rendered upstream Helm manifest directories must not be committed')
k8up_sources = apps.fetch('k8up').dig('spec', 'sources')
check(k8up_sources.last['path'] == '02-controllers/k8up' && k8up_sources.last.dig('directory', 'include') == 'pdb.yaml',
      'K8up authored PDB must be the final Argo source')

# Authored resources may use native Git/Kustomize sources. OpenBao API
# configuration belongs to Ansible and must not have an in-cluster Application.
check(!apps.key?('openbao-access-config') && !File.exist?(File.join(ROOT, '04-secrets/openbao-access-config')),
      'Custom OpenBao API polling reconciler must be removed')

# VS Code YAML language server uses per-file, relative schemas in both single-root
# and multi-root workspaces; only active chart values schemas are checked in.
%w[01-bootstrap/monitoring-crds 02-controllers/cert-manager 02-controllers/k8up 02-controllers/longhorn 03-core/kube-prometheus-stack 03-core/openbao].each do |dir|
  values_path = File.join(ROOT, dir, 'values.yml')
  chart = dir == '01-bootstrap/monitoring-crds' ? 'kube-prometheus-stack' : File.basename(dir)
  relative_schema = "../../values-schemas/#{chart}/values.schema.json"
  check(File.readlines(values_path).first&.chomp == "# yaml-language-server: $schema=#{relative_schema}",
        "Missing/mismatched editor schema association: #{dir}/values.yml")
  schema_path = File.expand_path(relative_schema, File.dirname(values_path))
  check(File.file?(schema_path), "Editor schema missing for #{chart}")
  schema = JSON.parse(File.read(schema_path))
  check(schema['$schema'] && (schema['type'] == 'object' || schema['$ref']),
        "Editor schema invalid or not a values schema: #{chart}")
end

all_sources = applications.flat_map do |app|
  spec = app.fetch('spec')
  spec['sources'] || [spec['source']]
end.compact
first_party_url = lambda do |url|
  url&.match?(%r{\A(?:https://github\.com/dmuiX/|git@github\.com:dmuiX/|ssh://git@github\.com/dmuiX/)})
end
check(first_party_url.call('git@github.com:dmuiX/unreviewed.git'),
      'First-party source detection must include SSH-form GitHub URLs')
git_sources = all_sources.select { |candidate| first_party_url.call(candidate['repoURL']) }
infra_sources = git_sources.select { |candidate| candidate['repoURL'] == INFRA_REPOSITORY }
live_sources = git_sources.select { |candidate| candidate['repoURL'] == LIVE_REPOSITORY }
check(git_sources.all? { |candidate| [INFRA_REPOSITORY, LIVE_REPOSITORY].include?(candidate['repoURL']) },
      'Every first-party Git source must use an exact reviewed HTTPS repository identity')
check(!infra_sources.empty? && infra_sources.all? { |candidate| candidate['targetRevision'] == INFRA_REVISION },
      'Every Infra child source must track the main branch')
check(live_sources.length == 9 && live_sources.all? { |candidate| candidate['targetRevision'] == LIVE_REVISION },
      'All nine first-party private Git child sources must track protected Live main')

health_path = '01-bootstrap/argocd-bootstrap/application-health-config.yml'
health = docs(health_path).first
check(File.fnmatch(source.fetch('directory').fetch('include'), health_path, File::FNM_EXTGLOB),
      'Root must discover the bootstrap health configuration')
check(wave(health) == 1, 'Bootstrap health configuration must be in wave 1')
expected_waves = [1, 2, 3, 4, 5, 6, 8]
check(([wave(health)] + applications.map { |app| wave(app) }).uniq.sort == expected_waves,
      'Infra Applications must preserve the complete automated rollout wave sequence')
check(wave(health) <= applications.map { |app| wave(app) }.min,
      'Child health customization must not follow the first child Application')
# Both the health ConfigMap and CRD Application are wave 1. The custom
# health check must be seeded in Argo CD before the first root sync; wave 1
# alone does not order resources within the wave.
health_keys = health.fetch('data').keys
check(health_keys.sort == [
        'resource.customizations.health.argoproj.io_Application',
        'resource.customizations.health.secrets.hashicorp.com_VaultDynamicSecret',
        'resource.customizations.health.secrets.hashicorp.com_VaultStaticSecret',
        'resource.exclusions'
      ].sort,
      'Argo must retain only reviewed health contracts and resource exclusions')
crd_app = apps.fetch('monitoring-crds')
monitoring = apps.fetch('kube-prometheus-stack')
%w[longhorn cert-manager kube-prometheus-stack openbao vault-secrets-webhook].each do |name|
  check(wave(crd_app) < wave(apps.fetch(name)), "Monitoring CRDs must precede #{name}")
end
check(wave(apps.fetch('longhorn')) < wave(monitoring), 'Longhorn must precede monitoring PVCs')
check(wave(apps.fetch('longhorn')) < wave(apps.fetch('openbao')), 'Longhorn must precede OpenBao PVCs')
check(crd_app.dig('spec', 'sources', 0, 'chart') == 'kube-prometheus-stack' &&
      monitoring.dig('spec', 'sources', 0, 'chart') == 'kube-prometheus-stack' &&
      crd_app.dig('spec', 'sources', 0, 'helm', 'valueFiles') == ['$values/01-bootstrap/monitoring-crds/values.yml'] &&
      monitoring.dig('spec', 'sources', 0, 'helm', 'valueFiles') == ['$values/03-core/kube-prometheus-stack/values.yml'],
      'Monitoring phases must render the pinned chart with their separate Git values')
[crd_app, monitoring].each do |app|
  check(app.dig('spec', 'syncPolicy', 'automated', 'prune') == false,
        'CRD ownership handoff must not delete monitoring CRDs')
end
check(wave(apps.fetch('openbao')) < wave(apps.fetch('vault-secrets-webhook')), 'OpenBao must precede its consumer')
check(wave(apps.fetch('argocd-config')) == 1, 'Existing Argo CD server configuration must be wave 1')
expected_apps = %w[argocd-config argocd-route grafana-route kube-prometheus-stack monitoring-crds cert-manager
                   cert-manager-config k8up longhorn longhorn-route openbao openbao-config openbao-pki
                   cloudnative-pg openbao-route provider-vault provider-vault-config radar
                   vault-secrets-operator vault-secrets-webhook]
check(apps.keys.sort == expected_apps.sort,
      'Public root must own the complete steady-state application inventory')
expected_by_wave = {
  1 => %w[argocd-config monitoring-crds],
  2 => %w[cert-manager k8up longhorn],
  3 => %w[openbao provider-vault],
  4 => %w[vault-secrets-operator vault-secrets-webhook],
  5 => %w[argocd-route cert-manager-config longhorn-route openbao-pki openbao-route provider-vault-config],
  6 => %w[cloudnative-pg openbao-config],
  8 => %w[grafana-route kube-prometheus-stack radar]
}
actual_by_wave = applications.group_by { |app| wave(app) }.transform_values do |items|
  items.map { |app| app.dig('metadata', 'name') }.sort
end
check(actual_by_wave == expected_by_wave.transform_values(&:sort),
      'Applications must stay in their wave cohorts; no same-wave ordering is assumed')
check(wave(apps.fetch('cert-manager')) < wave(apps.fetch('openbao-pki')) &&
      wave(apps.fetch('openbao')) < wave(apps.fetch('openbao-pki')),
      'OpenBao PKI must follow cert-manager and OpenBao')
check(wave(apps.fetch('openbao-pki')) < wave(postgresql_app),
      'PostgreSQL must follow the OpenBao PKI phase')
postgres_sources = postgresql_app.dig('spec', 'sources')
check(postgres_sources.count { |entry| entry['chart'] } == 2 &&
      postgres_sources.any? { |entry| entry['chart'] == 'cloudnative-pg' && entry['targetRevision'] == '0.29.1' } &&
      postgres_sources.any? { |entry| entry['chart'] == 'plugin-barman-cloud' && entry['targetRevision'] == '0.8.1' } &&
      postgres_sources.any? { |entry| entry['path'] == '06-data/cloudnative-pg' && entry['ref'] == 'infra' } &&
      postgres_sources.any? { |entry| entry['path'] == '06-data/cloudnative-pg/controllers' } &&
      postgres_sources.any? { |entry| entry['path'] == 'charts/cluster-config' } &&
      postgres_sources.any? { |entry| entry['ref'] == 'private' },
      'PostgreSQL must keep pinned charts and explicit controller/data namespace sources')
{ 'argocd-route' => ['argocd', 5], 'grafana-route' => ['grafana', 8],
  'longhorn-route' => ['longhorn', 5], 'openbao-route' => ['openbao', 5],
  'cert-manager-config' => ['certificates', 5], 'openbao-config' => ['backups', 6] }.each do |name, (component, stage)|
  app = apps.fetch(name)
  chart, private_values = app.dig('spec', 'sources')
  check(wave(app) == stage && chart['path'] == 'charts/cluster-config' &&
        chart.dig('helm', 'parameters', 0) == { 'name' => 'component', 'value' => component } &&
        chart.dig('helm', 'valueFiles') == ['$values/clusters/omv/values.yml'] &&
        chart['targetRevision'] == INFRA_REVISION &&
        private_values['repoURL'] == 'https://github.com/dmuiX/k3s-on-omv-live.git' &&
        private_values['ref'] == 'values', "Wrong private Helm values wiring for #{name}")
end
cert_manager_retry = apps.fetch('cert-manager-config').dig('spec', 'syncPolicy', 'retry')
check(cert_manager_retry == {
        'limit' => -1,
        'backoff' => {'duration' => '10s', 'factor' => 2, 'maxDuration' => '5m'}
      },
      'Certificate configuration must keep retrying its expected create-only OpenBao prerequisite with bounded backoff')
%w[argocd-route grafana-route longhorn-route openbao-route].each do |name|
  check(wave(apps.fetch('cert-manager-config')) <= wave(apps.fetch(name)),
        "Route #{name} must not precede wildcard certificate configuration")
end
check(wave(apps.fetch('openbao')) < wave(apps.fetch('cert-manager-config')) &&
      wave(apps.fetch('vault-secrets-webhook')) < wave(apps.fetch('cert-manager-config')) &&
      wave(apps.fetch('k8up')) < wave(apps.fetch('openbao-config')),
      'Issuer and backup configuration must follow their controllers and the webhook')

crd_values = docs('01-bootstrap/monitoring-crds/values.yml').first
check(crd_values.dig('crds', 'enabled'), 'Bootstrap monitoring CRDs disabled')
%w[alertmanager grafana prometheus prometheusOperator kubeStateMetrics nodeExporter].each do |component|
  check(crd_values.dig(component, 'enabled') == false, "Bootstrap must not deploy #{component}")
end
monitor_values = docs('03-core/kube-prometheus-stack/values.yml').first
check(monitor_values.dig('crds', 'enabled') == false, 'Full monitoring stack must not own CRDs')
check(monitor_values.dig('grafana', 'persistence', 'enabled') == true &&
      monitor_values.dig('grafana', 'persistence', 'storageClassName') == 'longhorn' &&
      monitor_values.dig('grafana', 'persistence', 'size') == '2Gi',
      'Grafana must use the right-sized Longhorn claim')
check(monitor_values.dig('grafana', 'admin').nil?,
      'Grafana must let the chart create its initial random administrator Secret')
grafana_database = monitor_values.dig('grafana', 'grafana.ini', 'database')
check(grafana_database&.dig('ssl_mode') == 'require' &&
      grafana_database['ca_cert_path'].nil? &&
      monitor_values.dig('grafana', 'extraVolumes').nil? &&
      monitor_values.dig('grafana', 'extraVolumeMounts').nil?,
      'Grafana must require encrypted PostgreSQL transport without referencing a cross-namespace Secret')
grafana_secret_ignore = monitoring.fetch('spec').fetch('ignoreDifferences').find do |entry|
  entry['group'] == '' && entry['kind'] == 'Secret' && entry['name'] == 'kube-prometheus-stack-grafana'
end
grafana_volume_ignore = monitoring.fetch('spec').fetch('ignoreDifferences').find do |entry|
  entry['group'] == '' && entry['kind'] == 'PersistentVolumeClaim' &&
    entry['name'] == 'kube-prometheus-stack-grafana'
end
check(grafana_volume_ignore && grafana_volume_ignore['jsonPointers'] == ['/spec/volumeName'],
      'Grafana retained-PV prebinding must ignore only the immutable volumeName')
check(grafana_secret_ignore && grafana_secret_ignore['jsonPointers'].sort ==
      ['/data/admin-password', '/data/admin-user'],
      'Argo must ignore only the chart-generated Grafana administrator data fields')
{ 'prometheus' => ['20Gi', 'longhorn-monitoring'],
  'alertmanager' => ['1Gi', 'longhorn'] }.each do |component, (size, storage_class)|
  spec = component == 'prometheus' ? 'prometheusSpec' : 'alertmanagerSpec'
  field = component == 'prometheus' ? 'storageSpec' : 'storage'
  claim = monitor_values.dig(component, spec, field, 'volumeClaimTemplate', 'spec')
  check(claim['storageClassName'] == storage_class && claim.dig('resources', 'requests', 'storage') == size,
        "#{component} must use its right-sized Longhorn claim")
end
check(monitor_values.dig('prometheus', 'prometheusSpec', 'retention') == '15d' &&
      monitor_values.dig('prometheus', 'prometheusSpec', 'retentionSize') == '18GB',
      'Prometheus retention must fit inside its 20Gi claim')
capacity_alerts = monitor_values.dig('additionalPrometheusRulesMap', 'persistent-volume-capacity', 'groups', 0, 'rules')
check(capacity_alerts&.map { |rule| rule['alert'] } == %w[PlatformPVCUsageWarning PlatformPVCUsageCritical],
      'Platform PVC capacity alerts are missing')
check(monitor_values.dig('prometheusOperator', 'admissionWebhooks', 'certManager', 'enabled') == false,
      'Monitoring must not depend on cert-manager')
%w[serviceMonitor podMonitor].each do |kind|
  check(monitor_values.dig('prometheus', 'prometheusSpec', "#{kind}SelectorNilUsesHelmValues") == false,
        "Prometheus would ignore monitors from other releases: #{kind}")
  check(monitor_values.dig('prometheus', 'prometheusSpec', "#{kind}Selector") == {}, 'Unexpected monitor filter')
  check(monitor_values.dig('prometheus', 'prometheusSpec', "#{kind}NamespaceSelector") == {}, 'Unexpected namespace filter')
end
check(monitor_values.dig('prometheus', 'prometheusSpec', 'ruleSelectorNilUsesHelmValues') == false &&
      monitor_values.dig('prometheus', 'prometheusSpec', 'ruleSelector') == {} &&
      monitor_values.dig('prometheus', 'prometheusSpec', 'ruleNamespaceSelector') == {},
      'Prometheus would ignore PostgreSQL rules from another namespace/release')

longhorn = apps.fetch('longhorn')
check(wave(longhorn) == 2, 'Longhorn must be in the storage bootstrap wave')
check(longhorn.dig('spec', 'destination', 'namespace') == 'longhorn', 'Wrong Longhorn namespace')
check(longhorn.dig('spec', 'syncPolicy', 'syncOptions').include?('CreateNamespace=true'), 'Longhorn namespace is not created')
check(!longhorn.dig('spec', 'syncPolicy', 'syncOptions').include?('ApplyOutOfSyncOnly=true'),
      'Longhorn sync must repair every drifted resource instead of hiding invalid Node contracts')
%w[enforce audit warn].each do |policy|
  check(longhorn.dig('spec', 'syncPolicy', 'managedNamespaceMetadata', 'labels', "pod-security.kubernetes.io/#{policy}") == 'privileged',
        'Longhorn Pod Security labels must target its namespace')
end
check(!longhorn.fetch('spec').key?('labels'), 'Misplaced Application labels')
longhorn_files = Dir.glob(File.join(ROOT, '02-controllers/longhorn', '*.{yml,yaml}'))
check(longhorn_files.map { |f| File.basename(f) }.sort ==
      %w[app.yml monitoring-network-policy.yaml monitoring-storage.yaml node-default-tags.yaml storageclass-configmap.yaml values.yml],
      'Longhorn folder must contain only its Application, values and reviewed storage resources')
longhorn_authored = longhorn_files.flat_map { |file| YAML.load_stream(File.read(file)).compact }
check(longhorn_authored.none? { |resource| %w[HelmRelease HelmRepository].include?(resource['kind']) },
      'Flux leftovers remain')
check(longhorn_authored.none? { |resource| resource['kind'] == 'Job' },
      'Longhorn polling Jobs must not be managed')
check(longhorn_authored.none? do |resource|
  resource['kind'] == 'Node' && resource.fetch('apiVersion', '').start_with?('longhorn.io/')
end, 'Longhorn Node custom resources must not be managed')
check(longhorn_authored.none? do |resource|
  resource.dig('metadata', 'name').to_s.start_with?('longhorn-manager-node-wait')
end, 'Longhorn polling ServiceAccount, RBAC and kubeconfig resources must be removed')
values = docs('02-controllers/longhorn/values.yml').first
check(values.dig('persistence', 'defaultClassReplicaCount') == 3, 'New PVCs must use three Longhorn replicas')
check(values.dig('defaultSettings', 'defaultReplicaCount') == { 'v1' => '3', 'v2' => '3' }, 'UI volume replica defaults differ')
check(values.dig('defaultSettings', 'createDefaultDiskLabeledNodes') == 'false',
      'Longhorn default disk creation must remain enabled for all new nodes')
check(values.dig('persistence', 'reclaimPolicy') == 'Retain', 'Unexpected volume deletion policy')
check(values.dig('persistence', 'defaultClass') == false, 'Do not silently add a second default StorageClass')
check(values.dig('service', 'ui', 'type') == 'ClusterIP', 'Longhorn UI must use the shared Gateway')
check(values.dig('metrics', 'serviceMonitor', 'enabled'), 'Longhorn monitoring missing')
monitoring_nodes = docs('02-controllers/longhorn/node-default-tags.yaml')
node_sync_options = %w[Prune=false Delete=false ServerSideApply=true]
check(monitoring_nodes.map { |resource| resource.dig('metadata', 'name') }.sort == %w[omv wyse5070] &&
      monitoring_nodes.all? do |resource|
        annotations = resource.dig('metadata', 'annotations')
        resource['apiVersion'] == 'v1' && resource['kind'] == 'Node' &&
          resource.keys.sort == %w[apiVersion kind metadata] &&
          annotations['node.longhorn.io/default-node-tags'] == '["monitoring-storage"]' &&
          annotations['argocd.argoproj.io/sync-wave'] == '-1' &&
          annotations['argocd.argoproj.io/sync-options'].split(',').sort == node_sync_options.sort
      end && monitoring_nodes.none? { |resource| resource.dig('metadata', 'name') == 'raspi4' },
      'OMV/Wyse must be metadata-only core Nodes annotated before the Longhorn chart')
monitoring_storage = docs('02-controllers/longhorn/monitoring-storage.yaml')
check(monitoring_storage.length == 1, 'Monitoring storage file must contain only its StorageClass')
monitoring_class = monitoring_storage.first
check(monitoring_class.dig('metadata', 'name') == 'longhorn-monitoring' &&
      monitoring_class.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '3' &&
      monitoring_class.dig('parameters', 'numberOfReplicas') == '2' &&
      monitoring_class.dig('parameters', 'nodeSelector') == 'monitoring-storage' &&
      monitoring_class.dig('parameters', 'encrypted') == 'true' &&
      monitoring_class['reclaimPolicy'] == 'Retain',
      'Prometheus StorageClass must use two encrypted replicas on the selected nodes')
openbao_values = docs('03-core/openbao/values.yml').first
check(openbao_values.dig('server', 'ha', 'enabled') && openbao_values.dig('server', 'ha', 'replicas') == 3,
      'OpenBao Raft must use three server pods')
check(File.read(File.join(ROOT, '03-core/openbao/values.yml'))
          .include?('bootstrap.k3s-on-omv.dev/snapshot-profile: native-r2-v1'),
      'OpenBao values must identify native snapshot Job provenance')
%w[dataStorage auditStorage].each do |storage|
  check(openbao_values.dig('server', storage, 'storageClass') == 'longhorn' &&
        openbao_values.dig('server', storage, 'size') == '1Gi',
        'OpenBao must explicitly use right-sized Longhorn claims')
end
longhorn_sources = longhorn.dig('spec', 'sources')
check(longhorn_sources.any? { |entry| entry['chart'] == 'longhorn' } &&
      longhorn_sources.any? { |entry| entry['ref'] == 'values' } &&
      longhorn_sources.any? { |entry| entry['path'] == '02-controllers/longhorn' &&
        entry['targetRevision'] == INFRA_REVISION &&
        entry.dig('directory', 'include') ==
          '{storageclass-configmap.yaml,node-default-tags.yaml,monitoring-storage.yaml,monitoring-network-policy.yaml}' },
      'Longhorn must combine its pinned chart, Git values, node annotations and storage resources')
puts 'PASS: one public root, multi-source Helm, CRD/storage wave order, selected monitoring storage and three-node OpenBao'
puts applications.sort_by { |app| [wave(app), app.dig('metadata', 'name')] }.map { |app| "  #{wave(app)}: #{app.dig('metadata', 'name')}" }
