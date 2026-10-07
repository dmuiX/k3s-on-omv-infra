#!/usr/bin/env ruby
# Offline Crunchy PostgreSQL desired-state checks. No Kubernetes API is contacted.
require 'yaml'
require 'open3'
require 'tmpdir'

ROOT = File.expand_path('..', __dir__)
PI_TOLERATION = {'key' => 'CriticalAddonsOnly', 'operator' => 'Equal',
                 'value' => 'true', 'effect' => 'NoSchedule'}.freeze

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
  output, _error, status = Open3.capture3(*args, chdir: chdir)
  raise("Command failed: #{args.first}; diagnostics suppressed") unless status.success?
  output
end

kustomized = docs(run('kubectl', 'kustomize', '06-data/postgresql'))
check(kustomized.none? { |item| %w[Cluster DatabaseRole Database ScheduledBackup ObjectStore].include?(item['kind']) },
      'CloudNativePG or Barman resources remain in the Crunchy platform')
check(kustomized.none? { |item| %w[Ingress HTTPRoute Gateway].include?(item['kind']) },
      'PostgreSQL must not have a public route')

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

pdb = resource(kustomized, 'PodDisruptionBudget', 'pgo')
check(pdb.dig('spec', 'minAvailable') == 2 &&
      pdb.dig('spec', 'selector', 'matchLabels') == {
        'postgres-operator.crunchydata.com/control-plane' => 'pgo'
      }, 'PGO controller PDB must retain two exact replicas')

policies = kustomized.select { |item| item['kind'] == 'NetworkPolicy' }
check(policies.all? { |item| item.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '0' },
      'Network isolation must apply before PostgreSQL')
check(policies.any? { |item| item.dig('metadata', 'name') == 'default-deny' &&
      item.dig('spec', 'policyTypes').sort == %w[Egress Ingress] }, 'PostgreSQL default deny is missing')
operator_policy = resource(policies, 'NetworkPolicy', 'allow-pgo-operator')
cluster_policy = resource(policies, 'NetworkPolicy', 'allow-platform-postgres')
check(operator_policy.dig('spec', 'podSelector', 'matchLabels') == {
        'postgres-operator.crunchydata.com/control-plane' => 'pgo'
      }, 'PGO operator policy selector changed')
check(cluster_policy.dig('spec', 'podSelector', 'matchLabels') == {
        'postgres-operator.crunchydata.com/cluster' => 'platform-postgres'
      } && cluster_policy.to_s.include?('radar') && cluster_policy.to_s.include?('grafana') &&
      cluster_policy.to_s.include?('9187') && cluster_policy.to_s.include?('2022'),
      'PostgreSQL cluster, consumer, monitoring or pgBackRest network paths are missing')

monitor = resource(kustomized, 'PodMonitor', 'platform-postgres')
check(monitor.dig('spec', 'selector', 'matchLabels',
                  'postgres-operator.crunchydata.com/cluster') == 'platform-postgres' &&
      monitor.dig('spec', 'podMetricsEndpoints', 0, 'port') == 'exporter',
      'PGO exporter PodMonitor changed')
alerts = resource(kustomized, 'PrometheusRule', 'platform-postgres')
alert_names = alerts.dig('spec', 'groups').flat_map { |group| group['rules'] }.map { |rule| rule['alert'] }
%w[PostgreSQLExporterTargetsMissing PostgreSQLSynchronousStandbyUnavailable PostgreSQLBaseBackupTooOld PostgreSQLWALArchivingStalled
   PostgreSQLPVCUsageWarning PostgreSQLPVCUsageCritical PostgreSQLBackupMetricMissing].each do |name|
  check(alert_names.include?(name), "Missing alert #{name}")
end
check(alerts.to_s.include?('ccp_backrest_') && alerts.to_s.include?('ccp_archive_command_status_') &&
      !alerts.to_s.include?('cnpg_'), 'Monitoring must use pgMonitor/pgBackRest metrics')

rendered = docs(run('helm', 'template', 'postgresql', 'charts/cluster-config', '--namespace', 'postgresql',
                    '--set', 'component=postgresql'))
cluster = resource(rendered, 'PostgresCluster', 'platform-postgres')
check(cluster['apiVersion'] == 'postgres-operator.crunchydata.com/v1' &&
      cluster.dig('spec', 'postgresVersion') == 18 &&
      cluster.dig('spec', 'image').match?(/:ubi9-18\.6-2633@sha256:[0-9a-f]{64}\z/),
      'Crunchy PostgreSQL 18.6 image must be digest pinned')
