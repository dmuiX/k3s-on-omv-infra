#!/usr/bin/env ruby
# Offline desired-state checks; not a proof of host prerequisites or working PVCs.
require 'yaml'
require 'json'
require 'pathname'

ROOT = File.expand_path('..', __dir__)
INFRA_REVISION = 'a88b95d54326ccad6d94de2e1c8f76b457e32f76'
LIVE_REVISION = 'ce6ad756dd48ef28145f836e6825a65fcafe548f'
POSTGRES_REVISION = INFRA_REVISION
POSTGRES_LIVE_REVISION = '3e2ae87315f679fbb6ffc0be2342a74a43d213a6'

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
directory = source.fetch('directory')
include_pattern = directory.fetch('include')
exclude_pattern = directory.fetch('exclude')
root_selects = lambda do |path|
  File.fnmatch(include_pattern, path, File::FNM_EXTGLOB) &&
    !File.fnmatch(exclude_pattern, path, File::FNM_EXTGLOB)
end
app_files = Dir.glob(File.join(ROOT, '*', '*', '{*app.yml,application.yml}'), File::FNM_EXTGLOB).select do |path|
  root_selects.call(path.delete_prefix(ROOT + '/'))
end
applications = app_files.flat_map { |path| YAML.load_stream(File.read(path)).compact }
check(applications.all? { |app| app['kind'] == 'Application' }, 'Root discovers unexpected resources')
apps = applications.to_h { |app| [app.dig('metadata', 'name'), app] }
check(apps.size == applications.size, 'Duplicate Application names')

pki_path = '05-pki/openbao-pki/application.yml'
postgresql_path = '06-data/postgresql/app.yml'
check(File.fnmatch(include_pattern, pki_path, File::FNM_EXTGLOB),
      'Root include must explicitly stage the mandatory OpenBao PKI Application')
check(File.fnmatch(exclude_pattern, pki_path, File::FNM_EXTGLOB) &&
      File.fnmatch(exclude_pattern, postgresql_path, File::FNM_EXTGLOB),
      'Root defaults must gate OpenBao PKI and PostgreSQL until guarded activation')
check(!root_selects.call(pki_path) && !root_selects.call(postgresql_path),
      'Staged platform Applications must remain inactive before their bootstrap gates pass')
pki_app = docs(pki_path).first
postgresql_app = docs(postgresql_path).first
check(pki_app.dig('spec', 'source', 'targetRevision').to_s.match?(/\A[0-9a-f]{40}\z/),
      'Staged OpenBao PKI workload must remain immutably pinned')
check(postgresql_app.fetch('spec').fetch('sources').all? do |candidate|
        !candidate['repoURL']&.start_with?('https://github.com/dmuiX/') ||
          candidate['targetRevision'].to_s.match?(/\A[0-9a-f]{40}\z/)
      end, 'Staged PostgreSQL Git sources must remain immutably pinned')
activated_applications = applications + [pki_app, postgresql_app]
activated_apps = activated_applications.to_h { |app| [app.dig('metadata', 'name'), app] }
check(activated_apps.size == activated_applications.size,
      'Bootstrap activation must not introduce a duplicate Application name')
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
  allowed_values_revisions = name == 'kube-prometheus-stack' ? [INFRA_REVISION, POSTGRES_REVISION] : [INFRA_REVISION]
  check(values_source && values_source['repoURL'] == source['repoURL'] &&
        allowed_values_revisions.include?(values_source['targetRevision']),
        "#{name} values source is not the pinned reviewed Git revision")
  chart.fetch('helm', {}).fetch('valueFiles', []).each do |path|
    check(path.start_with?('$values/') && File.file?(File.join(ROOT, path.delete_prefix('$values/'))),
          "#{name} references a missing Git values file")
  end
end
upstream_dirs = %w[01-bootstrap/monitoring-crds 02-controllers/cert-manager 02-controllers/k8up 02-controllers/longhorn
                   03-core/kube-prometheus-stack 03-core/openbao 04-secrets/vault-secrets-webhook]
check(upstream_dirs.none? { |dir| File.directory?(File.join(ROOT, dir, 'manifests')) },
      'Rendered upstream Helm manifest directories must not be committed')
k8up_sources = apps.fetch('k8up').dig('spec', 'sources')
check(k8up_sources.last['path'] == '02-controllers/k8up' && k8up_sources.last.dig('directory', 'include') == 'pdb.yaml',
      'K8up authored PDB must be the final Argo source')

# Authored resources may use native Git/Kustomize sources.
check(apps.fetch('openbao-access-config').dig('spec', 'source', 'path') == '04-secrets/openbao-access-config/workload',
      'OpenBao access configuration must render its authored Kustomize source')

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

git_sources = activated_applications.flat_map do |app|
  spec = app.fetch('spec')
  spec['sources'] || [spec['source']]
