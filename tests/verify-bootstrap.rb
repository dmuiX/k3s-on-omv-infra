#!/usr/bin/env ruby
# Offline desired-state checks; not a proof of host prerequisites or working PVCs.
require 'yaml'
require 'json'
require 'pathname'

ROOT = File.expand_path('..', __dir__)
INFRA_REVISION = '0f3a9a03d3747798093d6de84fe9bedf0176b9a9'
LIVE_REVISION = 'ce6ad756dd48ef28145f836e6825a65fcafe548f'
POSTGRES_REVISION = '6ca730a268c1a857893672013f4425222dbd9f4c'
OPENBAO_REVISION = '712c40098802fc761bec45ed3704defc977ba595'
LONGHORN_REVISION = 'cceca6288815a2a864a7f3fc3280285ce2dc45c2'
POSTGRES_LIVE_REVISION = '3e2ae87315f679fbb6ffc0be2342a74a43d213a6'
OPENBAO_LIVE_REVISION = 'da4b8dabdf4983933f9beb47e36ddebea389045f'

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

pki_path = '05-platform/openbao-pki/application.yml'
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
check(pki_app.dig('spec', 'source', 'path') == '05-platform/openbao-pki/workload' &&
      pki_app.dig('spec', 'source', 'targetRevision').to_s.match?(/\A[0-9a-f]{40}\z/),
      'Staged OpenBao PKI workload must remain a single immutable source')
cert_manager_sources = apps.fetch('cert-manager').dig('spec', 'sources')
cert_manager_network_source = cert_manager_sources.find do |candidate|
  candidate['path'] == '02-controllers/cert-manager/network-policy'
end
cert_manager_private_source = cert_manager_sources.find { |candidate| candidate['ref'] == 'private' }
check(cert_manager_network_source && cert_manager_private_source &&
      cert_manager_network_source.dig('helm', 'valueFiles') == ['$private/clusters/omv/values.yml'] &&
      cert_manager_network_source['targetRevision'].to_s.match?(/\A[0-9a-f]{40}\z/) &&
      cert_manager_private_source['targetRevision'].to_s.match?(/\A[0-9a-f]{40}\z/) &&
      !cert_manager_private_source.key?('path'),
      'cert-manager must own its immutable private-endpoint NetworkPolicy source')
check(postgresql_app.fetch('spec').fetch('sources').all? do |candidate|
        !candidate['repoURL']&.start_with?('https://github.com/dmuiX/') ||
          candidate['targetRevision'].to_s.match?(/\A[0-9a-f]{40}\z/)
      end, 'Staged PostgreSQL Git sources must remain immutably pinned')
pki_activated_applications = applications + [pki_app]
pki_activated_apps = pki_activated_applications.to_h { |app| [app.dig('metadata', 'name'), app] }
fully_activated_applications = pki_activated_applications + [postgresql_app]
fully_activated_apps = fully_activated_applications.to_h { |app| [app.dig('metadata', 'name'), app] }
check(fully_activated_apps.size == fully_activated_applications.size,
      'Staged activation must not introduce a duplicate Application name')
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
  allowed_values_revisions = case name
                             when 'kube-prometheus-stack' then [INFRA_REVISION, POSTGRES_REVISION]
                             when 'openbao' then [INFRA_REVISION, OPENBAO_REVISION]
                             when 'cert-manager' then [INFRA_REVISION, cert_manager_network_source['targetRevision']]
                             when 'longhorn' then [INFRA_REVISION, LONGHORN_REVISION]
                             else [INFRA_REVISION]
                             end
  check(values_source && values_source['repoURL'] == source['repoURL'] &&
        allowed_values_revisions.include?(values_source['targetRevision']),
        "#{name} values source is not the pinned reviewed Git revision")
  chart.fetch('helm', {}).fetch('valueFiles', []).each do |path|
    if path.start_with?('$snapshot-values/')
      private_source = sources.find { |candidate| candidate['ref'] == 'snapshot-values' }
      check(name == 'openbao' && path == '$snapshot-values/clusters/omv/openbao-values.yml' &&
            private_source && private_source['targetRevision'] == OPENBAO_LIVE_REVISION,
            'OpenBao snapshot values must use the dedicated immutable private source')
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

git_sources = fully_activated_applications.flat_map do |app|
  spec = app.fetch('spec')
  spec['sources'] || [spec['source']]
end.compact.select { |candidate| candidate['repoURL']&.start_with?('https://github.com/dmuiX/') }
check(git_sources.select { |candidate| candidate['repoURL'].end_with?('k3s-on-omv-infra.git') }
                 .map { |candidate| candidate['targetRevision'] }.uniq.all? do |revision|
        system('git', '-C', ROOT, 'cat-file', '-e', "#{revision}^{commit}",
               out: File::NULL, err: File::NULL)
      end, 'Every Infra child pin must resolve to a local reviewed commit object')
