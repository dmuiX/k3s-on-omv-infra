#!/usr/bin/env ruby
# Optional online render checks using Helm. No Kubernetes access or apply.
# Generated Secrets stay in memory and are never printed or saved as artifacts.
require 'yaml'
require 'json'
require 'open3'
require 'tmpdir'

ROOT = File.expand_path('..', __dir__)
HELM = ARGV.fetch(0, 'helm')

def check(condition, message)
  raise message unless condition
end

def yaml_docs(text)
  YAML.load_stream(text).compact
end

def find_resource(resources, kind, name)
  resources.find { |r| r['kind'] == kind && r.dig('metadata', 'name') == name } ||
    raise("Rendered #{kind}/#{name} not found")
end

def render(name, env)
  # Folder prefixes affect ordering/readability, never the Helm release name.
  app = Dir.glob(File.join(ROOT, '[0-9][0-9]-*', 'app.yml'))
           .flat_map { |path| yaml_docs(File.read(path)) }
           .find { |r| r.dig('metadata', 'name') == name }
  check(!app.nil?, "Application #{name} not found in a numbered component directory")
  source = app.fetch('spec').fetch('sources').find { |s| s['chart'] }
  repo = source.fetch('repoURL')
  chart = source.fetch('chart')
  chart_ref = repo.start_with?('ghcr.io/') ? "oci://#{repo}/#{chart}" : chart
  args = [HELM, 'template', name, chart_ref]
  args.concat(['--repo', repo]) unless chart_ref.start_with?('oci://')
  args.concat(['--version', source.fetch('targetRevision'), '--namespace', app.dig('spec', 'destination', 'namespace'),
               '--kube-version', '1.36.4', '--include-crds'])
  source.fetch('helm').fetch('valueFiles').each do |path|
    raise 'Expected a Git values reference' unless path.start_with?('$values/')
    args.concat(['--values', File.join(ROOT, path.delete_prefix('$values/'))])
  end
  # Render outside the repo so a directory with the chart's name can never
  # shadow the remote chart, even when --repo is supplied.
  output, _stderr, status = Open3.capture3(env, *args, chdir: File.dirname(env.fetch('HELM_CACHE_HOME')))
  # Do not leak rendered Secrets through chart error diagnostics.
  check(status.success?, "Helm rendering failed for #{name} (exit #{status.exitstatus}); chart diagnostics suppressed")
  yaml_docs(output)
rescue Errno::ENOENT
  abort 'Helm executable not found. Install Helm or pass its path: ruby tests/verify-charts.rb /path/to/helm'
end

