#!/usr/bin/env ruby
# Offline PostgreSQL desired-state checks. No Kubernetes API is contacted.
require 'yaml'
require 'json'
require 'open3'
require 'tmpdir'

ROOT = File.expand_path('..', __dir__)

def check(condition, message)
  raise message unless condition
end

def docs(text)
  YAML.load_stream(text).compact
end

def resource(items, kind, name)
  items.find { |item| item['kind'] == kind && item.dig('metadata', 'name') == name } ||
    raise("Missing #{kind}/#{name}")
end

def run(*args, chdir: ROOT)
  output, error, status = Open3.capture3(*args, chdir: chdir)
  raise("Command failed: #{args.first}; diagnostics suppressed") unless status.success?
  output
end

application = docs(File.read(File.join(ROOT, '06-data/cloudnative-pg/app.yml'))).first
check(application.dig('spec', 'syncPolicy', 'automated', 'prune') == false,
      'PostgreSQL pruning must remain disabled until live acceptance and cleanup')
kustomized = docs(run('kubectl', 'kustomize', '06-data/cloudnative-pg'))
controller_namespace = docs(File.read(File.join(ROOT, '06-data/cloudnative-pg/controllers/namespace.yaml')))
controller_policies = docs(File.read(File.join(ROOT, '06-data/cloudnative-pg/controllers/networkpolicy.yaml')))
controller_pdbs = docs(File.read(File.join(ROOT, '06-data/cloudnative-pg/controllers/pdbs.yaml')))
check(resource(controller_namespace, 'Namespace', 'cloudnative-pg').dig('metadata', 'labels',
      'pod-security.kubernetes.io/enforce') == 'restricted',
      'CloudNativePG controller namespace must enforce restricted Pod Security')
check(controller_pdbs.all? { |item| item.dig('metadata', 'namespace') == 'cloudnative-pg' },
      'CloudNativePG controller PDBs must stay with their workloads')
cluster = resource(kustomized, 'Cluster', 'k3s-postgres')
check(cluster.dig('spec', 'instances') == 3, 'PostgreSQL must have exactly three instances')
check(cluster.dig('spec', 'imageName').match?(/:18\.6-system-bookworm@sha256:[0-9a-f]{64}\z/),
      'PostgreSQL 18.6 system image must be digest pinned')
check(cluster.dig('spec', 'storage') == {
        'storageClass' => 'longhorn-postgres', 'size' => '5Gi', 'resizeInUseVolumes' => true
      }, 'Unexpected PostgreSQL storage contract')
check(cluster.dig('spec', 'inheritedMetadata', 'annotations', 'k8up.io/backup') == 'false',
      'Generated PGDATA PVCs must be excluded from K8up')
check(cluster.dig('spec', 'resources') == {
        'requests' => {'cpu' => '250m', 'memory' => '512Mi'},
        'limits' => {'cpu' => '1', 'memory' => '1Gi'}
      }, 'Unexpected PostgreSQL resource budget')
check(cluster.dig('spec', 'affinity', 'podAntiAffinityType') == 'required' &&
      cluster.dig('spec', 'affinity', 'topologyKey') == 'kubernetes.io/hostname',
      'PostgreSQL instances need hard hostname anti-affinity')
hosts = cluster.dig('spec', 'affinity', 'nodeAffinity', 'requiredDuringSchedulingIgnoredDuringExecution',
                    'nodeSelectorTerms', 0, 'matchExpressions', 0, 'values')
check(hosts.sort == %w[omv raspi4 wyse5070], 'PostgreSQL is not restricted to the reviewed three nodes')
check(cluster.dig('spec', 'postgresql', 'synchronous') == {
        'method' => 'any', 'number' => 1, 'dataDurability' => 'preferred',
        'maxStandbyNamesFromCluster' => 2, 'failoverQuorum' => true
      }, 'Synchronous replication must be ANY 1 with preferred durability and failover quorum')
check(cluster.dig('spec', 'postgresql', 'parameters', 'max_connections') == '100' &&
      cluster.dig('spec', 'postgresql', 'parameters', 'shared_buffers') == '128MB' &&
      cluster.dig('spec', 'postgresql', 'parameters', 'archive_timeout') == '5min',
      'PostgreSQL connection/memory/WAL defaults changed')
check(cluster.dig('spec', 'certificates', 'serverTLSSecret') == 'k3s-postgres-server-tls' &&
      cluster.dig('spec', 'plugins', 0) == {
        'name' => 'barman-cloud.cloudnative-pg.io', 'enabled' => true,
        'isWALArchiver' => true,
        'parameters' => {'barmanObjectName' => 'k3s-postgres-backups'}
      }, 'TLS or Barman plugin wiring is incomplete')
