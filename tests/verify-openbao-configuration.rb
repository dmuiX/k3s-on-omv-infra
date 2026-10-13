#!/usr/bin/env ruby
# Offline contracts for shared OpenBao configuration; no cluster access.
require 'yaml'

ROOT = File.expand_path('..', __dir__)

def docs(path)
  YAML.load_stream(File.read(File.join(ROOT, path))).compact
end

def check(condition, message)
  raise message unless condition
end

platform_files = %w[
  secret-engines.yaml
  userpass-auth-method.yaml
  kv-v2-config.yaml
  human-access-policy.yaml
]
resources = platform_files.flat_map do |filename|
  docs("03-core/crossplane/provider-config/#{filename}")
end
identities = resources.map { |item| [item['apiVersion'], item['kind'], item.dig('metadata', 'name')] }
expected_identities = [
  ['vault.vault.upbound.io/v1alpha1', 'Mount', 'kv'],
  ['vault.vault.upbound.io/v1alpha1', 'Mount', 'database'],
  ['vault.vault.upbound.io/v1alpha1', 'Mount', 'pki-root'],
  ['vault.vault.upbound.io/v1alpha1', 'Mount', 'pki-services'],
  ['vault.vault.upbound.io/v1alpha1', 'Mount', 'pki-clients'],
  ['auth.vault.upbound.io/v1alpha1', 'Backend', 'userpass'],
  ['kv.vault.upbound.io/v1alpha1', 'SecretBackendV2', 'kv-config'],
  ['vault.vault.upbound.io/v1alpha1', 'Policy', 'human-admin']
]
check(identities == expected_identities, 'Stage-1 shared observation inventory changed')

resources.each do |item|
  spec = item.fetch('spec')
  check(spec['managementPolicies'] == ['Observe'], "#{item['kind']}/#{item.dig('metadata', 'name')} is not observe-only")
  check((spec['managementPolicies'] & %w[Create Update Delete LateInitialize *]).empty?,
        "#{item['kind']}/#{item.dig('metadata', 'name')} permits mutation")
  check(spec['deletionPolicy'] == 'Orphan', "#{item['kind']}/#{item.dig('metadata', 'name')} does not orphan external state")
  check(spec.dig('providerConfigRef', 'name') == 'openbao', 'Observer must use the central OpenBao ProviderConfig')
  external_name = item.dig('metadata', 'annotations', 'crossplane.io/external-name')
  check(external_name.is_a?(String) && !external_name.empty?, 'Every observed object needs an exact external name')
end

by_name = resources.to_h { |item| [[item['kind'], item.dig('metadata', 'name')], item] }
mounts = %w[kv database pki-root pki-services pki-clients].to_h do |name|
  item = by_name.fetch(['Mount', name])
  [name, item]
end
mounts.each do |name, item|
  check(item.dig('metadata', 'annotations', 'crossplane.io/external-name') == name &&
        item.dig('spec', 'forProvider', 'path') == name,
        "Mount/#{name} must bind the exact Bootstrap path")
end
check(mounts['kv'].dig('spec', 'forProvider') == {'path' => 'kv', 'type' => 'kv', 'options' => {'version' => '2'}},
      'KV mount must retain the Bootstrap KV-v2 contract')
check(mounts['database'].dig('spec', 'forProvider') == {'path' => 'database', 'type' => 'database'},
      'Database mount contract changed')
check(mounts['pki-root'].dig('spec', 'forProvider', 'type') == 'pki' &&
      mounts['pki-root'].dig('spec', 'forProvider', 'maxLeaseTtlSeconds') == 315_360_000,
      'Root PKI mount must retain its ten-year maximum TTL')
%w[pki-services pki-clients].each do |name|
  check(mounts[name].dig('spec', 'forProvider', 'type') == 'pki' &&
        mounts[name].dig('spec', 'forProvider', 'maxLeaseTtlSeconds') == 157_680_000,
        "#{name} must retain its five-year maximum TTL")
end

backend = by_name.fetch(['Backend', 'userpass'])
check(backend.dig('metadata', 'annotations', 'crossplane.io/external-name') == 'userpass' &&
      backend.dig('spec', 'forProvider') == {'path' => 'userpass', 'type' => 'userpass'},
      'Human auth observer must bind the exact userpass backend')
kv_config = by_name.fetch(['SecretBackendV2', 'kv-config'])
check(kv_config.dig('metadata', 'annotations', 'crossplane.io/external-name') == 'kv/config' &&
      kv_config.dig('spec', 'forProvider') == {'mount' => 'kv'},
      'KV backend observer must use the provider import ID kv/config')

expected_policy = <<~HCL
  # Personal administrator only; not for the webhook or configuration Job.
  # This policy is highly privileged and can grant further permissions.
  path "sys/mounts"          { capabilities = ["read"] }
  path "sys/mounts/*"        { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
  path "sys/auth"            { capabilities = ["read"] }
  path "sys/auth/*"          { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
  path "sys/policies/acl"    { capabilities = ["list"] }
  path "sys/policies/acl/*"  { capabilities = ["create", "read", "update", "delete", "list"] }
  path "auth/*"             { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
  path "identity/*"         { capabilities = ["create", "read", "update", "delete", "list", "sudo"] }
  path "kv/*"               { capabilities = ["create", "read", "update", "delete", "list", "patch"] }
HCL
policy = by_name.fetch(['Policy', 'human-admin'])
check(policy.dig('metadata', 'annotations', 'crossplane.io/external-name') == 'human-admin' &&
      policy.dig('spec', 'forProvider') == {'name' => 'human-admin', 'policy' => expected_policy},
      'Human administrator policy must match the non-secret Bootstrap source')

# The provider cannot safely observe the policy or Kubernetes auth role that
# grants its own access. Those remain entirely outside its managed-resource set.
external_names = resources.map { |item| item.dig('metadata', 'annotations', 'crossplane.io/external-name') }
check(!external_names.include?('crossplane-provider-vault'), 'Provider must not observe its own policy or auth role')
check(resources.none? { |item| item['kind'] == 'AuthBackendRole' }, 'Provider auth role crosses the self-management boundary')
check(resources.none? { |item| item['kind'] == 'Backend' && item.dig('spec', 'forProvider', 'path') == 'kubernetes' },
      'Provider must not observe its own Kubernetes authentication backend')

provider_config = docs('03-core/crossplane/provider-config/config.yaml').first
check(provider_config.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '-1',
      'ProviderConfig must precede observe-only managed resources in its Application')

kustomization = docs('03-core/crossplane/provider-config/kustomization.yaml').first
check(kustomization.fetch('resources') == ['config.yaml', *platform_files],
      'Central provider-vault-config source must select the durable platform resources')
app = docs('05-platform/crossplane/config-app.yml').first
source_paths = app.dig('spec', 'sources').map { |source| source['path'] }
check(source_paths == %w[03-core/crossplane/provider-config 04-secrets/vault-secrets-webhook/openbao-access],
      'Provider-ready observers must stay under the existing provider-vault-config Application')

puts 'PASS: OpenBao shared objects are exact observe-only Orphan bindings with a provider self-boundary'
