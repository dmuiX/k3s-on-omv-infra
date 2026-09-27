#!/usr/bin/env ruby
# Offline desired-state checks; not a proof of host prerequisites or working PVCs.
require 'yaml'
require 'json'

ROOT = File.expand_path('..', __dir__)

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
app_files = Dir.glob(File.join(ROOT, '*', '*app.yml')).select do |path|
  File.fnmatch(source.fetch('directory').fetch('include'), path.delete_prefix(ROOT + '/'), File::FNM_EXTGLOB)
end
applications = app_files.flat_map { |path| YAML.load_stream(File.read(path)).compact }
check(applications.all? { |app| app['kind'] == 'Application' }, 'Root discovers unexpected resources')
apps = applications.to_h { |app| [app.dig('metadata', 'name'), app] }
check(apps.size == applications.size, 'Duplicate Application names')
# Public Applications own all resources; only their real value overrides are private.
app_files.group_by { |path| File.dirname(path) }.each do |dir, paths|
  prefix = File.basename(dir)[/\A\d{2}(?=-)/]
  check(!prefix.nil?, "Component directory needs a wave prefix: #{File.basename(dir)}")
  first_wave = paths.flat_map { |path| YAML.load_stream(File.read(path)).compact }.map { |app| wave(app) }.min
  check(prefix.to_i == first_wave, "Directory prefix disagrees with first wave: #{File.basename(dir)}")
  paths.each do |path|
    check(YAML.load_stream(File.read(path)).compact.length == 1, "Keep later config in config-app.yml: #{path}")
  end
end

applications.each do |app|
  spec = app.fetch('spec')
  sources = spec['sources'] || [spec.fetch('source')]
  sources.each do |s|
    if s['chart']
      check(s.fetch('targetRevision').match?(/\Av?\d+\.\d+\.\d+(?:[-+][\w.-]+)?\z/),
            "Unpinned chart in #{app.dig('metadata', 'name')}")
      s.fetch('helm', {}).fetch('valueFiles', []).each do |path|
        check(path.start_with?('$values/'), 'Helm values must use the Git values source')
        check(File.file?(File.join(ROOT, path.delete_prefix('$values/'))), 'Referenced values file missing')
        check(sources.count { |v| v['ref'] == 'values' } == 1, 'Missing/ambiguous values source')
      end
    elsif s['ref'] == 'values' && s['repoURL'] != source['repoURL']
      check(s['repoURL'] == 'https://github.com/dmuiX/k3s-on-omv-live.git' && !s.key?('path'),
            "Private values source must not render manifests: #{app.dig('metadata', 'name')}")
    else
      check(s['repoURL'] == source['repoURL'], "Git source mismatch: #{app.dig('metadata', 'name')}")
      check(s['targetRevision'] == source['targetRevision'], "Git revision mismatch: #{app.dig('metadata', 'name')}")
    end
  end
end

# VS Code YAML language server uses per-file, relative schemas in both single-root
# and multi-root workspaces; only active chart values schemas are checked in.
%w[01-kube-prometheus-stack 02-cert-manager 02-k8up 02-longhorn 03-openbao].each do |dir|
  values_path = File.join(ROOT, dir, 'values.yml')
  chart = dir.sub(/\A\d{2}-/, '')
  relative_schema = "../values-schemas/#{chart}/values.schema.json"
  check(File.readlines(values_path).first&.chomp == "# yaml-language-server: $schema=#{relative_schema}",
        "Missing/mismatched editor schema association: #{dir}/values.yml")
  schema_path = File.expand_path(relative_schema, File.dirname(values_path))
  check(File.file?(schema_path), "Editor schema missing for #{chart}")
  schema = JSON.parse(File.read(schema_path))
  check(schema['$schema'] && (schema['type'] == 'object' || schema['$ref']),
        "Editor schema invalid or not a values schema: #{chart}")
end

health_path = '01-argocd-bootstrap/application-health-config.yml'
health = docs(health_path).first
check(File.fnmatch(source.fetch('directory').fetch('include'), health_path, File::FNM_EXTGLOB),
      'Root must discover the bootstrap health configuration')
check(wave(health) == 1, 'Bootstrap health configuration must be in wave 1')
check(([wave(health)] + applications.map { |app| wave(app) }).uniq.sort == (1..6).to_a,
      'Infra waves must be consecutive from 1 through 6')
check(wave(health) <= applications.map { |app| wave(app) }.min,
      'Child health customization must not follow the first child Application')
# Both the health ConfigMap and monitoring Application are wave 1. The custom
# health check must be seeded in Argo CD before the first root sync; wave 1
# alone does not order resources within the wave.
check(health.fetch('data').key?('resource.customizations.health.argoproj.io_Application'),
      'Root cannot wait for child Application health')
monitoring = apps.fetch('kube-prometheus-stack')
%w[longhorn cert-manager openbao vault-secrets-webhook].each do |name|
  check(wave(monitoring) < wave(apps.fetch(name)), "Monitoring CRDs must precede #{name}")
end
check(wave(apps.fetch('longhorn')) < wave(apps.fetch('openbao')), 'Longhorn must precede OpenBao PVCs')
check(wave(apps.fetch('openbao')) < wave(apps.fetch('vault-secrets-webhook')), 'OpenBao must precede its consumer')
check(wave(apps.fetch('argocd-config')) == 1, 'Existing Argo CD server configuration must be wave 1')
expected = %w[argocd-config argocd-route grafana-route kube-prometheus-stack cert-manager cert-manager-config
              k8up longhorn longhorn-route openbao openbao-config openbao-route vault-secrets-webhook]