certificate = resource(kustomized, 'Certificate', 'k3s-postgres-server')
check(certificate.dig('spec', 'privateKey') == {
        'algorithm' => 'RSA', 'size' => 2048, 'rotationPolicy' => 'Always'
      }, 'PostgreSQL TLS key must match the RSA-only OpenBao service signing role')
check(cluster.dig('spec', 'postgresql', 'pg_hba').first == 'hostnossl all all all reject' &&
      cluster.dig('spec', 'postgresql', 'pg_hba').include?('hostssl postgres openbao_manager all scram-sha-256') &&
      cluster.dig('spec', 'postgresql', 'pg_hba').last(2) == [
        'hostssl all grafana all reject', 'hostssl all radar all reject'
      ], 'Non-TLS and cross-database role connections must fail closed')

storage = resource(kustomized, 'StorageClass', 'longhorn-postgres')
check(storage['provisioner'] == 'driver.longhorn.io' && storage['reclaimPolicy'] == 'Retain' &&
      storage['allowVolumeExpansion'] == true && storage['volumeBindingMode'] == 'WaitForFirstConsumer',
      'PostgreSQL StorageClass binding/retention/expansion changed')
check(storage.dig('parameters', 'numberOfReplicas') == '1' &&
      storage.dig('parameters', 'dataLocality') == 'strict-local' &&
      storage.dig('parameters', 'encrypted') == 'true',
      'PostgreSQL StorageClass must be encrypted, strict-local and single-replica')
%w[provisioner node-publish node-stage node-expand].each do |operation|
  check(storage.dig('parameters', "csi.storage.k8s.io/#{operation}-secret-name") == 'longhorn-volume-encryption',
        "Missing Longhorn encryption reference for #{operation}")
end

{'grafana' => 25, 'radar' => 12}.each do |name, connection_limit|
  database = resource(kustomized, 'Database', name)
  static_role = resource(kustomized, 'SecretBackendStaticRole', name)
  rotation = static_role.dig('spec', 'forProvider', 'rotationStatements', 0)
  check(kustomized.none? { |item| item['kind'] == 'DatabaseRole' && item.dig('metadata', 'name') == name },
        "CNPG must not retain ownership of the OpenBao-managed #{name} login")
  check(database.dig('spec', 'owner') == name && database.dig('spec', 'databaseReclaimPolicy') == 'retain' &&
        database.dig('spec', 'connectionLimit') == connection_limit &&
        database.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '4',
        "Unsafe or inconsistent #{name} database")
  check(static_role.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '3' &&
        static_role.dig('spec', 'forProvider', 'username') == name &&
        rotation.include?("CREATE ROLE #{name}") && rotation.include?("ALTER ROLE #{name}") &&
        rotation.include?("CONNECTION LIMIT #{connection_limit}") &&
        rotation.include?('NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'),
        "OpenBao must create and constrain the #{name} login before database ownership")
end
check(kustomized.none? { |item| item['kind'] == 'Secret' },
      'PostgreSQL desired state must consume VSO-owned Secrets, not author credential placeholders')
manager = resource(kustomized, 'DatabaseRole', 'openbao-manager')
check(manager.dig('spec', 'passwordSecret', 'name') == 'openbao-manager-credentials' &&
      manager.dig('spec', 'createrole') == true && manager.dig('spec', 'superuser') == false,
      'OpenBao manager must use its VSO credential with only role-management privilege')

backup = resource(kustomized, 'ScheduledBackup', 'k3s-postgres-daily')
check(backup.dig('spec', 'schedule') == '0 30 3 * * *' && backup.dig('spec', 'immediate') == true &&
      backup.dig('spec', 'method') == 'plugin' && backup.dig('spec', 'target') == 'prefer-standby',
      'Daily immediate plugin backup contract changed')
check(kustomized.none? { |item| %w[Ingress HTTPRoute Gateway].include?(item['kind']) },
      'PostgreSQL must not have a public route')
policies = kustomized.select { |item| item['kind'] == 'NetworkPolicy' }
all_policies = policies + controller_policies
check(all_policies.all? { |item| item.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '0' },
      'Network isolation must apply before the first external backup gate')
check(all_policies.one? { |item| item.dig('metadata', 'name') == 'default-deny' &&
      item.dig('spec', 'policyTypes').sort == %w[Egress Ingress] },
      'The unified CloudNativePG namespace needs exactly one default deny policy')
check(all_policies.one? { |item| item.dig('metadata', 'name') == 'allow-dns' },
      'The unified CloudNativePG namespace needs exactly one DNS policy')
check(policies.any? { |item| item.to_s.include?('kube-prometheus-stack') } &&
      policies.any? { |item| item.to_s.include?('radar') && item.to_s.include?('grafana') },
      'Monitoring or registered-consumer network paths are missing')