instance = cluster.dig('spec', 'instances', 0)
check(cluster.dig('spec', 'instances').length == 1 && instance['replicas'] == 3 && instance['minAvailable'] == 2,
      'PostgreSQL must have exactly three HA instances and retain two during disruption')
check(instance.dig('dataVolumeClaimSpec') == {
        'accessModes' => ['ReadWriteOnce'], 'storageClassName' => 'longhorn-postgres',
        'resources' => {'requests' => {'storage' => '5Gi'}}
      }, 'Unexpected PostgreSQL storage contract')
check(instance.dig('metadata', 'annotations', 'k8up.io/backup') == 'false',
      'Generated PGDATA PVCs must be excluded from K8up')
check(instance['resources'] == {
        'requests' => {'cpu' => '250m', 'memory' => '512Mi'},
        'limits' => {'cpu' => '1', 'memory' => '1Gi'}
      }, 'Unexpected PostgreSQL resource budget')
check(instance.fetch('tolerations').include?(PI_TOLERATION), 'PostgreSQL instances cannot use raspi4')
hosts = instance.dig('affinity', 'nodeAffinity', 'requiredDuringSchedulingIgnoredDuringExecution',
                     'nodeSelectorTerms', 0, 'matchExpressions', 0, 'values')
check(hosts.sort == %w[omv raspi4 wyse5070] &&
      instance.dig('affinity', 'podAntiAffinity', 'requiredDuringSchedulingIgnoredDuringExecution', 0,
                   'topologyKey') == 'kubernetes.io/hostname',
      'PostgreSQL instances need reviewed nodes and hard hostname anti-affinity')
check(cluster.dig('spec', 'patroni', 'dynamicConfiguration') == {
        'synchronous_mode' => true, 'synchronous_mode_strict' => false,
        'synchronous_node_count' => 1
      }, 'Patroni must prefer one synchronous standby without sacrificing severe-degradation availability')
check(cluster.dig('spec', 'config', 'parameters', 'max_connections') == '100' &&
      cluster.dig('spec', 'config', 'parameters', 'shared_buffers') == '128MB' &&
      cluster.dig('spec', 'config', 'parameters', 'archive_timeout') == '5min',
      'PostgreSQL connection/memory/WAL defaults changed')
rules = cluster.dig('spec', 'authentication', 'rules')
check(rules.first == {'connection' => 'hostnossl', 'method' => 'reject'} &&
      rules.any? { |rule| rule['users'] == ['grafana'] && rule['databases'] == ['grafana'] } &&
      rules.any? { |rule| rule['users'] == ['radar'] && rule['databases'] == ['radar'] },
      'TLS-only and cross-database role authentication must fail closed')
users = cluster.dig('spec', 'users')
check(users == [
        {'name' => 'grafana', 'databases' => ['grafana'], 'options' => 'CONNECTION LIMIT 25',
         'password' => {'type' => 'AlphaNumeric'}},
        {'name' => 'radar', 'databases' => ['radar'], 'options' => 'CONNECTION LIMIT 12',
         'password' => {'type' => 'AlphaNumeric'}}
      ], 'PGO must generate the two bounded application users, databases and passwords')
check(rendered.none? { |item| item['kind'] == 'Secret' && item.dig('metadata', 'name').match?(/pguser/) },
      'Git must not pre-create PGO generated user credential Secrets')
backup = cluster.dig('spec', 'backups', 'pgbackrest')
check(backup['image'].match?(/@sha256:[0-9a-f]{64}\z/) &&
      backup['manual'] == {'repoName' => 'repo1', 'options' => ['--type=full']} &&
      backup.dig('repos', 0, 'name') == 'repo1' &&
      backup.dig('repos', 0, 'schedules', 'full') == '30 3 * * *' &&
      backup.dig('global', 'repo1-retention-full') == '30' &&
      backup.dig('global', 'archive-async') == 'y',
      'pgBackRest R2 schedule, retention, WAL archive or image pin changed')
check(backup.dig('sidecars', 'pgbackrest', 'resources', 'limits') ==
        {'cpu' => '500m', 'memory' => '256Mi'} &&
      backup.dig('sidecars', 'pgbackrestConfig', 'resources', 'limits') ==
        {'cpu' => '200m', 'memory' => '128Mi'} && backup['repoHost'].nil?,
      'S3-only pgBackRest sidecars must be bounded without a nonexistent repository host')
