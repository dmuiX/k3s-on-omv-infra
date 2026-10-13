#!/usr/bin/env ruby
# Offline OpenBao/PostgreSQL contract checks; no cluster or network access.
require 'open3'
require 'yaml'

ROOT = File.expand_path('..', __dir__)
LIVE_VALUES = File.expand_path('../k3s-on-omv-live/clusters/omv/values.yml', ROOT)
def docs(path)
  YAML.load_stream(File.read(File.join(ROOT, path))).compact
end
def helm_docs(path, values)
  output, error, status = Open3.capture3('helm', 'template', 'radar-platform',
                                         File.join(ROOT, path), '--values', values)
  raise error unless status.success?
  YAML.load_stream(output).compact
end

def assert(value, message)
  raise message unless value
end

def resource(items, kind, name)
  items.find { |item| item['kind'] == kind && item.dig('metadata', 'name') == name } ||
    raise("Missing #{kind}/#{name}")
end

provider = docs('03-core/crossplane/workload/provider.yaml')
package = resource(provider, 'Provider', 'provider-vault')
assert(package.dig('spec', 'package') == 'xpkg.upbound.io/upbound/provider-vault:v4.0.4', 'Wrong provider package')
runtime = resource(provider, 'DeploymentRuntimeConfig', 'provider-vault')
assert(runtime.dig('spec', 'deploymentTemplate', 'spec', 'template', 'spec', 'serviceAccountName') == 'provider-vault',
       'Provider must use an explicit runtime ServiceAccount')
assert(runtime.dig('spec', 'deploymentTemplate', 'spec', 'template', 'spec', 'containers') ==
       [{'name' => 'package-runtime'}],
       'Crossplane v2 runtime config must declare the generated provider container merge key')
assert(runtime.dig('spec', 'deploymentTemplate', 'spec', 'template', 'metadata', 'labels',
                   'bootstrap.k3s-on-omv.dev/openbao-postgres-client') == 'true',
       'Provider PostgreSQL network identity must be explicit')
provider_app = docs('03-core/crossplane/app.yml').first
provider_workload_source = provider_app.dig('spec', 'sources').find do |source|
  source['path'] == '03-core/crossplane/workload'
end
assert(provider_workload_source && provider_workload_source['targetRevision'] == 'main',
       'Provider workload must track the Infra main branch')
config_app = docs('05-platform/crossplane/config-app.yml').first
assert(config_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '5',
       'ProviderConfig must follow provider install')
config = docs('03-core/crossplane/provider-config/config.yaml').first
assert(config['apiVersion'] == 'vault.upbound.io/v1beta1' && config['kind'] == 'ProviderConfig',
       'Wrong cluster-scoped ProviderConfig GVK')
assert(config.dig('spec', 'credentials', 'source') == 'Kubernetes' &&
       config.dig('spec', 'role') == 'crossplane-provider-vault' &&
       config.dig('spec', 'mountPath') == 'kubernetes' &&
       !config.dig('spec', 'credentials').key?('kubernetes'), 'Wrong ProviderConfig auth')
assert(config.dig('spec', 'address') == 'http://openbao-active.openbao.svc.cluster.local:8200',
       'Wrong OpenBao listener')

resources = docs('06-data/cloudnative-pg/databases/grafana-openbao.yaml')
connection = resource(resources, 'SecretBackendConnection', 'postgresql')
static_role = resource(resources, 'SecretBackendStaticRole', 'grafana')
manager = resource(resources, 'DatabaseRole', 'openbao-manager')
assert [connection, static_role].all? { |item| item['apiVersion'] == 'database.vault.upbound.io/v1alpha1' },
       'Wrong provider-vault database API'
pg = connection.dig('spec', 'forProvider', 'postgresql', 0)
assert pg['username'] == 'openbao_manager' &&
       pg.dig('passwordSecretRef', 'name') == 'openbao-manager-credentials' &&
       pg.dig('passwordSecretRef', 'key') == 'password', 'Manager credential reference is incomplete'
assert connection.dig('spec', 'forProvider', 'backend') == 'database' &&
       connection.dig('spec', 'forProvider', 'name') == 'k3s-postgres' &&
       !connection.dig('spec', 'forProvider').key?('path'), 'Connection must use the exact provider-vault backend/name schema'
