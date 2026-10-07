#!/usr/bin/env ruby
# Optional offline integration check. Requires the private values checkout + Helm.
# Never prints rendered Secret data or private identifiers.
require 'yaml'
require 'open3'
require 'uri'

ROOT = File.expand_path('..', __dir__)
PRIVATE = File.expand_path(ARGV.fetch(0, '../k3s-on-omv-live'), ROOT)
BOOTSTRAP = File.expand_path(ARGV.fetch(1, '../k3s-on-omv-bootstrap/traefik/traefik-config.yml'), ROOT)
VALUES = File.join(PRIVATE, 'clusters/omv/values.yml')
CHART = File.join(ROOT, 'charts/cluster-config')

def check(condition, message)
  raise message unless condition
end

def wave(app)
  Integer(app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') || 0)
end

def render(component)
  output, status = Open3.capture2('helm', 'template', "check-#{component}", CHART,
                                  '--set', "component=#{component}", '--values', VALUES, err: File::NULL)
  check(status.success?, "Failed to render #{component} with private values; diagnostics suppressed")
  YAML.load_stream(output).compact
rescue Errno::ENOENT
  abort 'Helm is required: ruby tests/verify-private-values.rb'
end

root = YAML.load_file(File.join(ROOT, 'infra.yml'))
public_url = root.dig('spec', 'source', 'repoURL')
directory = root.dig('spec', 'source', 'directory')
include_pattern = directory.fetch('include')
exclude_pattern = directory.fetch('exclude')
apps = Dir.glob(File.join(ROOT, '[0-9][0-9]-*', '*', '{*app.yml,application.yml}'), File::FNM_EXTGLOB).map do |path|
  relative = path.delete_prefix(ROOT + '/')
  selected = File.fnmatch(include_pattern, relative, File::FNM_EXTGLOB) &&
    !File.fnmatch(exclude_pattern, relative, File::FNM_EXTGLOB)
  YAML.load_file(path) if selected
end.compact.to_h { |app| [app.dig('metadata', 'name'), app] }
expected_apps = %w[argocd-config argocd-route grafana-route kube-prometheus-stack monitoring-crds
                   cert-manager cert-manager-config k8up longhorn longhorn-route openbao
                   openbao-access-config openbao-config openbao-route vault-secrets-webhook]
check(apps.keys.sort == expected_apps.sort && apps.values.map { |app| wave(app) }.uniq.sort == [1, 2, 3, 4, 5, 6, 8],
      'Default public root must own regular components and keep staged phases inactive')
check(File.file?(VALUES), 'Private values file missing')
private_values = YAML.load_file(VALUES)
backup_endpoint = URI.parse(private_values.dig('backup', 'endpoint'))
check(backup_endpoint.is_a?(URI::HTTPS) && ['', '/'].include?(backup_endpoint.path),
      'Backup endpoint must not repeat the separately configured bucket path')

rendered = {}
{ 'argocd-route' => ['argocd', 8, 'argocd-config', 'argocd-server', 80],
  'grafana-route' => ['grafana', 8, 'kube-prometheus-stack', 'kube-prometheus-stack-grafana', 80],
  'longhorn-route' => ['longhorn', 8, 'longhorn', 'longhorn-frontend', 80],
  'openbao-route' => ['openbao', 8, 'openbao', 'openbao-ui', 8200],
  'cert-manager-config' => ['certificates', 5, 'vault-secrets-webhook'],
  'openbao-config' => ['backups', 6, 'k8up'] }.each do |name, (component, stage, dependency, service, port)|
  app = apps.fetch(name)
  check(wave(app) == stage && wave(apps.fetch(dependency)) <= stage &&
        (!service || wave(apps.fetch('cert-manager-config')) < stage),
        "#{name} is scheduled before its backend/certificate dependency")
  chart_source, values_source = app.dig('spec', 'sources')
  check(chart_source['repoURL'] == public_url && chart_source['path'] == 'charts/cluster-config' &&
        chart_source.dig('helm', 'valueFiles') == ['$values/clusters/omv/values.yml'] &&
        chart_source.dig('helm', 'parameters', 0) == { 'name' => 'component', 'value' => component } &&
        values_source['repoURL'] == 'https://github.com/dmuiX/k3s-on-omv-live.git' &&
        values_source['ref'] == 'values' && !values_source.key?('path'),
        "#{name} must render the public chart with private Git values")
  docs = render(component)
  check(!docs.empty?, "No rendered resource for #{component}")
  docs.each do |doc|
    key = [doc['apiVersion'], doc['kind'], doc.dig('metadata', 'namespace'), doc.dig('metadata', 'name')]
    check(!rendered.key?(key), "Two Applications own #{key.last}")
    rendered[key] = doc
  end
  next unless service
  route = docs.fetch(0)
  check(route['kind'] == 'HTTPRoute' && route.dig('spec', 'hostnames') == [private_values.dig('routes', component, 'hostname')] &&
        route.dig('spec', 'rules', 0, 'backendRefs', 0) ==
          { 'group' => '', 'kind' => 'Service', 'name' => service, 'port' => port, 'weight' => 1 },
        "Private values did not produce the expected #{component} route")
end

certificate = rendered.fetch(['cert-manager.io/v1', 'Certificate', 'kube-system', 'wildcard-tls'])
issuer = rendered.fetch(['cert-manager.io/v1', 'ClusterIssuer', nil, 'cluster-issuer-prod'])
schedule = rendered.fetch(['k8up.io/v1', 'Schedule', 'openbao', 'openbao-k8up-schedule'])
check(certificate.dig('spec', 'dnsNames') == [private_values.dig('certificate', 'dnsName')] &&
      issuer.dig('spec', 'acme', 'email') == private_values.dig('certificate', 'acmeEmail') &&
      wave(certificate) == 1, 'Private certificate values or child wave mismatch')
check(schedule.dig('spec', 'backend', 's3', 'endpoint') == private_values.dig('backup', 'endpoint') &&
      schedule.dig('spec', 'backend', 's3', 'bucket') == private_values.dig('backup', 'bucket'),
      'Private backup values not rendered')
check(render('restore').one? { |r| r['kind'] == 'Restore' } &&
      apps.values.none? { |app| app.dig('spec', 'sources', 0, 'helm', 'parameters', 0, 'value') == 'restore' },
      'Restore must be manual-only')
check(render('none').empty?, 'Sample chart defaults must not deploy resources')

check(File.file?(BOOTSTRAP), 'Pass the private host Gateway config as the second argument')
host = YAML.safe_load(YAML.load_file(BOOTSTRAP).dig('spec', 'valuesContent'))
listener = host.dig('gateway', 'listeners', 'websecure')
check(listener.dig('certificateRefs', 0, 'name') == certificate.dig('spec', 'secretName') &&
      listener['hostname'] == certificate.dig('spec', 'dnsNames', 0),
      'Gateway listener and private wildcard certificate differ')

identifiers = private_values.fetch('routes').values.map { |r| r.fetch('hostname') }
identifiers += [private_values.dig('certificate', 'dnsName'), private_values.dig('certificate', 'acmeEmail'),
                private_values.dig('backup', 'endpoint'), private_values.dig('backup', 'bucket')]
files = Dir.glob(File.join(ROOT, '{[0-9][0-9]-*,charts,docs,tests}', '**', '*.{md,yml,yaml,rb,tpl,py,json,hcl}'), File::FNM_EXTGLOB)
files += [File.join(ROOT, 'README.md'), File.join(ROOT, 'infra.yml')]
files.uniq.each do |path|
  next unless File.file?(path)
  check(identifiers.none? { |value| File.read(path).include?(value) },
        "Private identifier leaked into public file #{path.delete_prefix(ROOT + '/')}")
end
puts 'PASS: one public root, private Helm values, rendered routes/TLS/backups, no duplicate owners'