Dir.mktmpdir('infra-helm-check-') do |dir|
  env = { 'HELM_CACHE_HOME' => File.join(dir, 'cache'), 'HELM_CONFIG_HOME' => File.join(dir, 'config'),
          'HELM_DATA_HOME' => File.join(dir, 'data'), 'HELM_PLUGINS' => File.join(dir, 'plugins') }
  monitoring = render('kube-prometheus-stack', env)
  longhorn = render('longhorn', env)
  %w[cert-manager k8up openbao vault-secrets-webhook].each { |name| render(name, env) }

  # Public cluster resources are complete templates, but their sample defaults
  # must never create live resources unless an explicit component is selected.
  local_chart = File.join(ROOT, 'charts', 'cluster-config')
  %w[none argocd grafana longhorn openbao certificates backups restore].each do |component|
    output, _stderr, status = Open3.capture3(env, HELM, 'template', "check-#{component}", local_chart,
                                              '--set', "component=#{component}", chdir: dir)
    check(status.success?, "Local #{component} template failed; chart diagnostics suppressed")
    resources = yaml_docs(output)
    check(resources.empty? == (component == 'none'), "Unexpected default render for #{component}")
    check(resources.all? { |r| r['kind'] == 'HTTPRoute' }, "#{component} route templates changed") if %w[argocd grafana longhorn openbao].include?(component)
    check(resources.any? { |r| r['kind'] == 'Certificate' } && resources.count { |r| r['kind'] == 'ClusterIssuer' } == 2,
          'Public certificate template incomplete') if component == 'certificates'
    check(resources.any? { |r| r['kind'] == 'Schedule' }, 'Public backup template missing') if component == 'backups'
    check(resources.one? { |r| r['kind'] == 'Restore' }, 'Manual restore template missing') if component == 'restore'
  end

  %w[servicemonitors.monitoring.coreos.com podmonitors.monitoring.coreos.com].each do |name|
    find_resource(monitoring, 'CustomResourceDefinition', name)
  end
  check(monitoring.none? { |r| r['kind'] == 'PersistentVolumeClaim' || r['kind'] == 'Certificate' },
        'Monitoring unexpectedly depends on PVCs or cert-manager')
  monitoring.select { |r| %w[Deployment StatefulSet DaemonSet].include?(r['kind']) }.each do |r|
    check(!r.dig('spec', 'volumeClaimTemplates') || r.dig('spec', 'volumeClaimTemplates').empty?,
          'Monitoring renders volumeClaimTemplates')
    check((r.dig('spec', 'template', 'spec', 'volumes') || []).none? { |v| v['persistentVolumeClaim'] },
          'Monitoring mounts a PVC')
  end
  prometheus = find_resource(monitoring, 'Prometheus', 'kube-prometheus-stack-prometheus')
  alertmanager = find_resource(monitoring, 'Alertmanager', 'kube-prometheus-stack-alertmanager')
  [prometheus, alertmanager].each do |r|
    check(!r.dig('spec', 'storage') || r.dig('spec', 'storage').empty?, 'Monitoring has persistent operator storage')
  end
  %w[serviceMonitorSelector serviceMonitorNamespaceSelector podMonitorSelector podMonitorNamespaceSelector].each do |key|
    check(prometheus.dig('spec', key) == {}, "Prometheus #{key} would filter out infra monitors")
  end
  # These CRDs appear in the monitoring chart render; this does not prove they
  # are installed before earlier charts emit monitoring resources.
  kinds = monitoring.select { |r| r['kind'] == 'CustomResourceDefinition' && r.dig('spec', 'group') == 'monitoring.coreos.com' }
                    .map { |r| r.dig('spec', 'names', 'kind') }
  (monitoring + longhorn).each do |r|
    next unless r.fetch('apiVersion', '').start_with?('monitoring.coreos.com/')
    check(kinds.include?(r['kind']), 'Rendered monitor has no corresponding bootstrap CRD')
  end

  storage_cm = find_resource(longhorn, 'ConfigMap', 'longhorn-storageclass')
  storage = YAML.safe_load(storage_cm.fetch('data').fetch('storageclass.yaml'))
  check(storage.dig('metadata', 'name') == 'longhorn', 'Wrong StorageClass name')
  check(storage['provisioner'] == 'driver.longhorn.io', 'Wrong storage provisioner')
  check(storage.dig('metadata', 'annotations', 'storageclass.kubernetes.io/is-default-class') == 'false',
        'Chart would add a second default StorageClass')
  check(storage.dig('parameters', 'numberOfReplicas') == '1', 'Rendered PVC replica count is not 1')
  check(storage['reclaimPolicy'] == 'Retain', 'Rendered reclaim policy is not Retain')
  settings_cm = find_resource(longhorn, 'ConfigMap', 'longhorn-default-setting')
  settings = YAML.safe_load(settings_cm.fetch('data').fetch('default-setting.yaml'))
  check(JSON.parse(settings.fetch('default-replica-count')) == { 'v1' => '1', 'v2' => '1' },
        'Rendered UI replica defaults do not match single-node configuration')
  ui = find_resource(longhorn, 'Service', 'longhorn-frontend')
  check(ui.dig('spec', 'type') == 'ClusterIP', 'UI unexpectedly exposed directly')
  check(ui.dig('spec', 'ports').any? { |p| p['port'] == 80 }, 'HTTPRoute backend port does not exist')
  monitor = find_resource(longhorn, 'ServiceMonitor', 'longhorn-prometheus-servicemonitor')
  backend = find_resource(longhorn, 'Service', 'longhorn-backend')
  check(monitor.dig('spec', 'selector', 'matchLabels').all? { |k, v| backend.dig('metadata', 'labels', k) == v },
        'Longhorn monitor does not select its metrics Service')
  check(monitor.dig('spec', 'endpoints').all? { |e| backend.dig('spec', 'ports').any? { |p| p['name'] == e['port'] } },
        'Longhorn monitor refers to a missing metrics port')
  manager = find_resource(longhorn, 'DaemonSet', 'longhorn-manager')
  container = manager.dig('spec', 'template', 'spec', 'containers').find { |c| c['name'] == 'longhorn-manager' }
  check(container.dig('resources', 'requests', 'memory') == '256Mi', 'Manager memory request was ignored')
  check(container.dig('resources', 'limits', 'memory') == '512Mi', 'Manager memory limit was ignored')
  check(longhorn.none? { |r| %w[Gateway Ingress HelmRelease HelmRepository].include?(r['kind']) },
        'Unexpected routing or Flux resource rendered by Longhorn')
  puts 'PASS: six pinned charts and public resource templates render, monitoring CRDs/selectors, Longhorn replicas/retention/UI'
end
