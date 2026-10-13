#!/usr/bin/env ruby
# Read-only regression checks; rendered Secrets and dashboard bodies are never printed.
require 'yaml'
require 'json'
require 'open3'
require 'tmpdir'
require 'set'

ROOT = File.expand_path('..', __dir__)
FAMILIES = %w[
  apiserver_watch_events_dispatch_duration_seconds
  etcd_request_duration_seconds
  apiserver_storage_list_duration_seconds
  apiserver_watch_list_duration_seconds
  apiserver_watch_cache_read_wait_seconds
  apiserver_watch_cache_initialization_duration_seconds
].freeze
REQUIRED = %w[apiserver_request_duration_seconds_bucket
              apiserver_request_sli_duration_seconds_bucket
              apiserver_request_total etcd_server_has_leader
              kubelet_volume_stats_used_bytes].freeze

def check(ok, message)
  raise message unless ok
end

def docs(path)
  YAML.load_stream(File.read(File.join(ROOT, path))).compact
end

values = docs('03-core/kube-prometheus-stack/values.yml').first
policies = values.dig('kubeApiServer', 'serviceMonitor', 'metricRelabelings') || []
check(policies.length == 2, 'Preserve chart bucket thinning and add one narrowly scoped API server metric filter')
check(policies.first['action'] == 'drop' && policies.first['sourceLabels'] == ['__name__', 'le'],
      'Existing chart bucket thinning must be retained')
policy = policies.last
check(policy['action'] == 'drop' && policy['sourceLabels'] == ['__name__'], 'Filter must drop by metric name, not remove labels')
regex = Regexp.new("\\A(?:#{policy.fetch('regex')})\\z")
FAMILIES.each do |base|
  ['', '_bucket', '_sum', '_count'].each { |suffix| check(regex.match?(base + suffix), "Missing unused histogram filter: #{base + suffix}") }
  check(!regex.match?(base + '_created'), 'Filter unexpectedly matches additional metrics')
end
REQUIRED.each { |name| check(!regex.match?(name), "Required metric filtered: #{name}") }
check(!regex.match?('unrelated_metric'), 'Filter must not drop unrelated metrics')

crossplane = docs('03-core/crossplane/values.yml').first
activations = crossplane.dig('provider', 'defaultActivations')
check(activations == %w[secretbackendconnections.database.vault.upbound.io secretbackendstaticroles.database.vault.upbound.io], 'Only declared database managed resources may activate automatically')
activation_policy = docs('03-core/crossplane/workload/activation-policy.yaml').first
check(activation_policy.dig('metadata', 'name') == 'default' && activation_policy.dig('spec', 'activate') == activations,
      'The existing default policy must be reconciled; Helm init only creates it once')
check(docs('03-core/crossplane/workload/kustomization.yaml').first.fetch('resources').include?('activation-policy.yaml'),
      'Steady-state activation policy must be selected by Kustomize')
app = docs('03-core/crossplane/app.yml').first
sources = app.dig('spec', 'sources')
check(sources[0].dig('helm', 'valueFiles') == ['$values/03-core/crossplane/values.yml'], 'Crossplane chart must consume the Git values')
check(sources[1]['ref'] == 'values' && sources[1]['path'] == '03-core/crossplane/workload', 'Retain authored provider resources alongside the values source')

cm = docs('01-bootstrap/argocd-bootstrap/application-health-config.yml').first
exclusions = YAML.safe_load(cm.fetch('data').fetch('resource.exclusions'))
def excluded?(exclusions, group, kind)
  exclusions.any? do |entry|
    entry.fetch('apiGroups').any? { |pattern| File.fnmatch?(pattern, group) } &&
      entry.fetch('kinds').any? { |pattern| File.fnmatch?(pattern, kind) }
  end
end
check(excluded?(exclusions, 'pki.vault.upbound.io', 'SecretBackend'), 'Unused legacy provider APIs must not occupy Argo caches')
check(excluded?(exclusions, 'database.vault.m.upbound.io', 'SecretBackendConnection'), 'Unused namespaced provider APIs must not occupy Argo caches')
check(excluded?(exclusions, 'apiextensions.crossplane.io', 'ManagedResourceDefinition'), 'Generated MRDs must not occupy Argo caches')
%w[OidcKey OidcScope OidcClient OidcProvider].each do |kind|
  check(!excluded?(exclusions, 'identity.vault.upbound.io', kind),
        "Application-owned OpenBao identity resource excluded from GitOps: #{kind}")