assert static_role.dig('spec', 'forProvider', 'rotationPeriod') == 2_592_000,
       'Static role rotation must be a 30-day period in seconds'
assert [connection, static_role].all? { |item| item.dig('spec', 'deletionPolicy') == 'Orphan' },
       'OpenBao resources must orphan external state on deletion'
assert manager.dig('spec', 'passwordSecret', 'name') == 'openbao-manager-credentials' &&
       manager.dig('spec', 'login') == true && manager.dig('spec', 'createrole') == true &&
       manager.dig('spec', 'superuser') == false && manager.dig('spec', 'createdb') == false,
       'CNPG manager role must use its VSO credential with least privileges'
bootstrap_config = YAML.load_file(File.join(ROOT, '../k3s-on-omv-bootstrap/ansible/roles/gitops_bootstrap/files/openbao-access/config.json'))
grafana_identity = bootstrap_config.fetch('roles').find { |item| item['name'] == 'grafana-vso' }
radar_identity = bootstrap_config.fetch('roles').find { |item| item['name'] == 'radar-vso' }
assert grafana_identity && grafana_identity['service_account'] == 'grafana-vso' &&
       grafana_identity['namespace'] == 'kube-prometheus-stack' &&
       grafana_identity['policies'] == ['grafana-vso-read'] &&
       radar_identity && radar_identity['service_account'] == 'radar-vso' &&
       radar_identity['namespace'] == 'radar' && radar_identity['policies'] == ['radar-vso-read'],
       'Ansible trust bootstrap must bind each VSO identity to its own database policy and namespace'
assert File.read(File.join(ROOT, '../k3s-on-omv-bootstrap/ansible/roles/gitops_bootstrap/files/openbao-access/policy_files/grafana-vso-read.hcl')).include?('database/static-creds/grafana') &&
       File.read(File.join(ROOT, '../k3s-on-omv-bootstrap/ansible/roles/gitops_bootstrap/files/openbao-access/policy_files/radar-vso-read.hcl')).include?('database/static-creds/radar'),
       'VSO read policies must be limited to one static role each'
provider_policy = File.read(File.join(ROOT, '../k3s-on-omv-bootstrap/ansible/roles/gitops_bootstrap/files/openbao-access/policy_files/crossplane-provider-vault.hcl'))
provider_policy.scan(/path\s+"([^"]+)"\s*\{(.*?)\}/m).each do |path, body|
  next unless path.start_with?('sys/policies/acl') || path.start_with?('auth/kubernetes/role')

  assert (body.scan(/"([^"]+)"/).flatten & %w[create update delete patch sudo]).empty?,
         "Crossplane provider must not administer policy or auth-role path #{path}"
end
openbao_values = YAML.load_file(File.join(ROOT, '03-core/openbao/values.yml'))
ca_volume = openbao_values.dig('server', 'volumes').find { |volume| volume['name'] == 'postgresql-ca' }
ca_mount = openbao_values.dig('server', 'volumeMounts').find { |mount| mount['name'] == 'postgresql-ca' }
assert pg['connectionUrl'].include?('sslmode=verify-full') &&
       pg['connectionUrl'].include?('sslrootcert=/openbao/postgresql-tls/ca.crt') &&
       ca_volume&.dig('secret', 'secretName') == 'postgresql-ca' &&
       ca_volume&.dig('secret', 'optional') == true &&
       ca_mount&.dig('mountPath') == '/openbao/postgresql-tls' &&
       !File.read(File.join(ROOT, '06-data/cloudnative-pg/databases/grafana-openbao.yaml')).include?('${'),
       'OpenBao must verify the PostgreSQL service name against its mounted internal CA'

radar = docs('06-data/cloudnative-pg/databases/radar-openbao.yaml')
radar_static_role = resource(radar, 'SecretBackendStaticRole', 'radar')
assert radar_static_role.dig('spec', 'forProvider', 'rotationPeriod') == 2_592_000 &&
       radar_static_role.dig('spec', 'forProvider', 'rotationStatements', 0).include?('CREATE ROLE radar'),
       'Radar credentials must use a self-provisioning OpenBao static role'