check(git_sources.all? do |candidate|
  allowed = if candidate['repoURL'].end_with?('k3s-on-omv-infra.git')
              [INFRA_REVISION, POSTGRES_REVISION, OPENBAO_REVISION,
               pki_app.dig('spec', 'source', 'targetRevision'),
               cert_manager_network_source['targetRevision'], LONGHORN_REVISION]
            else
              [LIVE_REVISION, POSTGRES_LIVE_REVISION, OPENBAO_LIVE_REVISION,
               cert_manager_private_source['targetRevision']]
            end
  allowed.include?(candidate['targetRevision'])
end, 'Every owned Git child source must use its reviewed immutable revision')

health_path = '01-bootstrap/argocd-bootstrap/application-health-config.yml'
health = docs(health_path).first
check(File.fnmatch(source.fetch('directory').fetch('include'), health_path, File::FNM_EXTGLOB),
      'Root must discover the bootstrap health configuration')
check(wave(health) == 1, 'Bootstrap health configuration must be in wave 1')
expected_waves = [1, 2, 3, 4, 5, 6]
check(([wave(health)] + applications.map { |app| wave(app) }).uniq.sort == expected_waves,
      'Infra Applications must use the implemented wave folders; later waves are reserved for future apps')
check(wave(health) <= applications.map { |app| wave(app) }.min,
      'Child health customization must not follow the first child Application')
# Both the health ConfigMap and CRD Application are wave 1. The custom
# health check must be seeded in Argo CD before the first root sync; wave 1
# alone does not order resources within the wave.
health_keys = health.fetch('data').keys
check(health_keys.include?('resource.customizations.health.argoproj.io_Application'),
      'Root cannot wait for child Application health')
check(health_keys.include?('resource.customizations.health.postgres-operator.crunchydata.com_PostgresCluster'),
      'Missing Argo health gate for Crunchy PostgresCluster')
check(health_keys.none? { |key| key.include?('cnpg.io') },
      'CloudNativePG/Barman health gates must be retired')
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
expected_pki_activated = expected_default + %w[openbao-pki]
check(pki_activated_apps.keys.sort == expected_pki_activated.sort &&
      !pki_activated_apps.key?('postgresql'),
      'OpenBao PKI activation must not implicitly activate PostgreSQL')
expected_fully_activated = expected_pki_activated + %w[postgresql]
check(fully_activated_apps.keys.sort == expected_fully_activated.sort,
      'PostgreSQL must remain available only through its independent activation gate')
expected_by_wave = {
  1 => %w[argocd-config monitoring-crds],
  2 => %w[cert-manager k8up longhorn],
  3 => %w[kube-prometheus-stack openbao],
  4 => %w[openbao-access-config vault-secrets-webhook],
  5 => %w[argocd-route cert-manager-config grafana-route longhorn-route openbao-pki openbao-route],
  6 => %w[openbao-config postgresql]
}
actual_by_wave = fully_activated_applications.group_by { |app| wave(app) }.transform_values do |items|
  items.map { |app| app.dig('metadata', 'name') }.sort
end
check(actual_by_wave == expected_by_wave.transform_values(&:sort),
      'Activated Applications must stay in their independent wave cohorts; no same-wave ordering is assumed')
check(wave(pki_activated_apps.fetch('openbao-access-config')) < wave(pki_activated_apps.fetch('openbao-pki')) &&
      wave(pki_activated_apps.fetch('cert-manager')) < wave(pki_activated_apps.fetch('openbao-pki')) &&
      wave(pki_activated_apps.fetch('openbao')) < wave(pki_activated_apps.fetch('openbao-pki')),
      'OpenBao PKI activation must follow its controller, OpenBao and access bootstrap phases')
check(wave(fully_activated_apps.fetch('openbao-pki')) < wave(fully_activated_apps.fetch('postgresql')),
      'Separately activated PostgreSQL must follow the mandatory OpenBao PKI phase')
