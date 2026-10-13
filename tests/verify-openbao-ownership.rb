#!/usr/bin/env ruby
# Offline ownership and exact-contract checks for consumer OpenBao access.
require 'yaml'

ROOT = File.expand_path('..', __dir__)
POLICY_GVK = ['vault.vault.upbound.io/v1alpha1', 'Policy'].freeze
AUTH_GVK = ['kubernetes.vault.upbound.io/v1alpha1', 'AuthBackendRole'].freeze
PKI_GVK = ['pki.vault.upbound.io/v1alpha1', 'SecretBackendRole'].freeze

OWNERS = {
  '05-platform/crossplane/config-app.yml' => %w[04-secrets/vault-secrets-webhook/openbao-access 04-secrets/vault-secrets-webhook/openbao-access],
  '06-data/openbao-backups/config-app.yml' => %w[06-data/openbao-backups/openbao-access 06-data/openbao-backups/openbao-access],
  '05-platform/openbao-pki/application.yml' => %w[05-platform/openbao-pki/workload 05-platform/openbao-pki/workload/openbao-access],
  '06-data/cloudnative-pg/app.yml' => %w[06-data/cloudnative-pg/openbao-access 06-data/cloudnative-pg/openbao-access],
  '07-applications/radar/app.yml' => %w[07-applications/radar/openbao-access 07-applications/radar/openbao-access],
  '08-monitoring/kube-prometheus-stack/app.yml' => %w[08-monitoring/kube-prometheus-stack/openbao-access 08-monitoring/kube-prometheus-stack/openbao-access]
}.freeze

MANIFEST_FILES = OWNERS.values.map(&:last).uniq.to_h do |owner_path|
  files = if owner_path == '05-platform/openbao-pki/workload/openbao-access'
            %w[policies.yaml kubernetes-auth-roles.yaml certificate-issuance-roles.yaml]
          else
            %w[policy.yaml kubernetes-auth-role.yaml]
          end
  [owner_path, files]
end.freeze

POLICIES = {
  'grafana-vso-read' => <<~HCL,
    path "auth/token/renew-self" {
      capabilities = ["update"]
    }

    path "database/static-creds/grafana" {
      capabilities = ["read"]
    }
  HCL
  'radar-vso-read' => <<~HCL,
    path "auth/token/renew-self" {
      capabilities = ["update"]
    }

    path "database/static-creds/radar" {
      capabilities = ["read"]
    }
  HCL
  'postgresql-vso-read' => <<~HCL,
    path "auth/token/renew-self" {
      capabilities = ["update"]
    }

    path "kv/data/postgresql/openbao-manager" {
      capabilities = ["read"]
    }

    path "kv/metadata/postgresql/openbao-manager" {
      capabilities = ["read"]
    }

    path "kv/data/postgresql/r2-credentials" {
      capabilities = ["read"]
    }

    path "kv/metadata/postgresql/r2-credentials" {
      capabilities = ["read"]
    }
  HCL
  'vault-secrets-webhook-read' => <<~HCL,
    # Deliberately broad: the webhook may read every secret in the existing KV v2
    # mount. Anyone able to submit and read a matching labelled Kubernetes Secret
    # can request any field from kv/data/* through the webhook. Review before use.
    path "kv/data/*" {
      capabilities = ["read"]
    }
  HCL
  'openbao-snapshot' => <<~HCL,
    # Native Raft backup plus only the dedicated R2 credential object.
    path "sys/storage/raft/snapshot" {
      capabilities = ["read"]
    }

    path "kv/data/openbao-snapshots/r2-credentials" {
      capabilities = ["read"]
    }
  HCL
  'cert-manager-pki-services-sign' => <<~HCL.chomp,
    path "pki-services/sign/services" {
      capabilities = ["update"]
    }
  HCL
  'cert-manager-pki-clients-sign' => <<~HCL.chomp
    path "pki-clients/sign/clients" {
      capabilities = ["update"]
    }
  HCL
}.freeze

