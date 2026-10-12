#!/usr/bin/env ruby
# Offline OpenBao/PostgreSQL contract checks; no cluster or network access.
require 'yaml'

ROOT = File.expand_path('..', __dir__)
def docs(path)
  YAML.load_stream(File.read(File.join(ROOT, path))).compact
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
assert(runtime.dig('spec', 'deploymentTemplate', 'spec', 'template', 'metadata', 'labels',
                   'bootstrap.k3s-on-omv.dev/openbao-postgres-client') == 'true',
       'Provider PostgreSQL network identity must be explicit')
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

resources = docs('06-data/postgresql/databases/grafana-openbao.yaml')
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
       connection.dig('spec', 'forProvider', 'name') == 'platform-postgres' &&
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
assert !provider_policy.include?('sys/policies/acl') && !provider_policy.include?('auth/kubernetes/role'),
       'Crossplane provider must not receive policy or auth-role administration privileges'
assert pg['connectionUrl'].include?('sslmode=require') &&
       !File.read(File.join(ROOT, '06-data/postgresql/databases/grafana-openbao.yaml')).include?('${'),
       'PostgreSQL transport must be encrypted and no shell-style secret interpolation is permitted'

radar = docs('06-data/postgresql/databases/radar-openbao.yaml')
assert resource(radar, 'SecretBackendStaticRole', 'radar').dig('spec', 'forProvider', 'rotationPeriod') == 2_592_000,
       'Radar credentials must use a separate OpenBao static role'
radar_db_role = resource(docs('06-data/postgresql/databases/radar.yaml'), 'DatabaseRole', 'radar')
assert !radar_db_role.dig('spec').key?('passwordSecret'), 'CNPG must not own Radar password rotation'

gitops_vso = docs('04-secrets/vault-secrets-operator/workload/resources.yaml')
assert(gitops_vso.select { |item| item['kind'] == 'VaultConnection' }.all? do |item|
  item.dig('spec', 'address') == 'http://openbao-active.openbao.svc.cluster.local:8200'
end, 'VSO must use the OpenBao in-cluster listener')
assert gitops_vso.any? { |item| item['kind'] == 'Namespace' && item.dig('metadata', 'name') == 'postgresql' } &&
       gitops_vso.any? { |item| item['kind'] == 'VaultStaticSecret' &&
         item.dig('spec', 'path') == 'postgresql/openbao-manager' } &&
       gitops_vso.any? { |item| item['kind'] == 'VaultStaticSecret' &&
         item.dig('spec', 'path') == 'postgresql/r2-credentials' },
       'Wave-4 VSO prerequisites must establish PostgreSQL namespace and protected inputs'
credential_app = docs('07-secrets/postgresql-vso/app.yml').first
assert credential_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '7',
       'Per-application credentials must reconcile after PostgreSQL roles'
credentials = docs('07-secrets/postgresql-vso/workload/resources.yaml')
grafana_vds = resource(credentials, 'VaultDynamicSecret', 'grafana-postgresql')
assert grafana_vds.dig('spec', 'allowStaticCreds') == true &&
       grafana_vds.dig('spec', 'path') == 'static-creds/grafana' &&
       grafana_vds.dig('spec', 'destination', 'name') == 'grafana-db-credentials' &&
       grafana_vds.dig('spec', 'rolloutRestartTargets', 0, 'name') == 'kube-prometheus-stack-grafana',
       'VSO must materialize Grafana static credentials and restart Grafana on rotation'
radar_vds = resource(credentials, 'VaultDynamicSecret', 'radar-postgresql')
grafana_auth = resource(credentials, 'VaultAuth', 'openbao-grafana')
radar_auth = resource(credentials, 'VaultAuth', 'openbao-radar')
assert grafana_vds.dig('spec', 'allowStaticCreds') == true &&
       grafana_vds.dig('spec', 'destination', 'name') == 'grafana-db-credentials' &&
       grafana_auth.dig('spec', 'kubernetes', 'role') == grafana_identity['name'],
       'VSO must materialize Grafana credentials using its bootstrapped OpenBao role'
assert radar_vds.dig('spec', 'allowStaticCreds') == true &&
       radar_vds.dig('spec', 'destination', 'name') == 'radar-db-credentials' &&
       radar_auth.dig('spec', 'kubernetes', 'role') == radar_identity['name'],
       'VSO must materialize Radar credentials using its bootstrapped OpenBao role'
monitoring_crds = docs('01-bootstrap/monitoring-crds/app.yml').first
assert monitoring_crds.dig('spec', 'syncPolicy', 'syncOptions').include?('CreateNamespace=true'),
       'The Grafana VSO ServiceAccount namespace must exist before Wave 7'

crossplane_app = docs('03-core/crossplane/app.yml').first
vso_app = docs('04-secrets/vault-secrets-operator/app.yml').first
postgresql_app = docs('06-data/postgresql/app.yml').first
monitoring_app = docs('08-monitoring/kube-prometheus-stack/app.yml').first
assert crossplane_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '3' &&
       vso_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '4' &&
       postgresql_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '6' &&
       monitoring_app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '8',
       'Crossplane, VSO, PostgreSQL and monitoring rollout dependencies changed'

values = YAML.load_file(File.join(ROOT, '03-core/kube-prometheus-stack/values.yml'))
grafana_values = values.fetch('grafana')
assert grafana_values.dig('grafana.ini', 'database', 'type') == 'postgres' &&
       grafana_values.dig('grafana.ini', 'database', 'host') == 'platform-postgres-rw.postgresql.svc:5432' &&
       grafana_values.dig('grafana.ini', 'database', 'ssl_mode') == 'require' &&
       grafana_values.dig('envValueFrom', 'GF_DATABASE_PASSWORD', 'secretKeyRef', 'name') == 'grafana-db-credentials',
       'Grafana must consume the OpenBao-managed PostgreSQL credential over TLS'

puts 'PASS: OpenBao manager, Crossplane static roles, VSO rotation and automated wave contracts'
