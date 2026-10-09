#!/usr/bin/env ruby
# Optional offline integration check. Requires the private values checkout + Helm.
# Never prints rendered Secret data or private identifiers.
require 'yaml'
require 'base64'
require 'fileutils'
require 'open3'
require 'tmpdir'
require 'uri'

ROOT = File.expand_path('..', __dir__)
PRIVATE = File.expand_path(ARGV.fetch(0, '../k3s-on-omv-live'), ROOT)
GATEWAY_CONFIG = File.expand_path(
  ARGV.fetch(1, '../k3s-on-omv-bootstrap/ansible/roles/k3s_cluster/templates/traefik-config.yml.j2'), ROOT
)
VALUES = File.join(PRIVATE, 'clusters/omv/values.yml')
CHART = File.join(ROOT, 'charts/cluster-config')

def check(condition, message)
  raise message unless condition
end

def wave(app)
  Integer(app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') || 0)
end

def render_chart(component, chart)
  output, status = Open3.capture2('helm', 'template', "check-#{component}", chart,
                                  '--set', "component=#{component}", '--values', VALUES, err: File::NULL)
  check(status.success?, "Failed to render #{component} with private values; diagnostics suppressed")
  YAML.load_stream(output).compact
rescue Errno::ENOENT
  abort 'Helm is required: ruby tests/verify-private-values.rb'
end

def render(component, revision = nil)
  return render_chart(component, CHART) unless revision

  Dir.mktmpdir('cluster-config-pin-') do |directory|
    paths, error, status = Open3.capture3(
      'git', '-C', ROOT, 'ls-tree', '-r', '--name-only', revision, '--', 'charts/cluster-config'
    )
    check(status.success? && error.empty? && !paths.empty?,
          "Pinned cluster-config chart #{revision} is unavailable")
    paths.lines(chomp: true).each do |path|
      contents, show_error, show_status = Open3.capture3(
        'git', '-C', ROOT, 'show', "#{revision}:#{path}"
      )
      check(show_status.success? && show_error.empty?, "Pinned chart file #{path} is unavailable")
      destination = File.join(directory, path)
      FileUtils.mkdir_p(File.dirname(destination))
      File.binwrite(destination, contents)
    end
    render_chart(component, File.join(directory, 'charts/cluster-config'))
  end
end

root = YAML.load_file(File.join(ROOT, 'infra.yml'))
public_url = root.dig('spec', 'source', 'repoURL')
directory = root.dig('spec', 'source', 'directory')
include_pattern = directory.fetch('include')
check(!directory.key?('exclude'), 'Steady-state root must not carry mutable exclusion state')
apps = Dir.glob(File.join(ROOT, '[0-9][0-9]-*', '*', '{*app.yml,application.yml}'), File::FNM_EXTGLOB).map do |path|
  relative = path.delete_prefix(ROOT + '/')
  YAML.load_file(path) if File.fnmatch(include_pattern, relative, File::FNM_EXTGLOB)
end.compact.to_h { |app| [app.dig('metadata', 'name'), app] }
expected_apps = %w[argocd-config argocd-route grafana-route kube-prometheus-stack monitoring-crds
                   cert-manager cert-manager-config k8up longhorn longhorn-route openbao openbao-config
                   openbao-pki openbao-route postgresql vault-secrets-webhook]
check(apps.keys.sort == expected_apps.sort && apps.values.map { |app| wave(app) }.uniq.sort == [1, 2, 3, 4, 5, 6],
      'Public root must own the complete steady-state inventory')
check(File.file?(VALUES), 'Private values file missing')
private_values = YAML.load_file(VALUES)
expected_gateway = {
  'name' => 'traefik-gateway',
  'namespace' => 'kube-system',
  'listener' => 'websecure'
}
backup_endpoint = URI.parse(private_values.dig('backup', 'endpoint'))
check(backup_endpoint.is_a?(URI::HTTPS) && ['', '/'].include?(backup_endpoint.path),
      'Backup endpoint must not repeat the separately configured bucket path')

rendered = {}
{ 'argocd-route' => ['argocd', 5, 'argocd-config', 'argocd-server', 80],
  'grafana-route' => ['grafana', 5, 'kube-prometheus-stack', 'kube-prometheus-stack-grafana', 80],
  'longhorn-route' => ['longhorn', 5, 'longhorn', 'longhorn-frontend', 80],
  'openbao-route' => ['openbao', 5, 'openbao', 'openbao-ui', 8200],
  'cert-manager-config' => ['certificates', 5, 'vault-secrets-webhook'],
  'openbao-config' => ['backups', 6, 'k8up'] }.each do |name, (component, stage, dependency, service, port)|
  app = apps.fetch(name)
  check(wave(app) == stage && wave(apps.fetch(dependency)) <= stage &&
        (!service || wave(apps.fetch('cert-manager-config')) <= stage),
        "#{name} is scheduled before its backend/certificate dependency")
  chart_source, values_source = app.dig('spec', 'sources')
  check(chart_source['repoURL'] == public_url && chart_source['path'] == 'charts/cluster-config' &&
        chart_source.dig('helm', 'valueFiles') == ['$values/clusters/omv/values.yml'] &&
        chart_source.dig('helm', 'parameters', 0) == { 'name' => 'component', 'value' => component } &&
        values_source['repoURL'] == 'https://github.com/dmuiX/k3s-on-omv-live.git' &&
        values_source['ref'] == 'values' && !values_source.key?('path'),
        "#{name} must render the public chart with private Git values")
  docs = render(component, chart_source['targetRevision'])
  check(!docs.empty?, "No rendered resource for #{component}")
  docs.each do |doc|
    key = [doc['apiVersion'], doc['kind'], doc.dig('metadata', 'namespace'), doc.dig('metadata', 'name')]
    check(!rendered.key?(key), "Two Applications own #{key.last}")
    rendered[key] = doc
  end
  next unless service
  route = docs.fetch(0)
  check(route['kind'] == 'HTTPRoute' && route.dig('spec', 'hostnames') == [private_values.dig('routes', component, 'hostname')] &&
        route.dig('spec', 'parentRefs', 0) == {
          'group' => 'gateway.networking.k8s.io', 'kind' => 'Gateway',
          'name' => expected_gateway['name'], 'namespace' => expected_gateway['namespace'],
          'sectionName' => expected_gateway['listener']
        } &&
        route.dig('spec', 'rules', 0, 'backendRefs', 0) ==
          { 'group' => '', 'kind' => 'Service', 'name' => service, 'port' => port, 'weight' => 1 },
        "Private values did not produce the expected #{component} route")
end

pki_network_chart = File.join(ROOT, '02-controllers/cert-manager/network-policy')
pki_network_output, pki_network_status = Open3.capture2(
  'helm', 'template', 'cert-manager-network-policy', pki_network_chart,
  '--values', VALUES, err: File::NULL
)
pki_network_docs = YAML.load_stream(pki_network_output).compact
check(pki_network_status.success? && pki_network_docs.length == 1 &&
      pki_network_docs.first['kind'] == 'NetworkPolicy',
      'Private API endpoint values must render exactly one cert-manager-owned NetworkPolicy')
pki_network = pki_network_docs.first
api_egress = pki_network.dig('spec', 'egress').find do |entry|
  entry.fetch('ports').map { |port| [port['protocol'], port['port']] } == [['TCP', 6443]]
end
expected_api_cidrs = private_values.dig('clusterNetwork', 'kubernetesApiServerEndpointCIDRs')
check(api_egress && expected_api_cidrs&.length == 3 &&
      api_egress.fetch('to').map { |peer| peer.dig('ipBlock', 'cidr') }.sort == expected_api_cidrs.sort &&
      expected_api_cidrs.all? { |cidr| cidr.match?(/\A(?:[0-9]{1,3}\.){3}[0-9]{1,3}\/32\z/) },
      'cert-manager API egress must use only the three private /32 endpoints')

certificate = rendered.fetch(['cert-manager.io/v1', 'Certificate', 'kube-system', 'wildcard-tls'])
issuer = rendered.fetch(['cert-manager.io/v1', 'ClusterIssuer', nil, 'cluster-issuer-prod'])
schedule = rendered.fetch(['k8up.io/v1', 'Schedule', 'openbao', 'openbao-k8up-schedule'])
repo_password = rendered.fetch(['v1', 'Secret', 'openbao', 'k8up-repo-password'])
r2_credentials = rendered.fetch(['v1', 'Secret', 'openbao', 'r2-credentials'])
wildcard_name = private_values.dig('certificate', 'dnsName')
wildcard_suffix = wildcard_name.to_s.delete_prefix('*')
route_hosts = private_values.fetch('routes').values.map { |route| route.fetch('hostname') }
check(wildcard_name.to_s.start_with?('*.') && route_hosts.all? do |hostname|
        label = hostname.delete_suffix(wildcard_suffix)
        hostname.end_with?(wildcard_suffix) && !label.empty? && !label.include?('.')
      end, 'Every early route hostname must be covered by the one-label wildcard certificate')
check(certificate.dig('spec', 'dnsNames') == [wildcard_name] &&
      certificate.dig('spec', 'secretName') == 'wildcard-tls' &&
      issuer.dig('spec', 'acme', 'email') == private_values.dig('certificate', 'acmeEmail') &&
      wave(certificate) == 1, 'Private certificate values, TLS Secret, or child wave mismatch')
check(schedule.dig('spec', 'backend', 's3', 'endpoint') == private_values.dig('backup', 'endpoint') &&
      schedule.dig('spec', 'backend', 's3', 'bucket') == private_values.dig('backup', 'bucket'),
      'Private backup values not rendered')
check(Base64.strict_decode64(repo_password.dig('data', 'password')) ==
        'vault:kv/data/k8up/repository-password#password' &&
      Base64.strict_decode64(r2_credentials.dig('data', 'access-key-id')) ==
        'vault:kv/data/k8up/r2-credentials#access-key-id' &&
      Base64.strict_decode64(r2_credentials.dig('data', 'secret-access-key')) ==
        'vault:kv/data/k8up/r2-credentials#secret-access-key',
      'Pinned backup chart must consume only canonical K8up credential paths')
postgresql = render('postgresql')
postgresql_cluster = postgresql.find do |resource|
  resource['kind'] == 'PostgresCluster' && resource.dig('metadata', 'name') == 'platform-postgres'
end
postgresql_secret = postgresql.find do |resource|
  resource['kind'] == 'Secret' && resource.dig('metadata', 'name') == 'platform-postgres-pgbackrest'
end
repo = postgresql_cluster&.dig('spec', 'backups', 'pgbackrest', 'repos', 0, 's3')
s3_config = postgresql_secret&.dig('stringData', 's3.conf').to_s
expected_postgresql_region = private_values.dig('postgresqlBackup', 'region') ||
  YAML.load_file(File.join(CHART, 'values.yaml')).dig('postgresqlBackup', 'region')
postgresql_api_policies = postgresql.select do |resource|
  resource['kind'] == 'NetworkPolicy' && resource.dig('metadata', 'name').end_with?('kubernetes-api')
end
check(postgresql_api_policies.length == 2 && postgresql_api_policies.all? do |policy|
        policy.dig('spec', 'egress', 0, 'ports') == [{'protocol' => 'TCP', 'port' => 6443}] &&
          policy.dig('spec', 'egress', 0, 'to').map { |peer| peer.dig('ipBlock', 'cidr') }.sort ==
            expected_api_cidrs.sort
      end, 'PostgreSQL API egress must use only the three private /32 endpoints')
check(postgresql_cluster && postgresql_secret &&
      repo['endpoint'] == private_values.dig('postgresqlBackup', 'endpoint').sub(%r{\Ahttps://}, '').sub(%r{/\z}, '') &&
      repo['bucket'] == private_values.dig('postgresqlBackup', 'bucket') &&
      repo['region'] == expected_postgresql_region &&
      s3_config.include?('vault:kv/data/postgresql/r2-credentials#access-key-id') &&
      s3_config.include?('vault:kv/data/postgresql/r2-credentials#secret-access-key'),
      'Private PostgreSQL pgBackRest identifiers or OpenBao references did not render')
check(render('restore').one? { |r| r['kind'] == 'Restore' } &&
      apps.values.none? { |app| app.dig('spec', 'sources', 0, 'helm', 'parameters', 0, 'value') == 'restore' },
      'Restore must be manual-only')
check(render('none').empty?, 'Sample chart defaults must not deploy resources')

check(File.file?(GATEWAY_CONFIG), 'Pass the current Traefik HelmChartConfig or template as the second argument')
gateway_source = File.read(GATEWAY_CONFIG)
# The bootstrap source is an Ansible template. Its image placeholders are not
# relevant to the Gateway contract and are replaced only for local YAML parsing.
gateway_source = gateway_source.gsub(/\{\{[^{}]+\}\}/, 'test-value')
gateway_config = YAML.safe_load(gateway_source)
host = YAML.safe_load(gateway_config.dig('spec', 'valuesContent'))
listener = host.dig('gateway', 'listeners', 'websecure')
check(listener.dig('certificateRefs', 0, 'name') == certificate.dig('spec', 'secretName') &&
      listener['hostname'] == certificate.dig('spec', 'dnsNames', 0),
      'Gateway listener and private wildcard certificate differ')

identifiers = private_values.fetch('routes').values.map { |r| r.fetch('hostname') }
identifiers += [private_values.dig('certificate', 'dnsName'), private_values.dig('certificate', 'acmeEmail'),
                private_values.dig('backup', 'endpoint'), private_values.dig('backup', 'bucket'),
                private_values.dig('postgresqlBackup', 'endpoint'), private_values.dig('postgresqlBackup', 'bucket')]
identifiers += expected_api_cidrs
files = Dir.glob(File.join(ROOT, '{[0-9][0-9]-*,charts,docs,tests}', '**', '*.{md,yml,yaml,rb,tpl,py,json,hcl}'), File::FNM_EXTGLOB)
files += [File.join(ROOT, 'README.md'), File.join(ROOT, 'infra.yml')]
files.uniq.each do |path|
  next unless File.file?(path)
  check(identifiers.none? { |value| File.read(path).include?(value) },
        "Private identifier leaked into public file #{path.delete_prefix(ROOT + '/')}")
end
puts 'PASS: one public root, private Helm values, rendered routes/TLS/backups, no duplicate owners'