AUTH_ROLES = {
  'grafana-vso' => ['grafana-vso', 'kube-prometheus-stack', '', 'grafana-vso-read', 3600, true],
  'radar-vso' => ['radar-vso', 'radar', '', 'radar-vso-read', 3600, true],
  'postgresql-vso' => ['postgresql-vso', 'cloudnative-pg', '', 'postgresql-vso-read', 3600, true],
  'vault-secrets-webhook' => ['vault-secrets-webhook', 'vault-secrets-webhook', '', 'vault-secrets-webhook-read', 3600, false],
  'openbao-snapshot' => ['openbao-snapshot', 'openbao', '', 'openbao-snapshot', 900, true],
  'cert-manager-pki-services' => ['openbao-pki-services', 'cert-manager', 'vault://openbao-pki-services',
                                  'cert-manager-pki-services-sign', 3600, true],
  'cert-manager-pki-clients' => ['openbao-pki-clients', 'cert-manager', 'vault://openbao-pki-clients',
                                 'cert-manager-pki-clients-sign', 3600, true]
}.freeze

PKI_ROLES = {
  'services' => ['pki-services', ['svc', 'svc.cluster.local'], true, false],
  'clients' => ['pki-clients', ['clients.cluster.local'], false, true]
}.freeze

def documents(path)
  YAML.load_stream(File.read(File.join(ROOT, path))).compact
end

def check(condition, message)
  raise message unless condition
end

def app_sources(app)
  app.dig('spec', 'sources') || [app.dig('spec', 'source')].compact
end

resources = []
OWNERS.each do |app_path, paths|
  source_path, owner_path = paths
  app = documents(app_path).fetch(0)
  owned_sources = app_sources(app).select { |source| source['path'] == source_path }
  check(owned_sources.length == 1 && owned_sources.first['targetRevision'] == 'main',
        "#{app_path} must select #{source_path} exactly once from main")

  manifest_files = MANIFEST_FILES.fetch(owner_path)
  kustomization = documents("#{owner_path}/kustomization.yaml").fetch(0)
  check(kustomization == {
          'apiVersion' => 'kustomize.config.k8s.io/v1beta1',
          'kind' => 'Kustomization',
          'resources' => manifest_files
        }, "#{owner_path} must expose its named access manifests")
  manifest_files.each do |filename|
    documents("#{owner_path}/#{filename}").each do |resource|
      resource['_owner_path'] = owner_path
      resources << resource
    end
  end
end

check(resources.length == 16, "Expected 16 observed resources, found #{resources.length}")
identities = resources.map { |item| [item['apiVersion'], item['kind'], item.dig('metadata', 'name')] }
check(identities.uniq.length == identities.length, 'Managed resources contain a duplicate GVK/name')
check(resources.none? { |item| item.dig('metadata', 'name').to_s.include?('crossplane-provider-vault') ||
                               item.dig('spec', 'forProvider', 'name') == 'crossplane-provider-vault' ||
                               item.dig('spec', 'forProvider', 'roleName') == 'crossplane-provider-vault' },
      'Provider self-policy or self-role must remain excluded')

resources.each do |item|
  spec = item.fetch('spec')
  check(spec['managementPolicies'] == ['Observe'], "#{item['kind']}/#{item.dig('metadata', 'name')} is not Observe-only")
  check(spec['deletionPolicy'] == 'Orphan', "#{item['kind']}/#{item.dig('metadata', 'name')} is not Orphan")
  check(spec['providerConfigRef'] == {'name' => 'openbao'},
        "#{item['kind']}/#{item.dig('metadata', 'name')} has the wrong ProviderConfig")
  external_name = item.dig('metadata', 'annotations', 'crossplane.io/external-name')
  check(external_name.is_a?(String) && !external_name.empty?,
        "#{item['kind']}/#{item.dig('metadata', 'name')} lacks an exact external name")
end

policies = resources.select { |item| [item['apiVersion'], item['kind']] == POLICY_GVK }
check(policies.map { |item| item.dig('metadata', 'name') }.sort == POLICIES.keys.sort,
      'Observed ACL policy inventory differs from the consumer-owned contract')
policies.each do |item|
  name = item.dig('metadata', 'name')
  check(item.dig('metadata', 'annotations', 'crossplane.io/external-name') == name &&
        item.dig('spec', 'forProvider') == {'name' => name, 'policy' => POLICIES.fetch(name)},
        "Policy/#{name} differs from the exact name or HCL contract")
end

auth_roles = resources.select { |item| [item['apiVersion'], item['kind']] == AUTH_GVK }
check(auth_roles.map { |item| item.dig('metadata', 'name') }.sort == AUTH_ROLES.keys.sort,
      'Observed Kubernetes auth-role inventory differs from the consumer-owned contract')
