#!/usr/bin/env ruby
# Offline render and optional-registration checks; never contacts a cluster.
require 'yaml'
require 'json'
require 'open3'

ROOT = File.expand_path('..', __dir__)
def check(condition, message)
  raise message unless condition
end

application_path = '05-pki/openbao-pki/application.yml'
app = YAML.load_file(File.join(ROOT, application_path))
check(File.basename(application_path) == 'application.yml', 'Optional Application filename changed')
check(app.dig('metadata', 'name') == 'openbao-pki', 'Wrong optional Application identity')
check(app.dig('spec', 'source', 'path') == '05-pki/openbao-pki/workload', 'Wrong workload source')
revision = app.dig('spec', 'source', 'targetRevision').to_s
check(revision == 'main' || revision.match?(/\A[0-9a-f]{40}\z/), 'Revision must be the staging branch or an immutable commit')
source_text = File.read(File.join(ROOT, application_path))
check(revision != 'main' || source_text.include?('MUST PIN BEFORE REGISTRATION'), 'Missing prominent revision pin gate')
cert_manager_values = YAML.load_file(File.join(ROOT, '02-controllers/cert-manager/values.yml'))
check(cert_manager_values['clusterResourceNamespace'] == 'cert-manager',
      'ClusterIssuer ServiceAccount references must resolve in cert-manager')

root = YAML.load_file(File.join(ROOT, 'infra.yml'))
include_pattern = root.dig('spec', 'source', 'directory', 'include')
check(!File.fnmatch(include_pattern, application_path, File::FNM_EXTGLOB),
      'Optional PKI Application must not be root-discovered')
discovered = Dir.glob(File.join(ROOT, '*', '*', '*app.yml')).map { |p| p.delete_prefix(ROOT + '/') }
  .select { |p| File.fnmatch(include_pattern, p, File::FNM_EXTGLOB) }
check(!discovered.include?(application_path), 'Optional PKI Application entered regular app inventory')

output, stderr, result = Open3.capture3('kubectl', 'kustomize', File.join(ROOT, '05-pki/openbao-pki/workload'))
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
check(policies.keys.sort == %w[cert-manager-openbao-pki-egress openbao-pki-reconciler],
      'Expected exactly the reconciler and cert-manager egress policies')
policy = policies.fetch('openbao-pki-reconciler')
check(policy.dig('metadata', 'namespace') == 'openbao' && policy.dig('spec', 'ingress') == [],
      'Reconciler ingress must be denied')
check(policy.dig('spec', 'podSelector', 'matchLabels') == { 'app.kubernetes.io/name' => 'openbao-pki-reconciler' },
      'NetworkPolicy must select only the reconciler')
ports = policy.dig('spec', 'egress').flat_map { |entry| entry.fetch('ports') }.map { |port| port['port'] }.sort
check(ports == [53, 53, 8200], 'Reconciler egress is not limited to DNS and OpenBao')
controller_policy = policies.fetch('cert-manager-openbao-pki-egress')
check(controller_policy.dig('metadata', 'namespace') == 'cert-manager' &&
      controller_policy.dig('spec', 'policyTypes') == ['Egress'],
      'cert-manager policy must be egress-only')
check(controller_policy.dig('spec', 'podSelector', 'matchLabels') == {
        'app.kubernetes.io/name' => 'cert-manager', 'app.kubernetes.io/component' => 'controller'
      }, 'cert-manager policy selects the wrong pods')
controller_ports = controller_policy.dig('spec', 'egress').flat_map { |entry| entry.fetch('ports') }
  .map { |port| port['port'] }.sort
check(controller_ports == [53, 53, 443, 8200],
      'cert-manager egress must retain only DNS/HTTPS and add OpenBao')

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

puts 'PASS: optional OpenBao PKI app stays outside root inventory; constrained issuers, identities and reconciler render'