check(cluster.dig('spec', 'customTLSSecret').nil? && cluster.dig('spec', 'customReplicationTLSSecret').nil?,
      'PGO must own and rotate its internal TLS credentials')
secret = resource(rendered, 'Secret', 'platform-postgres-pgbackrest')
s3_config = secret.dig('stringData', 's3.conf')
check(s3_config.include?('${vault:kv/data/postgresql/r2-credentials#access-key-id}') &&
      s3_config.include?('${vault:kv/data/postgresql/r2-credentials#secret-access-key}') &&
      !s3_config.match?(/replace-me-postgresql/),
      'pgBackRest Secret must contain only inline OpenBao references')

app = docs(File.read(File.join(ROOT, '06-data/postgresql/app.yml'))).first
chart = app.dig('spec', 'sources').find { |source| source['chart'] == 'pgo' }
check(chart && chart['repoURL'] == 'registry.developers.crunchydata.com/crunchydata' &&
      chart['targetRevision'] == '6.0.3', 'PostgreSQL Application must pin PGO 6.0.3')
infra_sources = app.dig('spec', 'sources').select do |source|
  source['repoURL'] == 'https://github.com/dmuiX/k3s-on-omv-infra.git'
end
infra_revisions = infra_sources.map { |source| source['targetRevision'] }.uniq
check(infra_revisions.length == 1 && infra_revisions.first.match?(/\A[0-9a-f]{40}\z/),
      'PostgreSQL local sources must share one immutable full Infra revision')
pinned_revision = infra_revisions.first
%w[06-data/postgresql/values-pgo.yml charts/cluster-config/templates/postgresql.yaml].each do |path|
  _output, _error, status = Open3.capture3('git', 'cat-file', '-e', "#{pinned_revision}:#{path}", chdir: ROOT)
  check(status.success?, "Pinned Infra revision does not contain #{path}")
end
check(app.dig('spec', 'sources').none? { |source| %w[cloudnative-pg plugin-barman-cloud].include?(source['chart']) },
      'PostgreSQL Application still contains a CloudNativePG/Barman chart')
check(app.dig('spec', 'syncPolicy', 'automated', 'prune') == false,
      'The migration release must preserve legacy CNPG/Barman resources until live acceptance')

Dir.mktmpdir('pgo-render-') do |dir|
  env = {'HELM_CACHE_HOME' => File.join(dir, 'cache'), 'HELM_CONFIG_HOME' => File.join(dir, 'config'),
         'HELM_DATA_HOME' => File.join(dir, 'data'), 'HELM_PLUGINS' => File.join(dir, 'plugins')}
  operator = docs(run(env, 'helm', 'template', 'pgo',
                      'oci://registry.developers.crunchydata.com/crunchydata/pgo',
                      '--version', '6.0.3', '--namespace', 'postgresql', '--include-crds',
                      '--values', '06-data/postgresql/values-pgo.yml', chdir: ROOT))
  deployment = resource(operator, 'Deployment', 'pgo')
  check(deployment.dig('spec', 'replicas') == 3 &&
        deployment.dig('spec', 'template', 'spec', 'affinity', 'podAntiAffinity',
                       'requiredDuringSchedulingIgnoredDuringExecution') &&
        deployment.dig('spec', 'template', 'spec', 'tolerations').include?(PI_TOLERATION),
        'PGO controllers need three distributed Pi-capable replicas')
  check(deployment.dig('spec', 'template', 'spec', 'containers', 0, 'image').match?(/@sha256:[0-9a-f]{64}\z/),
        'PGO controller image is not digest pinned')
  env_images = deployment.dig('spec', 'template', 'spec', 'containers', 0, 'env')
                         .select { |entry| entry['name'].start_with?('RELATED_IMAGE_') }
                         .map { |entry| entry['value'] }
  check(!env_images.empty? && env_images.all? { |image| image.match?(/@sha256:[0-9a-f]{64}\z/) },
        'Every PGO related runtime image must be digest pinned')
  crd = resource(operator, 'CustomResourceDefinition', 'postgresclusters.postgres-operator.crunchydata.com')
  check(crd.dig('spec', 'versions').any? { |version| version['name'] == 'v1' && version['served'] },
        'Pinned PGO chart does not provide the v1 PostgresCluster API')
end

puts 'PASS: Crunchy PGO HA, generated users, Longhorn, pgBackRest R2, policies and monitoring render'