end.compact.select { |candidate| candidate['repoURL']&.start_with?('https://github.com/dmuiX/') }
check(git_sources.all? do |candidate|
  allowed = if candidate['repoURL'].end_with?('k3s-on-omv-infra.git')
              [INFRA_REVISION, POSTGRES_REVISION, pki_app.dig('spec', 'source', 'targetRevision')]
            else
              [LIVE_REVISION, POSTGRES_LIVE_REVISION]
            end
  allowed.include?(candidate['targetRevision'])
end, 'Every owned Git child source must use its reviewed immutable revision')

health_path = '01-bootstrap/argocd-bootstrap/application-health-config.yml'
health = docs(health_path).first
check(File.fnmatch(source.fetch('directory').fetch('include'), health_path, File::FNM_EXTGLOB),
      'Root must discover the bootstrap health configuration')
check(wave(health) == 1, 'Bootstrap health configuration must be in wave 1')
expected_waves = [1, 2, 3, 4, 5, 6, 8]
check(([wave(health)] + applications.map { |app| wave(app) }).uniq.sort == expected_waves,
      'Infra Applications must use the implemented wave folders; wave 7 is reserved for future apps')
check(wave(health) <= applications.map { |app| wave(app) }.min,
      'Child health customization must not follow the first child Application')
# Both the health ConfigMap and CRD Application are wave 1. The custom
# health check must be seeded in Argo CD before the first root sync; wave 1
# alone does not order resources within the wave.
health_keys = health.fetch('data').keys
check(health_keys.include?('resource.customizations.health.argoproj.io_Application'),
      'Root cannot wait for child Application health')
%w[Cluster DatabaseRole Database ScheduledBackup Backup].each do |kind|
  check(health_keys.include?("resource.customizations.health.postgresql.cnpg.io_#{kind}"),
        "Missing Argo health gate for CloudNativePG #{kind}")
end
check(health_keys.include?('resource.customizations.health.barmancloud.cnpg.io_ObjectStore'),
      'Missing Argo health gate for the Barman ObjectStore')
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
check(wave(apps.fetch('openbao')) < wave(apps.fetch('openbao-access-config')) &&
      wave(apps.fetch('openbao-access-config')) < wave(apps.fetch('cert-manager-config')) &&
      wave(apps.fetch('openbao-access-config')) < wave(apps.fetch('openbao-config')),
      'OpenBao ACL reconciliation must precede webhook-backed Secrets')
check(wave(apps.fetch('argocd-config')) == 1, 'Existing Argo CD server configuration must be wave 1')
expected_default = %w[argocd-config argocd-route grafana-route kube-prometheus-stack monitoring-crds cert-manager
                      cert-manager-config k8up longhorn longhorn-route openbao openbao-access-config openbao-config
                      openbao-route vault-secrets-webhook]
check(apps.keys.sort == expected_default.sort,
      'Default public root must own regular Applications and keep staged platform phases inactive')
expected_activated = expected_default + %w[openbao-pki postgresql]
check(activated_apps.keys.sort == expected_activated.sort,
      'GitOps bootstrap activation must add mandatory OpenBao PKI and PostgreSQL Applications')
expected_by_wave = {
  1 => %w[argocd-config monitoring-crds],
  2 => %w[cert-manager k8up longhorn],
  3 => %w[kube-prometheus-stack openbao],
  4 => %w[openbao-access-config vault-secrets-webhook],
  5 => %w[cert-manager-config openbao-pki],
  6 => %w[openbao-config postgresql],
  8 => %w[argocd-route grafana-route longhorn-route openbao-route]
}
actual_by_wave = activated_applications.group_by { |app| wave(app) }.transform_values do |items|
  items.map { |app| app.dig('metadata', 'name') }.sort
end
check(actual_by_wave == expected_by_wave.transform_values(&:sort),
      'Activated Applications must stay in their independent wave cohorts; no same-wave ordering is assumed')
check(wave(activated_apps.fetch('openbao-access-config')) < wave(activated_apps.fetch('openbao-pki')) &&
      wave(activated_apps.fetch('cert-manager')) < wave(activated_apps.fetch('openbao-pki')) &&
      wave(activated_apps.fetch('openbao')) < wave(activated_apps.fetch('openbao-pki')),
      'OpenBao PKI activation must follow its controller, OpenBao and access bootstrap phases')
check(wave(activated_apps.fetch('openbao-pki')) < wave(activated_apps.fetch('postgresql')),
      'PostgreSQL must follow the mandatory OpenBao PKI phase')
postgres_sources = activated_apps.fetch('postgresql').dig('spec', 'sources')
check(postgres_sources.count { |entry| entry['chart'] } == 2 &&
      postgres_sources.any? { |entry| entry['chart'] == 'cloudnative-pg' && entry['targetRevision'] == '0.29.1' } &&
      postgres_sources.any? { |entry| entry['chart'] == 'plugin-barman-cloud' && entry['targetRevision'] == '0.8.1' } &&
      postgres_sources.any? { |entry| entry['path'] == '06-data/postgresql' && entry['ref'] == 'infra' } &&
      postgres_sources.any? { |entry| entry['path'] == 'charts/cluster-config' } &&
      postgres_sources.any? { |entry| entry['ref'] == 'private' },
      'PostgreSQL must remain one pinned multi-source Application')
{ 'argocd-route' => ['argocd', 8], 'grafana-route' => ['grafana', 8],
  'longhorn-route' => ['longhorn', 8], 'openbao-route' => ['openbao', 8],
  'cert-manager-config' => ['certificates', 5], 'openbao-config' => ['backups', 6] }.each do |name, (component, stage)|
  app = apps.fetch(name)
  chart, private_values = app.dig('spec', 'sources')
  check(wave(app) == stage && chart['path'] == 'charts/cluster-config' &&
        chart.dig('helm', 'parameters', 0) == { 'name' => 'component', 'value' => component } &&
        chart.dig('helm', 'valueFiles') == ['$values/clusters/omv/values.yml'] &&
        private_values['repoURL'] == 'https://github.com/dmuiX/k3s-on-omv-live.git' &&
        private_values['ref'] == 'values', "Wrong private Helm values wiring for #{name}")