check(apps.keys.sort == expected.sort, 'One public root must own all child Applications')
{ 'argocd-route' => ['argocd', 6], 'grafana-route' => ['grafana', 6],
  'longhorn-route' => ['longhorn', 6], 'openbao-route' => ['openbao', 6],
  'cert-manager-config' => ['certificates', 5], 'openbao-config' => ['backups', 6] }.each do |name, (component, stage)|
  app = apps.fetch(name)
  chart, private_values = app.dig('spec', 'sources')
  check(wave(app) == stage && chart['path'] == 'charts/cluster-config' &&
        chart.dig('helm', 'parameters', 0) == { 'name' => 'component', 'value' => component } &&
        chart.dig('helm', 'valueFiles') == ['$values/clusters/omv/values.yml'] &&
        private_values['ref'] == 'values', "Wrong private values wiring for #{name}")
end
%w[argocd-route grafana-route longhorn-route openbao-route].each do |name|
  check(wave(apps.fetch('cert-manager-config')) < wave(apps.fetch(name)),
        "Route #{name} must follow wildcard certificate configuration")
end
check(wave(apps.fetch('openbao')) < wave(apps.fetch('cert-manager-config')) &&
      wave(apps.fetch('vault-secrets-webhook')) < wave(apps.fetch('cert-manager-config')) &&
      wave(apps.fetch('k8up')) < wave(apps.fetch('openbao-config')),
      'Issuer and backup configuration must follow their controllers and the webhook')

monitor_values = docs('01-kube-prometheus-stack/values.yml').first
check(monitor_values.dig('crds', 'enabled'), 'Monitoring CRDs disabled')
check(monitor_values.dig('grafana', 'persistence', 'enabled') == false, 'Early Grafana cannot depend on storage')
check(monitor_values.dig('prometheus', 'prometheusSpec', 'storageSpec') == {}, 'Early Prometheus cannot depend on storage')
check(monitor_values.dig('alertmanager', 'alertmanagerSpec', 'storage') == {}, 'Early Alertmanager cannot depend on storage')
check(monitor_values.dig('prometheusOperator', 'admissionWebhooks', 'certManager', 'enabled') == false,
      'Early monitoring cannot depend on cert-manager')
%w[serviceMonitor podMonitor].each do |kind|
  check(monitor_values.dig('prometheus', 'prometheusSpec', "#{kind}SelectorNilUsesHelmValues") == false,
        "Prometheus would ignore monitors from other releases: #{kind}")
  check(monitor_values.dig('prometheus', 'prometheusSpec', "#{kind}Selector") == {}, 'Unexpected monitor filter')
  check(monitor_values.dig('prometheus', 'prometheusSpec', "#{kind}NamespaceSelector") == {}, 'Unexpected namespace filter')
end

longhorn = apps.fetch('longhorn')
check(wave(longhorn) == 2, 'Longhorn must be in the storage bootstrap wave')
check(longhorn.dig('spec', 'destination', 'namespace') == 'longhorn', 'Wrong Longhorn namespace')
check(longhorn.dig('spec', 'syncPolicy', 'syncOptions').include?('CreateNamespace=true'), 'Longhorn namespace is not created')
%w[enforce audit warn].each do |policy|
  check(longhorn.dig('spec', 'syncPolicy', 'managedNamespaceMetadata', 'labels', "pod-security.kubernetes.io/#{policy}") == 'privileged',
        'Longhorn Pod Security labels must target its namespace')
end
check(!longhorn.fetch('spec').key?('labels'), 'Misplaced Application labels')
longhorn_files = Dir.glob(File.join(ROOT, '02-longhorn', '*.{yml,yaml}'))
check(longhorn_files.map { |f| File.basename(f) }.sort == %w[app.yml values.yml],
      'Reusable Longhorn controller must not contain real route manifests')
check(longhorn_files.none? do |f|
  YAML.load_stream(File.read(f)).compact.any? { |d| %w[HelmRelease HelmRepository].include?(d['kind']) }
end, 'Flux leftovers remain')
values = docs('02-longhorn/values.yml').first
check(values.dig('persistence', 'defaultClassReplicaCount') == 1, 'Single-node PVC replica count must be 1')
check(values.dig('defaultSettings', 'defaultReplicaCount') == { 'v1' => '1', 'v2' => '1' }, 'UI volume replica defaults differ')
check(values.dig('persistence', 'reclaimPolicy') == 'Retain', 'Unexpected volume deletion policy')
check(values.dig('persistence', 'defaultClass') == false, 'Do not silently add a second default StorageClass')
check(values.dig('service', 'ui', 'type') == 'ClusterIP', 'Longhorn UI must use the shared Gateway')
check(values.dig('metrics', 'serviceMonitor', 'enabled'), 'Longhorn monitoring missing')
openbao_values = docs('03-openbao/values.yml').first
check(openbao_values.dig('server', 'ha', 'enabled') && openbao_values.dig('server', 'ha', 'replicas') == 1,
      'First-phase OpenBao Raft must use one server pod')
%w[dataStorage auditStorage].each do |storage|
  check(openbao_values.dig('server', storage, 'storageClass') == 'longhorn',
        'OpenBao must explicitly opt into Longhorn')
end
check(longhorn.dig('spec', 'sources').any? { |s| s['ref'] == 'values' && !s.key?('path') },
      'Public chart Application must not render a cluster-specific route')
puts 'PASS: one public root, private values wiring, wave order, editor schemas, and single-node Longhorn'
puts applications.sort_by { |app| [wave(app), app.dig('metadata', 'name')] }.map { |app| "  #{wave(app)}: #{app.dig('metadata', 'name')}" }