postgres_sources = fully_activated_apps.fetch('postgresql').dig('spec', 'sources')
check(postgres_sources.count { |entry| entry['chart'] } == 1 &&
      postgres_sources.any? { |entry| entry['chart'] == 'pgo' && entry['targetRevision'] == '6.0.3' } &&
      postgres_sources.any? { |entry| entry['path'] == '06-data/postgresql' && entry['ref'] == 'infra' } &&
      postgres_sources.any? { |entry| entry['path'] == 'charts/cluster-config' } &&
      postgres_sources.any? { |entry| entry['ref'] == 'private' },
      'PostgreSQL must remain one pinned multi-source Application')
{ 'argocd-route' => ['argocd', 5], 'grafana-route' => ['grafana', 5],
  'longhorn-route' => ['longhorn', 5], 'openbao-route' => ['openbao', 5],
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
      %w[app.yml monitoring-storage.yaml storageclass-configmap.yaml values.yml wait-for-manager-nodes.yaml],
      'Longhorn folder must contain only its Application, values and reviewed storage resources')
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
monitoring_storage = docs('02-controllers/longhorn/monitoring-storage.yaml')
monitoring_nodes = monitoring_storage.select { |resource| resource['kind'] == 'Node' }
check(monitoring_nodes.map { |resource| resource.dig('metadata', 'name') }.sort == %w[omv wyse5070] &&
      monitoring_nodes.all? do |resource|
        resource.dig('spec', 'name') == resource.dig('metadata', 'name') &&
          resource.dig('spec', 'tags') == ['monitoring-storage'] &&
          resource.dig('spec').keys.sort == %w[name tags] &&
          resource.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '2'
      end && monitoring_nodes.none? { |resource| resource.dig('metadata', 'name') == 'raspi4' },
      'Prometheus storage Nodes must adopt manager-created disks only after the wait hook')
manager_wait = docs('02-controllers/longhorn/wait-for-manager-nodes.yaml')
wait_job = manager_wait.find { |resource| resource['kind'] == 'Job' }
wait_role = manager_wait.find { |resource| resource['kind'] == 'Role' }
wait_account = manager_wait.find { |resource| resource['kind'] == 'ServiceAccount' }
wait_config = manager_wait.find do |resource|
  resource['kind'] == 'ConfigMap' && resource.dig('metadata', 'name') == 'longhorn-manager-node-wait-kubeconfig'
end
wait_pod = wait_job.dig('spec', 'template', 'spec')
wait_container = wait_pod.dig('containers', 0)
kubeconfig = wait_config.dig('data', 'kubeconfig')
api_projection = wait_pod.fetch('volumes').find { |volume| volume['name'] == 'kube-api-access' }
projected_sources = api_projection.dig('projected', 'sources')
check(wait_job.dig('metadata', 'annotations', 'argocd.argoproj.io/hook') == 'Sync' &&
      wait_job.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '1' &&
      wait_job.dig('spec', 'backoffLimit') == 0 &&
      wait_job.dig('spec', 'activeDeadlineSeconds') == 660 &&
      wait_account['automountServiceAccountToken'] == false &&
      wait_pod['automountServiceAccountToken'] == false &&
      wait_container['image'] == 'rancher/k3s:v1.37.1-k3s1@sha256:ca7f37d993d82ef0dcdcfecb2e0e2618ea541dbaffc620c8cedebe01a82acd0d' &&
      kubeconfig.include?('server: https://kubernetes.default.svc.cluster.local') &&
      kubeconfig.include?('certificate-authority: /run/kube-api/ca.crt') &&
      kubeconfig.include?('tokenFile: /run/kube-api/token') &&
      projected_sources.any? { |source| source.dig('serviceAccountToken', 'path') == 'token' } &&
      projected_sources.any? { |source| source.dig('configMap', 'name') == 'kube-root-ca.crt' } &&
      wait_container.fetch('args', []).first.include?('--kubeconfig=/run/kube-api/kubeconfig') &&
      wait_role.dig('rules', 0) == {'apiGroups' => ['longhorn.io'], 'resources' => ['nodes'], 'verbs' => ['get']} &&
      wait_container.dig('args', 0).include?('omv wyse5070'),
      'A bounded authenticated least-privilege wave-1 hook must wait for manager-owned OMV/Wyse disks')
monitoring_class = monitoring_storage.find { |resource| resource['kind'] == 'StorageClass' }
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
%w[dataStorage auditStorage].each do |storage|
  check(openbao_values.dig('server', storage, 'storageClass') == 'longhorn' &&
        openbao_values.dig('server', storage, 'size') == '1Gi',
        'OpenBao must explicitly use right-sized Longhorn claims')
end
longhorn_sources = longhorn.dig('spec', 'sources')
check(longhorn_sources.any? { |entry| entry['chart'] == 'longhorn' } &&
      longhorn_sources.any? { |entry| entry['ref'] == 'values' } &&
      longhorn_sources.any? { |entry| entry['path'] == '02-controllers/longhorn' &&
        entry.dig('directory', 'include') == '{storageclass-configmap.yaml,wait-for-manager-nodes.yaml,monitoring-storage.yaml}' },
      'Longhorn must combine its pinned chart, Git values and reviewed storage resources')
puts 'PASS: one public root, multi-source Helm, CRD/storage wave order, selected monitoring storage and three-node OpenBao'
puts applications.sort_by { |app| [wave(app), app.dig('metadata', 'name')] }.map { |app| "  #{wave(app)}: #{app.dig('metadata', 'name')}" }