operator_policy = resource(controller_policies, 'NetworkPolicy', 'allow-cloudnative-pg-operator')
check(operator_policy.dig('metadata', 'namespace') == 'cloudnative-pg' &&
      operator_policy.dig('spec', 'ingress').none? do |rule|
        rule.fetch('ports', []).any? { |port| port['port'] == 9443 }
      end, 'Public manifests must isolate the CloudNativePG webhook in its controller namespace')
postgres_policy = resource(policies, 'NetworkPolicy', 'allow-postgres')
radar_postgres_policy = resource(policies, 'NetworkPolicy', 'allow-radar-postgres')
check(radar_postgres_policy.dig('spec', 'ingress', 0, 'from', 0,
                                'namespaceSelector', 'matchLabels', 'kubernetes.io/metadata.name') == 'radar' &&
      radar_postgres_policy.dig('spec', 'ingress', 0, 'ports', 0, 'port') == 5432,
      'Radar must have a dedicated namespace-scoped PostgreSQL ingress grant')
instance_rule = postgres_policy.dig('spec', 'ingress').find do |rule|
  rule.fetch('from', []).any? { |source| source.dig('podSelector', 'matchLabels', 'cnpg.io/cluster') == 'k3s-postgres' }
end
check(instance_rule && [5432, 8000].all? { |port| instance_rule.fetch('ports').any? { |item| item['port'] == port } },
      'PostgreSQL instances must retain database and manager connectivity to their peers')
operator_source = postgres_policy.dig('spec', 'ingress').flat_map { |rule| rule.fetch('from', []) }.find do |source|
  source.dig('podSelector', 'matchLabels', 'app.kubernetes.io/name') == 'cloudnative-pg'
end
check(operator_source && !operator_source.key?('namespaceSelector'),
      'PostgreSQL policy must select the namespace-local CNPG controller')
radar_source = postgres_policy.dig('spec', 'ingress').flat_map { |rule| rule.fetch('from', []) }.find do |source|
  source.dig('namespaceSelector', 'matchLabels', 'kubernetes.io/metadata.name') == 'radar'
end
check(radar_source && !radar_source.key?('podSelector'),
      'PostgreSQL must accept Radar only from its dedicated namespace')
openbao_source = postgres_policy.dig('spec', 'ingress').flat_map { |rule| rule.fetch('from', []) }.find do |source|
  source.dig('namespaceSelector', 'matchLabels', 'kubernetes.io/metadata.name') == 'openbao'
end
check(openbao_source &&
      openbao_source.dig('podSelector', 'matchLabels', 'app.kubernetes.io/name') == 'openbao' &&
      openbao_source.dig('podSelector', 'matchLabels', 'component') == 'server',
      'PostgreSQL must accept database-engine connections from OpenBao servers')

alerts = resource(kustomized, 'PrometheusRule', 'k3s-postgres')
alert_names = alerts.dig('spec', 'groups').flat_map { |group| group['rules'] }.map { |rule| rule['alert'] }
%w[PostgreSQLSynchronousStandbyUnavailable PostgreSQLBaseBackupTooOld PostgreSQLWALArchivingStalled
   PostgreSQLPVCUsageWarning PostgreSQLPVCUsageCritical PostgreSQLBackupMetricMissing].each do |name|
  check(alert_names.include?(name), "Missing alert #{name}")
end