end
[['', 'Endpoints'], ['discovery.k8s.io', 'EndpointSlice'], ['coordination.k8s.io', 'Lease'],
 ['cert-manager.io', 'CertificateRequest'], ['cilium.io', 'CiliumEndpoint'],
 ['kyverno.io', 'UpdateRequest']].each do |group, kind|
  check(excluded?(exclusions, group, kind), "Existing Argo exclusion lost: #{group}/#{kind}")
end

# No authored desired resource may be hidden from GitOps, including future components.
Dir.glob(File.join(ROOT, '[0-9][0-9]-*', '**', '*.{yaml,yml}')).each do |path|
  next if path.split(File::SEPARATOR).include?('templates') # Helm templates are checked by rendering.
  YAML.load_stream(File.read(path)).compact.each do |resource|
    next unless resource.is_a?(Hash) && resource['kind'] && resource['apiVersion']
    group = resource['apiVersion'].include?('/') ? resource['apiVersion'].split('/').first : ''
    check(!excluded?(exclusions, group, resource['kind']), "Authored resource excluded from GitOps: #{path.delete_prefix(ROOT + '/')}")
  end
end

# Optional pinned local chart render. Supply the unpacked monitoring chart path.
if ARGV[0]
  Dir.mktmpdir('ram-render-') do |dir|
    env = {'HELM_CONFIG_HOME' => dir, 'HELM_CACHE_HOME' => dir, 'HELM_DATA_HOME' => dir}
    args = ['helm', 'template', 'kube-prometheus-stack', ARGV[0], '--namespace', 'kube-prometheus-stack',
            '--kube-version', '1.37.1', '--values', File.join(ROOT, '03-core/kube-prometheus-stack/values.yml')]
    output, _error, status = Open3.capture3(env, *args)
    check(status.success?, 'Pinned monitoring render failed; diagnostics suppressed')
    resources = YAML.load_stream(output).compact
    monitor = resources.find { |r| r['kind'] == 'ServiceMonitor' && r.dig('metadata', 'name') == 'kube-prometheus-stack-apiserver' }
    check(monitor, 'Rendered API server monitor missing')
    check(monitor.dig('spec', 'endpoints', 0, 'metricRelabelings') == policies, 'Helm did not render the metric filter exactly')
    dependents = resources.select { |r| r['kind'] == 'PrometheusRule' || (r['kind'] == 'ConfigMap' && r.dig('metadata', 'labels', 'grafana_dashboard') == '1') }
    dependents.each do |resource|
      text = JSON.generate(resource['spec'] || resource['data'])
      FAMILIES.each { |base| check(!text.include?(base), "Rendered rule/dashboard needs filtered metric: #{base}") }
    end
    if ARGV[1]
      baseline_args = args.dup
      baseline_args[-1] = ARGV[1]
      baseline_output, _error, baseline_status = Open3.capture3(env, *baseline_args)
      check(baseline_status.success?, 'Baseline monitoring render failed; diagnostics suppressed')
      baseline_resources = YAML.load_stream(baseline_output).compact
      baseline_monitor = baseline_resources.find { |r| r['kind'] == 'ServiceMonitor' && r.dig('metadata', 'name') == 'kube-prometheus-stack-apiserver' }
      check(baseline_monitor.dig('spec', 'endpoints', 0, 'metricRelabelings') == [policies.first],
            'All existing chart metric filters must be preserved exactly')
      index = lambda do |items|
        items.to_h do |item|
          # Grafana's chart-generated initial password is intentionally random.
          if item['kind'] == 'Secret' && item.dig('metadata', 'name') == 'kube-prometheus-stack-grafana'
            %w[admin-user admin-password].each { |key| item.fetch('data').delete(key) }
          end
          if item['kind'] == 'Deployment' && item.dig('metadata', 'name') == 'kube-prometheus-stack-grafana'
            item.dig('spec', 'template', 'metadata', 'annotations')&.delete('checksum/secret')
          end
          [[item['apiVersion'], item['kind'], item.dig('metadata', 'namespace'), item.dig('metadata', 'name')], item]
        end
      end
      before = index.call(baseline_resources)
      after = index.call(resources)
      changed = (before.keys | after.keys).select { |key| before[key] != after[key] }
      expected = [monitor['apiVersion'], 'ServiceMonitor', monitor.dig('metadata', 'namespace'), 'kube-prometheus-stack-apiserver']
      check(changed == [expected], "Monitoring patch must change only the API server ServiceMonitor; changed identities: #{changed.map { |key| key[1..] }.inspect}")
    end
  end
end
puts 'PASS: narrowly scoped unused metric filtering, selective future activation, and safe Argo resource exclusions'