assert docs('06-data/cloudnative-pg/databases/radar.yaml').none? { |item| item['kind'] == 'DatabaseRole' },
       'CNPG must not own the OpenBao-managed Radar login'

vso_operator_app = docs('04-secrets/vault-secrets-operator/app.yml').first
assert vso_operator_app.dig('spec', 'sources').length == 1 &&
       vso_operator_app.dig('spec', 'sources', 0, 'chart') == 'vault-secrets-operator',
       'The central VSO Application must own only the operator'
gitops_vso = docs('06-data/cloudnative-pg/credentials/vso.yaml')
assert(gitops_vso.select { |item| item['kind'] == 'VaultConnection' }.all? do |item|
  item.dig('spec', 'address') == 'http://openbao-active.openbao.svc.cluster.local:8200'
end, 'VSO must use the OpenBao in-cluster listener')
bootstrap_static_secrets = gitops_vso.select { |item| item['kind'] == 'VaultStaticSecret' }
assert gitops_vso.all? { |item| item.dig('metadata', 'namespace') == 'cloudnative-pg' } &&
       gitops_vso.all? { |item| item.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '0' } &&
       bootstrap_static_secrets.any? { |item| item.dig('spec', 'path') == 'postgresql/openbao-manager' } &&
       bootstrap_static_secrets.any? { |item| item.dig('spec', 'path') == 'postgresql/r2-credentials' } &&
       bootstrap_static_secrets.all? { |item| item.dig('spec', 'destination', 'transformation', 'excludeRaw') == true },
       'CloudNativePG must own its namespace-local VSO prerequisites without the raw payload key'
monitoring_app = docs('08-monitoring/kube-prometheus-stack/app.yml').first
radar_app = docs('07-applications/radar/app.yml').first
assert monitoring_app.dig('spec', 'sources').any? { |source|
         source['path'] == '08-monitoring/kube-prometheus-stack/workload'
       } && radar_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '7' &&
       radar_app.dig('spec', 'sources').any? { |source| source['path'] == '07-applications/radar' },
       'Applications must own their namespace-local PostgreSQL credentials'
grafana_credentials = docs('08-monitoring/kube-prometheus-stack/workload/postgresql-vso.yaml')
radar_resources = helm_docs('07-applications/radar', LIVE_VALUES)
radar_credentials = radar_resources
radar_oidc = radar_resources
oidc_key = resource(radar_oidc, 'OidcKey', 'radar')
oidc_client = resource(radar_oidc, 'OidcClient', 'radar')
oidc_provider = resource(radar_oidc, 'OidcProvider', 'radar')
private_routes = YAML.load_file(LIVE_VALUES).fetch('routes')
radar_callback = "https://#{private_routes.fetch('radar').fetch('hostname')}/auth/callback"
assert oidc_key.dig('spec', 'forProvider', 'algorithm') == 'RS256' &&
       oidc_client.dig('spec', 'forProvider', 'redirectUris') == [radar_callback] &&
       oidc_client.dig('spec', 'writeConnectionSecretToRef') == {'name' => 'radar-oidc-client', 'namespace' => 'radar'} &&
       oidc_provider.dig('spec', 'forProvider', 'issuerHost') == private_routes.fetch('openbao').fetch('hostname') &&
       oidc_provider.dig('spec', 'forProvider', 'allowedClientIdsRefs') == [{'name' => 'radar'}],
       'Radar must own its bounded OpenBao OIDC provider and generated client secret'
radar_role = resource(radar_resources, 'ClusterRole', 'radar')
radar_rules = radar_role.dig('rules')
assert radar_rules.none? { |rule| Array(rule['resources']).include?('secrets') } &&
       radar_rules.none? { |rule| (Array(rule['verbs']) & %w[delete patch update]).any? } &&
       resource(radar_resources, 'ClusterRoleBinding', 'radar-view').dig('subjects', 0, 'name') == 'radar:view',
       'Radar must use read-only application RBAC without Secret access'
