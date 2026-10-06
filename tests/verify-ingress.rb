#!/usr/bin/env ruby
# Public templates + Argo Applications, with no real cluster identifiers.
# Only Helm value overrides come from the separate private repo.
require 'yaml'

ROOT = File.expand_path('..', __dir__)

def check(condition, message)
  raise message unless condition
end

paths = Dir.glob(File.join(ROOT, '[0-9][0-9]-*', '*.{yml,yaml}'))
documents = paths.flat_map { |path| YAML.load_stream(File.read(path)).compact }
check(documents.none? { |doc| %w[Gateway GatewayClass TLSRoute HTTPRoute Certificate ClusterIssuer Schedule Restore].include?(doc['kind']) },
      'Public base includes a cluster-specific route, certificate or backup')
check(documents.none? { |doc| doc['kind'] == 'Namespace' && doc.dig('metadata', 'name') == 'ingress' },
      'K3s owns Traefik; public base must not create an ingress namespace')
check(documents.none? { |doc| doc['kind'] == 'Secret' },
      'Live Kubernetes Secrets belong in the private overlay or OpenBao')
check(documents.none? { |doc| doc['kind'] == 'HelmChartConfig' },
      'Traefik is host-managed in the separate bootstrap repository')
check(!Dir.exist?(File.join(ROOT, 'external-dns')), 'External-DNS must not be deployed')

root = YAML.load_file(File.join(ROOT, 'infra.yml'))
source = root.fetch('spec').fetch('source')
check(source.fetch('path') == '.' && source.dig('directory', 'recurse'), 'Public root must discover reusable Applications')
pattern = source.dig('directory', 'include')
%w[01-argocd-bootstrap/application-health-config.yml 01-monitoring-crds/app.yml
   02-longhorn/app.yml 03-kube-prometheus-stack/app.yml 03-openbao/app.yml].each do |path|
  check(File.fnmatch(pattern, path, File::FNM_EXTGLOB), "Public root excludes #{path}")
end

apps = documents.select { |doc| doc['kind'] == 'Application' }
apps.each do |app|
  sources = app.dig('spec', 'sources') || [app.dig('spec', 'source')]
  sources.compact.each do |child|
    next if child['chart'] || (child['ref'] == 'values' && child['repoURL'] != source['repoURL'])
    check(child['repoURL'] == source['repoURL'] && child['targetRevision'].to_s.match?(/\A[0-9a-f]{40}\z/),
          "Public Git source is not immutably pinned: #{app.dig('metadata', 'name')}")
    check(!child['path'] || !child['path'].start_with?('clusters/'),
          "Public Application selects private manifests: #{app.dig('metadata', 'name')}")
  end
end
server = YAML.load_file(File.join(ROOT, '01-argocd-server-config', 'argocd-cmd-params-cm.yml'))
check(server.dig('data', 'server.insecure') == 'true', 'Traefik HTTP backend setting missing')
%w[05-certificates 06-openbao-backups].each do |dir|
  check(Dir.glob(File.join(ROOT, dir, '*.{yml,yaml}')).map { |f| File.basename(f) } == ['config-app.yml'],
        "#{dir} must contain only its public Application, not real cluster data")
end

# Check publishable prose/manifests, not vendored chart schemas or CRDs.
text_files = Dir.glob(File.join(ROOT, '{[0-9][0-9]-*,charts,docs,tests}', '**', '*.{md,yml,yaml,rb,tpl,py,json,hcl}'), File::FNM_EXTGLOB)
text_files += [File.join(ROOT, 'README.md'), File.join(ROOT, 'infra.yml')]
text_files.uniq.each do |path|
  next unless File.file?(path)
  contents = File.read(path)
  check(!contents.match?(/\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b/),
        "Private LAN address in #{path.delete_prefix(ROOT + '/')}")
  check(!contents.match?(/[a-z0-9]{32}\.\w+\.r2\.cloudflarestorage\.com/i),
        "R2 account endpoint in #{path.delete_prefix(ROOT + '/')}")
end
puts 'PASS: public Applications/templates have no real cluster identifiers or raw cluster resources'