end
%w[argocd-route grafana-route longhorn-route openbao-route].each do |name|
  check(wave(apps.fetch('cert-manager-config')) < wave(apps.fetch(name)),
        "Route #{name} must follow wildcard certificate configuration")
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
      monitor_values.dig('grafana', 'persistence', 'storageClassName') == 'longhorn',
      'Grafana must persist on Longhorn')
check(monitor_values.dig('grafana', 'admin').nil?,
      'Grafana must let the chart create its initial random administrator Secret')
grafana_secret_ignore = monitoring.fetch('spec').fetch('ignoreDifferences').find do |entry|
  entry['group'] == '' && entry['kind'] == 'Secret' && entry['name'] == 'kube-prometheus-stack-grafana'
end
check(grafana_secret_ignore && grafana_secret_ignore['jsonPointers'].sort ==
      ['/data/admin-password', '/data/admin-user'],
      'Argo must ignore only the chart-generated Grafana administrator data fields')
%w[prometheus alertmanager].each do |component|
  spec = component == 'prometheus' ? 'prometheusSpec' : 'alertmanagerSpec'
  field = component == 'prometheus' ? 'storageSpec' : 'storage'
  check(monitor_values.dig(component, spec, field, 'volumeClaimTemplate', 'spec', 'storageClassName') == 'longhorn',
        "#{component} must persist on Longhorn")
end
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
%w[enforce audit warn].each do |policy|
  check(longhorn.dig('spec', 'syncPolicy', 'managedNamespaceMetadata', 'labels', "pod-security.kubernetes.io/#{policy}") == 'privileged',
        'Longhorn Pod Security labels must target its namespace')
end
check(!longhorn.fetch('spec').key?('labels'), 'Misplaced Application labels')
longhorn_files = Dir.glob(File.join(ROOT, '02-controllers/longhorn', '*.{yml,yaml}'))
check(longhorn_files.map { |f| File.basename(f) }.sort == %w[app.yml storageclass-configmap.yaml values.yml],
      'Longhorn folder must contain only its Application, values and encrypted default-class ConfigMap')
check(longhorn_files.none? do |f|
  YAML.load_stream(File.read(f)).compact.any? { |d| %w[HelmRelease HelmRepository].include?(d['kind']) }
end, 'Flux leftovers remain')
values = docs('02-controllers/longhorn/values.yml').first
check(values.dig('persistence', 'defaultClassReplicaCount') == 3, 'New PVCs must use three Longhorn replicas')
check(values.dig('defaultSettings', 'defaultReplicaCount') == { 'v1' => '3', 'v2' => '3' }, 'UI volume replica defaults differ')
check(values.dig('persistence', 'reclaimPolicy') == 'Retain', 'Unexpected volume deletion policy')
check(values.dig('persistence', 'defaultClass') == false, 'Do not silently add a second default StorageClass')
check(values.dig('service', 'ui', 'type') == 'ClusterIP', 'Longhorn UI must use the shared Gateway')
check(values.dig('metrics', 'serviceMonitor', 'enabled'), 'Longhorn monitoring missing')
openbao_values = docs('03-core/openbao/values.yml').first
check(openbao_values.dig('server', 'ha', 'enabled') && openbao_values.dig('server', 'ha', 'replicas') == 3,
      'OpenBao Raft must use three server pods')
%w[dataStorage auditStorage].each do |storage|
  check(openbao_values.dig('server', storage, 'storageClass') == 'longhorn',
        'OpenBao must explicitly opt into Longhorn')
end
longhorn_sources = longhorn.dig('spec', 'sources')
check(longhorn_sources.any? { |entry| entry['chart'] == 'longhorn' } &&
      longhorn_sources.any? { |entry| entry['ref'] == 'values' } &&
      longhorn_sources.any? { |entry| entry['path'] == '02-controllers/longhorn' && entry.dig('directory', 'include') == 'storageclass-configmap.yaml' },
      'Longhorn must combine its pinned chart, Git values and encrypted StorageClass override')
puts 'PASS: one public root, multi-source Helm, CRD/storage wave order, three-node Longhorn and OpenBao'
puts applications.sort_by { |app| [wave(app), app.dig('metadata', 'name')] }.map { |app| "  #{wave(app)}: #{app.dig('metadata', 'name')}" }