Dir.mktmpdir('postgresql-render-') do |dir|
  env = {'HELM_CACHE_HOME' => File.join(dir, 'cache'), 'HELM_CONFIG_HOME' => File.join(dir, 'config'),
         'HELM_DATA_HOME' => File.join(dir, 'data'), 'HELM_PLUGINS' => File.join(dir, 'plugins')}
  operator = docs(run(env, 'helm', 'template', 'cloudnative-pg', 'cloudnative-pg', '--repo',
                      'https://cloudnative-pg.github.io/charts', '--version', '0.29.1', '--namespace', 'cloudnative-pg',
                      '--include-crds', '--values', '06-data/cloudnative-pg/values-cloudnativepg.yml', chdir: ROOT))
  plugin = docs(run(env, 'helm', 'template', 'plugin-barman-cloud', 'plugin-barman-cloud', '--repo',
                    'https://cloudnative-pg.github.io/charts', '--version', '0.8.1', '--namespace', 'cloudnative-pg',
                    '--include-crds', '--values', '06-data/cloudnative-pg/values-barman.yml', chdir: ROOT))
  operator_deploy = resource(operator, 'Deployment', 'cloudnative-pg')
  plugin_deploy = resource(plugin, 'Deployment', 'plugin-barman-cloud')
  operator_config = resource(operator, 'ConfigMap', 'cnpg-controller-manager-config')
  check(operator_config.dig('data', 'WATCH_NAMESPACE') == 'cloudnative-pg',
        'CloudNativePG operator must watch only its own namespace')
  [operator_deploy, plugin_deploy].each do |deployment|
    check(deployment.dig('metadata', 'namespace') == 'cloudnative-pg',
          "#{deployment.dig('metadata', 'name')} must run in the controller namespace")
  end
  [operator_deploy, plugin_deploy].each do |deployment|
    check(deployment.dig('spec', 'replicas') == 2, "#{deployment.dig('metadata', 'name')} must have two replicas")
    check(deployment.dig('spec', 'template', 'spec', 'affinity', 'podAntiAffinity',
                         'requiredDuringSchedulingIgnoredDuringExecution'),
          "#{deployment.dig('metadata', 'name')} needs hard anti-affinity")
    image = deployment.dig('spec', 'template', 'spec', 'containers', 0, 'image')
    check(image.match?(/@sha256:[0-9a-f]{64}\z/), "#{deployment.dig('metadata', 'name')} image is not digest pinned")
  end
  sidecar = resource(plugin, 'ConfigMap', 'plugin-barman-cloud-config').dig('data', 'SIDECAR_IMAGE')
  check(sidecar.match?(/@sha256:[0-9a-f]{64}\z/), 'Barman sidecar image is not digest pinned')
  crd_kinds = (operator + plugin).select { |item| item['kind'] == 'CustomResourceDefinition' }
                                  .map { |item| item.dig('spec', 'names', 'kind') }
  %w[Cluster DatabaseRole Database ScheduledBackup ObjectStore].each do |kind|
    check(crd_kinds.include?(kind), "Pinned charts do not provide #{kind}")
  end
  dashboard = resource(operator, 'ConfigMap', 'cnpg-grafana-dashboard')
  dashboard_json = JSON.parse(dashboard.fetch('data').values.first)
  check(dashboard_json.fetch('panels').length > 10, 'Pinned CloudNativePG dashboard is incomplete')
end

private_template = docs(run('helm', 'template', 'postgresql', 'charts/cluster-config', '--namespace', 'cloudnative-pg',
                            '--set', 'component=postgresql'))
object_store = resource(private_template, 'ObjectStore', 'k3s-postgres-backups')
check(object_store.dig('spec', 'retentionPolicy') == '30d' &&
      object_store.dig('spec', 'configuration', 'destinationPath') == 's3://replace-me-postgresql/' &&
      object_store.dig('spec', 'configuration', 'wal', 'maxParallel') == 2,
      'Barman ObjectStore retention/path/WAL configuration changed')
check(private_template.none? { |item| item['kind'] == 'Secret' },
      'PostgreSQL chart must consume the VSO-owned R2 Secret, not author it')
check(object_store.dig('spec', 'configuration', 's3Credentials') == {
        'accessKeyId' => {'name' => 'postgresql-r2-credentials', 'key' => 'ACCESS_KEY_ID'},
        'secretAccessKey' => {'name' => 'postgresql-r2-credentials', 'key' => 'ACCESS_SECRET_KEY'},
        'region' => {'name' => 'postgresql-r2-credentials', 'key' => 'AWS_REGION'}
      }, 'Barman must use the fixed VSO-owned R2 Secret contract')
webhook_policy = resource(private_template, 'NetworkPolicy', 'allow-k3s-api-to-cloudnative-pg-webhook')
sources = webhook_policy.dig('spec', 'ingress', 0, 'from').map { |source| source.dig('ipBlock', 'cidr') }
check(webhook_policy.dig('metadata', 'namespace') == 'cloudnative-pg' &&
      sources == ['192.0.2.1/32', '198.51.100.1/32'] &&
      webhook_policy.dig('spec', 'ingress', 0, 'ports', 0, 'port') == 9443,
      'Webhook policy must use only explicitly configured API sources in the controller namespace')
api_egress_policies = private_template.select do |item|
  item['kind'] == 'NetworkPolicy' && item.dig('metadata', 'name') == 'allow-k3s-api-egress'
end
check(api_egress_policies.map { |item| item.dig('metadata', 'namespace') } == ['cloudnative-pg'],
      'The unified CloudNativePG namespace needs one exact K3s API egress policy')
api_egress_policies.each do |policy|
  destinations = policy.dig('spec', 'egress', 0, 'to').map { |target| target.dig('ipBlock', 'cidr') }
  check(policy.dig('spec', 'podSelector') == {} &&
        destinations == ['192.0.2.1/32'] &&
        policy.dig('spec', 'egress', 0, 'ports') == [{'protocol' => 'TCP', 'port' => 6443}],
        "#{policy.dig('metadata', 'namespace')} must reach only reviewed K3s API endpoints")
end

puts 'PASS: PostgreSQL HA, storage, TLS, roles, backups, policies, monitoring and pinned chart renders'
