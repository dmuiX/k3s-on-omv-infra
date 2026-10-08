#!/usr/bin/env ruby
# Offline render and mandatory staged-activation checks; never contacts a cluster.
require 'yaml'
require 'json'
require 'open3'

ROOT = File.expand_path('..', __dir__)
def check(condition, message)
  raise message unless condition
end

application_path = '05-platform/openbao-pki/application.yml'
app = YAML.load_file(File.join(ROOT, application_path))
check(File.basename(application_path) == 'application.yml', 'Staged Application filename changed')
check(app.dig('metadata', 'name') == 'openbao-pki', 'Wrong mandatory PKI Application identity')
sources = app.dig('spec', 'sources')
workload_source = sources&.find { |source| source['path'] == '05-platform/openbao-pki/workload' }
network_source = sources&.find { |source| source['path'] == 'charts/cluster-config' }
private_source = sources&.find { |source| source['ref'] == 'values' }
check(workload_source && network_source && private_source && sources.length == 3,
      'PKI must combine its public workload/network template with one private values source')
revision = workload_source['targetRevision'].to_s
check(revision.match?(/\A[0-9a-f]{40}\z/) && network_source['targetRevision'] == revision,
      'Mandatory PKI workload and network template revisions must remain immutable and equal')
check(network_source.dig('helm', 'valueFiles') == ['$values/clusters/omv/values.yml'] &&
      network_source.dig('helm', 'parameters', 0) == {'name' => 'component', 'value' => 'openbao-pki-network'} &&
      private_source['repoURL'].end_with?('k3s-on-omv-live.git') && !private_source.key?('path'),
      'PKI NetworkPolicy must render only from the private values ref')
cert_manager_values = YAML.load_file(File.join(ROOT, '02-controllers/cert-manager/values.yml'))
check(cert_manager_values['clusterResourceNamespace'] == 'cert-manager',
      'ClusterIssuer ServiceAccount references must resolve in cert-manager')

root = YAML.load_file(File.join(ROOT, 'infra.yml'))
directory = root.dig('spec', 'source', 'directory')
include_pattern = directory.fetch('include')
exclude_pattern = directory.fetch('exclude')
check(File.fnmatch(include_pattern, application_path, File::FNM_EXTGLOB),
      'Mandatory PKI Application must be explicitly included for bootstrap activation')
check(File.fnmatch(exclude_pattern, application_path, File::FNM_EXTGLOB),
      'Mandatory PKI Application must remain excluded before the ceremony')
discovered = Dir.glob(File.join(ROOT, '*', '*', '{*app.yml,application.yml}'), File::FNM_EXTGLOB)
  .map { |p| p.delete_prefix(ROOT + '/') }
  .select do |path|
    File.fnmatch(include_pattern, path, File::FNM_EXTGLOB) &&
      !File.fnmatch(exclude_pattern, path, File::FNM_EXTGLOB)
  end
check(!discovered.include?(application_path), 'Staged PKI Application became active before bootstrap promotion')
check(File.fnmatch(exclude_pattern, '06-data/postgresql/app.yml', File::FNM_EXTGLOB),
      'Future PostgreSQL Application must remain excluded by default')

output, stderr, result = Open3.capture3('kubectl', 'kustomize', File.join(ROOT, '05-platform/openbao-pki/workload'))
raise "Kustomize failed: #{stderr}" unless result.success?
resources = YAML.load_stream(output).compact
find_all = ->(kind) { resources.select { |resource| resource['kind'] == kind } }
check(find_all.call('Certificate').empty?, 'Certificate consumers are not part of this component')

service_accounts = find_all.call('ServiceAccount').to_h { |resource| [resource.dig('metadata', 'name'), resource] }
%w[openbao-pki-reconciler openbao-pki-services openbao-pki-clients].each do |name|
  check(service_accounts.key?(name), "Missing dedicated identity #{name}")
  check(service_accounts[name]['automountServiceAccountToken'] == false, "#{name} must not automount tokens")
end
check(service_accounts['openbao-pki-reconciler'].dig('metadata', 'namespace') == 'openbao', 'Reconciler must run in openbao')
%w[openbao-pki-services openbao-pki-clients].each do |name|
  check(service_accounts[name].dig('metadata', 'namespace') == 'cert-manager', "#{name} must live in cert-manager")
end