auth_roles.each do |item|
  name = item.dig('metadata', 'name')
  service_account, namespace, audience, policy, ttl, no_default = AUTH_ROLES.fetch(name)
  expected = {
    'backend' => 'kubernetes', 'roleName' => name,
    'boundServiceAccountNames' => [service_account],
    'boundServiceAccountNamespaces' => [namespace], 'audience' => audience,
    'tokenPolicies' => [policy], 'tokenTtl' => ttl, 'tokenNoDefaultPolicy' => no_default
  }
  if name.start_with?('cert-manager-pki-')
    expected.merge!('aliasNameSource' => 'serviceaccount_uid', 'tokenMaxTtl' => 3600,
                    'tokenExplicitMaxTtl' => 0, 'tokenPeriod' => 0, 'tokenNumUses' => 0,
                    'tokenBoundCidrs' => [], 'tokenType' => 'default')
  end
  check(item.dig('metadata', 'annotations', 'crossplane.io/external-name') == "auth/kubernetes/role/#{name}" &&
        item.dig('spec', 'forProvider') == expected,
        "AuthBackendRole/#{name} differs from the exact binding, audience, policy, or TTL contract")
end

pki_roles = resources.select { |item| [item['apiVersion'], item['kind']] == PKI_GVK }
check(pki_roles.map { |item| item.dig('metadata', 'name') }.sort == PKI_ROLES.keys.sort,
      'Observed PKI leaf-role inventory differs from the consumer-owned contract')
pki_roles.each do |item|
  name = item.dig('metadata', 'name')
  backend, domains, server, client = PKI_ROLES.fetch(name)
  common = {
    'backend' => backend, 'name' => name, 'issuerRef' => 'default', 'allowedDomains' => domains,
    'allowedDomainsTemplate' => false, 'allowSubdomains' => true, 'allowBareDomains' => false,
    'allowGlobDomains' => false, 'allowWildcardCertificates' => false, 'allowAnyName' => false,
    'enforceHostnames' => true, 'allowIpSans' => false, 'allowedUriSans' => [],
    'allowedUriSansTemplate' => false, 'allowedOtherSans' => [], 'allowedSerialNumbers' => [],
    'allowedUserIds' => [], 'allowLocalhost' => false, 'cnValidations' => ['hostname'],
    'serverFlag' => server, 'clientFlag' => client, 'codeSigningFlag' => false,
    'emailProtectionFlag' => false, 'keyType' => 'rsa', 'keyBits' => 2048,
    'keyUsage' => ['DigitalSignature', 'KeyEncipherment'], 'extKeyUsage' => [],
    'extKeyUsageOids' => [], 'policyIdentifiers' => [], 'ou' => [], 'organization' => [],
    'country' => [], 'locality' => [], 'province' => [], 'streetAddress' => [], 'postalCode' => [],
    'basicConstraintsValidForNonCa' => false, 'ttl' => '7776000', 'maxTtl' => '7776000',
    'notBeforeDuration' => '30', 'requireCn' => false, 'useCsrCommonName' => true,
    'useCsrSans' => true, 'usePss' => false, 'signatureBits' => 0,
    'generateLease' => false, 'noStore' => false
  }
  check(item.dig('metadata', 'annotations', 'crossplane.io/external-name') == "#{backend}/roles/#{name}" &&
        item.dig('spec', 'forProvider') == common,
        "SecretBackendRole/#{name} differs from the exact domain, usage, key, or TTL contract")
end

expected_owners = {
  '04-secrets/vault-secrets-webhook/openbao-access' => %w[vault-secrets-webhook-read vault-secrets-webhook],
  '06-data/openbao-backups/openbao-access' => %w[openbao-snapshot openbao-snapshot],
  '05-platform/openbao-pki/workload/openbao-access' => %w[cert-manager-pki-services-sign cert-manager-pki-services services cert-manager-pki-clients-sign cert-manager-pki-clients clients],
  '06-data/cloudnative-pg/openbao-access' => %w[postgresql-vso-read postgresql-vso],
  '07-applications/radar/openbao-access' => %w[radar-vso-read radar-vso],
  '08-monitoring/kube-prometheus-stack/openbao-access' => %w[grafana-vso-read grafana-vso]
}
expected_owners.each do |path, names|
  actual = resources.select { |item| item['_owner_path'] == path }.map { |item| item.dig('metadata', 'name') }
  check(actual.sort == names.sort, "#{path} owns the wrong OpenBao objects")
end

puts 'PASS: 16 consumer-owned OpenBao resources are exact, Observe-only, Orphaned, unique, and provider-self-excluding'
