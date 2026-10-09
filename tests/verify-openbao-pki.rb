#!/usr/bin/env ruby
# Offline steady-state OpenBao PKI Kubernetes-resource checks; never contacts a cluster.
require 'yaml'
require 'open3'

ROOT = File.expand_path('..', __dir__)

def check(condition, message)
  raise message unless condition
end

application_path = '05-platform/openbao-pki/application.yml'
app = YAML.load_file(File.join(ROOT, application_path))
check(app.dig('metadata', 'name') == 'openbao-pki', 'Wrong PKI Application identity')
check(app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '5',
      'OpenBao PKI must remain in wave 5')
source = app.dig('spec', 'source')
check(source == {
  'repoURL' => 'https://github.com/dmuiX/k3s-on-omv-infra.git',
  'targetRevision' => 'main',
  'path' => '05-platform/openbao-pki/workload'
}, 'OpenBao PKI must render the steady-state workload from the root branch')
cert_manager_values = YAML.load_file(File.join(ROOT, '02-controllers/cert-manager/values.yml'))
check(cert_manager_values['clusterResourceNamespace'] == 'cert-manager',
      'ClusterIssuer ServiceAccount references must resolve in cert-manager')

root = YAML.load_file(File.join(ROOT, 'infra.yml'))
directory = root.dig('spec', 'source', 'directory')
check(!directory.key?('exclude'), 'Root must not carry mutable exclusion state')
check(File.fnmatch(directory.fetch('include'), application_path, File::FNM_EXTGLOB),
      'Root must discover OpenBao PKI in steady state')
check(File.fnmatch(directory.fetch('include'), '06-data/postgresql/app.yml', File::FNM_EXTGLOB),
      'Root must discover PostgreSQL in steady state')

output, stderr, result = Open3.capture3('kubectl', 'kustomize', File.join(ROOT, '05-platform/openbao-pki/workload'))
raise "Kustomize failed: #{stderr}" unless result.success?
resources = YAML.load_stream(output).compact
find_all = ->(kind) { resources.select { |resource| resource['kind'] == kind } }
check(resources.map { |resource| resource['kind'] }.sort ==
      %w[Certificate ClusterIssuer ClusterIssuer Role RoleBinding ServiceAccount ServiceAccount].sort,
      'PKI workload must contain only issuers, certificate and cert-manager identity/RBAC resources')
check(resources.none? { |resource| %w[Job CronJob ConfigMap NetworkPolicy].include?(resource['kind']) },
      'PKI workload must not render a custom polling reconciler')

certificate = find_all.call('Certificate').fetch(0)
check(certificate.dig('metadata', 'name') == 'openbao-internal-tls' &&
      certificate.dig('metadata', 'namespace') == 'openbao' &&
      certificate.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '3',
      'OpenBao internal TLS Certificate identity, namespace or wave changed')
check(certificate.dig('spec', 'secretName') == 'openbao-internal-tls' &&
      certificate.dig('spec', 'duration') == '2160h' && certificate.dig('spec', 'renewBefore') == '360h' &&
      certificate.dig('spec', 'privateKey') == {
        'algorithm' => 'RSA', 'size' => 2048, 'rotationPolicy' => 'Always'
      } && certificate.dig('spec', 'usages') == ['server auth'],
      'OpenBao internal TLS key, lifetime or usage contract changed')
check(certificate.dig('spec', 'dnsNames') == [
        'openbao-active-tls.openbao.svc', 'openbao-active-tls.openbao.svc.cluster.local'
      ] && certificate.dig('spec', 'issuerRef') == {
        'group' => 'cert-manager.io', 'kind' => 'ClusterIssuer', 'name' => 'openbao-pki-services'
      }, 'OpenBao internal TLS DNS names or issuer changed')

service_accounts = find_all.call('ServiceAccount').to_h { |resource| [resource.dig('metadata', 'name'), resource] }
check(service_accounts.keys.sort == %w[openbao-pki-clients openbao-pki-services],
      'Only cert-manager issuer identities may remain')
service_accounts.each_value do |identity|
  check(identity.dig('metadata', 'namespace') == 'cert-manager' && identity['automountServiceAccountToken'] == false,
        'Issuer identities must live in cert-manager without token automounting')
end

role = find_all.call('Role').fetch(0)
check(role.dig('metadata', 'namespace') == 'cert-manager' && role.fetch('rules') == [{
  'apiGroups' => [''], 'resources' => ['serviceaccounts/token'],
  'resourceNames' => %w[openbao-pki-services openbao-pki-clients], 'verbs' => ['create']
}], 'TokenRequest RBAC is broader than the two issuer identities')
binding = find_all.call('RoleBinding').fetch(0)
check(binding.fetch('subjects') == [{ 'kind' => 'ServiceAccount', 'name' => 'cert-manager', 'namespace' => 'cert-manager' }],
      'TokenRequest permission must be bound only to cert-manager')

issuers = find_all.call('ClusterIssuer').to_h { |resource| [resource.dig('metadata', 'name'), resource] }
check(issuers.keys.sort == %w[openbao-pki-clients openbao-pki-services], 'Expected exactly two Vault ClusterIssuers')
{
  'openbao-pki-services' => ['pki-services/sign/services', 'cert-manager-pki-services', 'openbao-pki-services'],
  'openbao-pki-clients' => ['pki-clients/sign/clients', 'cert-manager-pki-clients', 'openbao-pki-clients']
}.each do |name, (path, auth_role, identity)|
  vault = issuers.fetch(name).dig('spec', 'vault')
  check(vault['path'] == path && vault['server'] == 'http://openbao-active.openbao.svc.cluster.local:8200',
        "Wrong Vault endpoint for #{name}")
  check(vault.dig('auth', 'kubernetes') == {
    'mountPath' => '/v1/auth/kubernetes', 'role' => auth_role,
    'serviceAccountRef' => { 'name' => identity, 'audiences' => ["vault://#{identity}"] }
  }, "Wrong Kubernetes auth identity/audience for #{name}")
end

openbao_files = Dir.glob(File.join(ROOT, '{04-secrets,05-platform/openbao-pki}', '**', '*')).select { |path| File.file?(path) }
check(openbao_files.none? { |path| %w[.py .hcl].include?(File.extname(path)) ||
      File.basename(path).match?(/cronjob|initial-job|config\.json/) },
      'OpenBao API reconciler code, Jobs, configuration or duplicate policies remain')
authored = openbao_files.select { |path| %w[.yml .yaml].include?(File.extname(path)) }.flat_map do |path|
  YAML.load_stream(File.read(path)).compact
end
check(authored.none? { |resource| %w[Job CronJob].include?(resource['kind']) },
      'OpenBao custom polling workloads must not remain anywhere in the authored manifests')
puts 'PASS: steady-state OpenBao PKI renders only native Kubernetes issuer, certificate and cert-manager RBAC resources'