role = find_all.call('Role').fetch(0)
check(role.dig('metadata', 'namespace') == 'cert-manager', 'TokenRequest Role is in the wrong namespace')
rule = role.fetch('rules').fetch(0)
check(rule == {
  'apiGroups' => [''], 'resources' => ['serviceaccounts/token'],
  'resourceNames' => %w[openbao-pki-services openbao-pki-clients], 'verbs' => ['create']
}, 'TokenRequest RBAC is broader than the two issuer identities')
binding = find_all.call('RoleBinding').fetch(0)
check(binding.fetch('subjects') == [{ 'kind' => 'ServiceAccount', 'name' => 'cert-manager', 'namespace' => 'cert-manager' }],
      'TokenRequest permission is not bound only to cert-manager')

issuers = find_all.call('ClusterIssuer').to_h { |resource| [resource.dig('metadata', 'name'), resource] }
check(issuers.keys.sort == %w[openbao-pki-clients openbao-pki-services], 'Expected exactly two Vault ClusterIssuers')
{
  'openbao-pki-services' => ['pki-services/sign/services', 'cert-manager-pki-services', 'openbao-pki-services'],
  'openbao-pki-clients' => ['pki-clients/sign/clients', 'cert-manager-pki-clients', 'openbao-pki-clients']
}.each do |name, (path, auth_role, identity)|
  vault = issuers.fetch(name).dig('spec', 'vault')
  check(vault['path'] == path && vault['server'] == 'http://openbao-active.openbao.svc.cluster.local:8200',
        "Wrong Vault endpoint for #{name}")
  kubernetes = vault.dig('auth', 'kubernetes')
  check(kubernetes == {
    'mountPath' => '/v1/auth/kubernetes', 'role' => auth_role,
    'serviceAccountRef' => { 'name' => identity, 'audiences' => ["vault://#{identity}"] }
  }, "Wrong Kubernetes auth identity/audience for #{name}")
end

config_map = find_all.call('ConfigMap').fetch(0)
config = JSON.parse(config_map.dig('data', 'config.json'))
check(config.fetch('mounts').all? { |entry| entry['namespace'] == 'cert-manager' }, 'Unexpected auth namespace')
check(config.fetch('mounts').map { |entry| entry['audience'] } ==
      %w[vault://openbao-pki-services vault://openbao-pki-clients], 'Issuer audiences are not exact')
check(config.fetch('mounts').map { |entry| entry['allowed_domains'] } ==
      [['svc', 'svc.cluster.local'], ['clients.cluster.local']], 'DNS profiles are too broad')

policies = find_all.call('NetworkPolicy').to_h { |resource| [resource.dig('metadata', 'name'), resource] }
check(policies.keys == ['openbao-pki-reconciler'],
      'The public PKI workload must own only the reconciler policy; cert-manager endpoint peers render from private values')
policy = policies.fetch('openbao-pki-reconciler')
check(policy.dig('metadata', 'namespace') == 'openbao' && policy.dig('spec', 'ingress') == [],
      'Reconciler ingress must be denied')
check(policy.dig('spec', 'podSelector', 'matchLabels') == { 'app.kubernetes.io/name' => 'openbao-pki-reconciler' },
      'NetworkPolicy must select only the reconciler')
ports = policy.dig('spec', 'egress').flat_map { |entry| entry.fetch('ports') }.map { |port| port['port'] }.sort
check(ports == [53, 53, 8200], 'Reconciler egress is not limited to DNS and OpenBao')
[find_all.call('Job').fetch(0), find_all.call('CronJob').fetch(0)].each do |workload|
  pod = workload['kind'] == 'Job' ? workload.dig('spec', 'template') : workload.dig('spec', 'jobTemplate', 'spec', 'template')
  spec = pod.fetch('spec')
  check(spec['serviceAccountName'] == 'openbao-pki-reconciler' && spec['automountServiceAccountToken'] == false,
        'Workload does not use the dedicated identity')
  projected = spec.fetch('volumes').find { |volume| volume['name'] == 'identity' }
  token = projected.dig('projected', 'sources', 0, 'serviceAccountToken')
  check(token['audience'] == 'openbao-pki-reconciler' && token['expirationSeconds'] == 600,
        'Reconciler projected audience/lifetime is wrong')
  container = spec.fetch('containers').fetch(0)
  check(container.fetch('image').include?('@sha256:'), 'Reconciler image must be digest pinned')
  check(container.dig('resources', 'requests', 'cpu') && container.dig('resources', 'limits', 'memory'),
        'Reconciler lacks resource bounds')
  check(container.dig('securityContext', 'readOnlyRootFilesystem') == true &&
        container.dig('securityContext', 'allowPrivilegeEscalation') == false,
        'Reconciler container is not hardened')
end

puts 'PASS: mandatory OpenBao PKI app is pinned and staged inactive; constrained issuers, identities and reconciler render'