radar_auth_secret = resource(radar_resources, 'Secret', 'radar-auth')
assert radar_auth_secret.dig('metadata', 'annotations', 'helm.sh/resource-policy') == 'keep' &&
       radar_auth_secret.dig('data', 'auth-secret').to_s.length > 40,
       'Radar must generate and retain an independent session-signing secret'
grafana_vds = resource(grafana_credentials, 'VaultDynamicSecret', 'grafana-postgresql')
assert grafana_vds.dig('spec', 'allowStaticCreds') == true &&
       grafana_vds.dig('spec', 'path') == 'static-creds/grafana' &&
       grafana_vds.dig('spec', 'destination', 'name') == 'grafana-db-credentials' &&
       grafana_vds.dig('spec', 'rolloutRestartTargets', 0, 'name') == 'kube-prometheus-stack-grafana',
       'VSO must materialize Grafana static credentials and restart Grafana on rotation'
radar_vds = resource(radar_credentials, 'VaultDynamicSecret', 'radar-postgresql')
grafana_auth = resource(grafana_credentials, 'VaultAuth', 'openbao-grafana')
radar_auth = resource(radar_credentials, 'VaultAuth', 'openbao-radar')
assert grafana_vds.dig('spec', 'allowStaticCreds') == true &&
       grafana_vds.dig('spec', 'destination', 'name') == 'grafana-db-credentials' &&
       grafana_auth.dig('spec', 'kubernetes', 'role') == grafana_identity['name'],
       'VSO must materialize Grafana credentials using its bootstrapped OpenBao role'
assert radar_vds.dig('spec', 'allowStaticCreds') == true &&
       radar_vds.dig('spec', 'destination', 'name') == 'radar-db-credentials' &&
       radar_vds.dig('spec', 'destination', 'transformation', 'templates', 'dsn', 'text').include?(
         'k3s-postgres-rw.cloudnative-pg.svc:5432/radar?sslmode=require') &&
       radar_vds.dig('spec', 'rolloutRestartTargets', 0, 'name') == 'radar' &&
       radar_auth.dig('spec', 'kubernetes', 'role') == radar_identity['name'],
       'VSO must materialize Radar credentials using its bootstrapped OpenBao role'
assert [grafana_vds, radar_vds].all? { |item|
  item.dig('spec', 'destination', 'transformation', 'excludeRaw') == true
}, 'Application credential Secrets must omit the VSO raw payload key'
monitoring_crds = docs('01-bootstrap/monitoring-crds/app.yml').first
assert monitoring_crds.dig('spec', 'syncPolicy', 'syncOptions').include?('CreateNamespace=true'),
       'The Grafana VSO ServiceAccount namespace must exist before monitoring reconciliation'

crossplane_app = docs('03-core/crossplane/app.yml').first
vso_app = docs('04-secrets/vault-secrets-operator/app.yml').first
postgresql_app = docs('06-data/cloudnative-pg/app.yml').first
assert crossplane_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '3' &&
       vso_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '4' &&
       postgresql_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '6' &&
       monitoring_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '8',
       'Crossplane, VSO, PostgreSQL and monitoring rollout dependencies changed'

values = YAML.load_file(File.join(ROOT, '03-core/kube-prometheus-stack/values.yml'))
grafana_values = values.fetch('grafana')
assert grafana_values.dig('grafana.ini', 'database', 'type') == 'postgres' &&
       grafana_values.dig('grafana.ini', 'database', 'host') == 'k3s-postgres-rw.cloudnative-pg.svc:5432' &&
       grafana_values.dig('grafana.ini', 'database', 'ssl_mode') == 'require' &&
       grafana_values.dig('grafana.ini', 'database', 'ca_cert_path').nil? &&
       grafana_values['extraVolumes'].nil? && grafana_values['extraVolumeMounts'].nil? &&
       grafana_values.dig('envValueFrom', 'GF_DATABASE_PASSWORD', 'secretKeyRef', 'name') == 'grafana-db-credentials',
       'Grafana must consume its OpenBao-managed credential over encrypted TLS without a cross-namespace Secret reference'

puts 'PASS: OpenBao manager, Crossplane static roles, VSO rotation and automated wave contracts'
