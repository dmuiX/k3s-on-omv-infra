#!/usr/bin/env ruby
# Render pinned Helm charts and local templates without Kubernetes access.
# Generated Secrets stay in memory and are never printed.
require 'yaml'
require 'json'
require 'base64'
require 'open3'
require 'tmpdir'

ROOT = File.expand_path('..', __dir__)
HELM = ARGV.fetch(0, 'helm')

def check(condition, message)
  raise message unless condition
end

def yaml_docs(text)
  YAML.load_stream(text).compact
end

def find_resource(resources, kind, name)
  resources.find { |r| r['kind'] == kind && r.dig('metadata', 'name') == name } ||
    raise("Rendered #{kind}/#{name} not found")
end

PI_TOLERATION = {'key' => 'CriticalAddonsOnly', 'operator' => 'Equal',
                 'value' => 'true', 'effect' => 'NoSchedule'}.freeze

def pod_spec(resource)
  case resource['kind']
  when 'Deployment', 'StatefulSet', 'DaemonSet', 'Job'
    resource.dig('spec', 'template', 'spec')
  when 'CronJob'
    resource.dig('spec', 'jobTemplate', 'spec', 'template', 'spec')
  end
end

def permits_pi?(resource)
  spec = pod_spec(resource)
  spec && Array(spec['tolerations']).include?(PI_TOLERATION)
end

def render(name, env, chart_name = nil)
  app = Dir.glob(File.join(ROOT, '[0-9][0-9]-*', '*', 'app.yml'))
           .flat_map { |path| yaml_docs(File.read(path)) }
           .find { |resource| resource.dig('metadata', 'name') == name }
  check(!app.nil?, "Application #{name} not found in a numbered component directory")
  sources = app.fetch('spec').fetch('sources')
  source = sources.find { |candidate| candidate['chart'] && (!chart_name || candidate['chart'] == chart_name) }
  check(source, "Application #{name} has no requested Helm chart source")
  repo = source.fetch('repoURL')
  chart = source.fetch('chart')
  release = source.dig('helm', 'releaseName') || name
  chart_ref = (repo.start_with?('ghcr.io/') || repo.start_with?('registry.developers.crunchydata.com/')) ?
    "oci://#{repo}/#{chart}" : chart
  args = [HELM, 'template', release, chart_ref]
  args.concat(['--repo', repo]) unless chart_ref.start_with?('oci://')
  args.concat(['--version', source.fetch('targetRevision'), '--namespace', app.dig('spec', 'destination', 'namespace'),
               '--kube-version', '1.36.4', '--include-crds'])
  source.fetch('helm', {}).fetch('valueFiles', []).each do |path|
    prefix = %w[$values/ $infra/ $snapshot-values/].find { |candidate| path.start_with?(candidate) }
    check(prefix, 'Helm values must use a reviewed Git values source')
    values_path = if prefix == '$snapshot-values/'
                    File.join(ROOT, 'tests/fixtures/openbao-private-values.yml')
                  else
                    File.join(ROOT, path.delete_prefix(prefix))
                  end
    args.concat(['--values', values_path])
  end
  output, _stderr, status = Open3.capture3(env, *args, chdir: File.dirname(env.fetch('HELM_CACHE_HOME')))
  check(status.success?, "Helm rendering failed for #{name} (exit #{status.exitstatus}); chart diagnostics suppressed")
  yaml_docs(output)
rescue Errno::ENOENT
  abort 'Helm executable not found. Install Helm or pass its path: ruby tests/verify-charts.rb /path/to/helm'
end

Dir.mktmpdir('infra-helm-check-') do |dir|
  env = { 'HELM_CACHE_HOME' => File.join(dir, 'cache'), 'HELM_CONFIG_HOME' => File.join(dir, 'config'),
          'HELM_DATA_HOME' => File.join(dir, 'data'), 'HELM_PLUGINS' => File.join(dir, 'plugins') }
  crds = render('monitoring-crds', env)
  monitoring = render('kube-prometheus-stack', env)
  longhorn = render('longhorn', env)
  # Argo's last source intentionally replaces the chart's ConfigMap with the
  # reviewed encrypted StorageClass template.
  longhorn_override = yaml_docs(File.read(File.join(ROOT, '02-controllers/longhorn', 'storageclass-configmap.yaml'))).first
  longhorn_index = longhorn.index { |resource| resource['kind'] == 'ConfigMap' && resource.dig('metadata', 'name') == 'longhorn-storageclass' }
  check(longhorn_index, 'Pinned Longhorn chart lost its storage-class ConfigMap')
  longhorn[longhorn_index] = longhorn_override
  longhorn.concat(yaml_docs(File.read(File.join(ROOT, '02-controllers/longhorn', 'monitoring-storage.yaml'))))
  openbao = render('openbao', env)
  cert_manager = render('cert-manager', env)
  k8up = render('k8up', env)
  secrets_webhook = render('vault-secrets-webhook', env)
  pgo = render('postgresql', env, 'pgo')
  auxiliary = k8up + secrets_webhook + pgo
  check((crds + monitoring + longhorn + openbao + cert_manager + auxiliary).none? do |resource|
    %w[Application ApplicationSet].include?(resource['kind'])
  end, 'A Helm chart unexpectedly rendered a nested Argo resource')

  {'cert-manager' => cert_manager, 'k8up' => k8up, 'longhorn' => longhorn,
   'openbao' => openbao, 'vault-secrets-webhook' => secrets_webhook,
   'pgo' => pgo}.each do |component, resources|
    workloads = resources.select { |resource| pod_spec(resource) }
    check(!workloads.empty?, "#{component} rendered no workload to validate")
    check(workloads.all? { |resource| permits_pi?(resource) },
          "#{component} rendered a workload without the restricted Pi toleration")
  end
  node_exporter = find_resource(monitoring, 'DaemonSet', 'kube-prometheus-stack-prometheus-node-exporter')
  pi_exclusion = node_exporter.dig('spec', 'template', 'spec', 'affinity', 'nodeAffinity',
                                   'requiredDuringSchedulingIgnoredDuringExecution', 'nodeSelectorTerms')
  check(Array(pi_exclusion).any? do |term|
          Array(term['matchExpressions']).include?(
            {'key' => 'kubernetes.io/hostname', 'operator' => 'NotIn', 'values' => ['raspi4']})
        end, 'Monitoring node exporter does not exclude raspi4')

  # Public cluster resources are complete templates, but their sample defaults
  # must never create live resources unless an explicit component is selected.
  local_chart = File.join(ROOT, 'charts', 'cluster-config')
  %w[none argocd grafana longhorn openbao certificates backups postgresql restore].each do |component|
    args = [HELM, 'template', "check-#{component}", local_chart, '--set', "component=#{component}"]
    if component == 'postgresql'
      args.concat(['--set-json',
                   'clusterNetwork.kubernetesApiServerEndpointCIDRs=["192.0.2.2/32","192.0.2.5/32","192.0.2.7/32"]'])
    end
    output, status = Open3.capture2(env, *args, err: File::NULL, chdir: dir)
    check(status.success?, "Local #{component} template failed; chart diagnostics suppressed")
    resources = yaml_docs(output)
    check(resources.empty? == (component == 'none'), "Unexpected default render for #{component}")
    check(resources.all? { |r| r['kind'] == 'HTTPRoute' }, "#{component} route templates changed") if %w[argocd grafana longhorn openbao].include?(component)
    check(resources.any? { |r| r['kind'] == 'Certificate' } && resources.count { |r| r['kind'] == 'ClusterIssuer' } == 2,
          'Public certificate template incomplete') if component == 'certificates'
    if component == 'backups'
      schedule = find_resource(resources, 'Schedule', 'openbao-k8up-schedule')
      pod_config = find_resource(resources, 'PodConfig', 'openbao-k8up-pod-config')
      check(schedule.dig('spec', 'podConfigRef', 'name') == pod_config.dig('metadata', 'name') &&
            pod_config.dig('spec', 'template', 'spec', 'containers') == [{'name' => 'k8up'}] &&
            Array(pod_config.dig('spec', 'template', 'spec', 'tolerations')).include?(PI_TOLERATION),
            'K8up Schedule jobs cannot use raspi4')
      %w[k8up-repo-password r2-credentials].each do |name|
        secret = find_resource(resources, 'Secret', name)
        check(secret.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '-1',
              "Backup Secret/#{name} must sync before its Schedule")
      end
      repository_secret = find_resource(resources, 'Secret', 'k8up-repo-password')
      r2_secret = find_resource(resources, 'Secret', 'r2-credentials')
      check(Base64.decode64(repository_secret.dig('data', 'password')) ==
              'vault:kv/data/k8up/repository-password#password' &&
            Base64.decode64(r2_secret.dig('data', 'access-key-id')) ==
              'vault:kv/data/k8up/r2-credentials#access-key-id' &&
            Base64.decode64(r2_secret.dig('data', 'secret-access-key')) ==
              'vault:kv/data/k8up/r2-credentials#secret-access-key',
            'K8up Secrets must use the canonical dedicated OpenBao paths')
      check(schedule.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '0',
            'Backup Schedule must sync after its Secrets')
    end
    if component == 'postgresql'
      postgres = find_resource(resources, 'PostgresCluster', 'platform-postgres')
      check(postgres.dig('spec', 'users').map { |user| user['name'] } == %w[grafana radar] &&
            postgres.dig('spec', 'backups', 'pgbackrest', 'repos', 0, 'name') == 'repo1' &&
            find_resource(resources, 'Secret', 'platform-postgres-pgbackrest'),
            'Crunchy PostgreSQL users or pgBackRest configuration is incomplete')
    end
    if component == 'restore'
      restore = resources.find { |resource| resource['kind'] == 'Restore' }
      check(restore, 'Manual restore template missing')
      pod_config = find_resource(resources, 'PodConfig', 'openbao-k8up-restore-pod-config')
      check(restore.dig('spec', 'podConfigRef', 'name') == pod_config.dig('metadata', 'name') &&
            pod_config.dig('spec', 'template', 'spec', 'containers') == [{'name' => 'k8up'}] &&
            Array(pod_config.dig('spec', 'template', 'spec', 'tolerations')).include?(PI_TOLERATION),
            'K8up Restore job cannot use raspi4')
    end
  end

  # Schema validation rejects invalid input; normalization keeps valid numeric
  # values from YAML/JSON out of scientific notation in resource/PVC names.
  ordinal_values = File.join(dir, 'restore-ordinal.yaml')
  File.write(ordinal_values, YAML.dump('backup' => { 'restoreOrdinal' => 2147483647 }))
  restore_cases = [
    [['--set', 'backup.restoreOrdinal=0'], 0],
    [['--set', 'backup.restoreOrdinal=1'], 1],
    [['--set', 'backup.restoreOrdinal=2'], 2],
    [['--set-json', 'backup.restoreOrdinal=1.0'], 1],
    [['--set-json', 'backup.restoreOrdinal=1e3'], 1000],
    [['--set-json', 'backup.restoreOrdinal=1000000'], 1000000],
    [['--set-json', 'backup.restoreOrdinal=2147483647'], 2147483647],
    [['--values', ordinal_values], 2147483647]
  ]
  restore_cases.each do |args, ordinal|
    output, status = Open3.capture2(env, HELM, 'template', 'check-restore', local_chart,
                                  '--set', 'component=restore', *args, err: File::NULL, chdir: dir)
    check(status.success?, 'Valid restore ordinal rejected; chart diagnostics suppressed')
    restore = find_resource(yaml_docs(output), 'Restore', "openbao-#{ordinal}-k8up-restore")
    check(restore.dig('spec', 'restoreMethod', 'folder', 'claimName') == "data-openbao-#{ordinal}" &&
          restore.dig('spec', 'paths') == ["/data/openbao-#{ordinal}"], 'Restore targets the wrong ordinal')
  end
  [['--set-string', 'typo'], ['--set-string', '1'], ['--set', '1.5'],
   ['--set', '-1'], ['--set', 'true'], ['--set', 'null'], ['--set', '2147483648']].each do |flag, value|
    output, status = Open3.capture2(env, HELM, 'template', 'check-restore', local_chart,
                                  '--set', 'component=restore', flag, "backup.restoreOrdinal=#{value}",
                                  err: File::NULL, chdir: dir)
    check(!status.success? && output.empty?, 'Invalid restore ordinal must fail without rendering resources')
  end

  %w[cert-manager cert-manager-webhook cert-manager-cainjector].each do |name|
    deployment = find_resource(cert_manager, 'Deployment', name)
    check(deployment.dig('spec', 'replicas') == 2, "#{name} must have two leader/webhook replicas")
    check(deployment.dig('spec', 'template', 'spec', 'affinity', 'podAntiAffinity'),
          "#{name} replicas must be spread across nodes")
    pdb = find_resource(cert_manager, 'PodDisruptionBudget', name)
    check(pdb.dig('spec', 'minAvailable') == 1, "#{name} must retain one pod during voluntary disruption")
  end
  k8up_deployment = find_resource(k8up, 'Deployment', 'k8up')
  check(k8up_deployment.dig('spec', 'replicas') == 2 &&
        k8up_deployment.dig('spec', 'template', 'spec', 'affinity', 'podAntiAffinity',
                             'requiredDuringSchedulingIgnoredDuringExecution'),
        'K8up must have two leader-elected controller replicas on distinct nodes')
  k8up_pdb = yaml_docs(File.read(File.join(ROOT, '02-controllers/k8up', 'pdb.yaml'))).first
  check(k8up_pdb['kind'] == 'PodDisruptionBudget' && k8up_pdb.dig('spec', 'minAvailable') == 1 &&
        k8up_pdb.dig('spec', 'selector', 'matchLabels').all? do |key, value|
          k8up_deployment.dig('spec', 'selector', 'matchLabels', key) == value
        end, 'K8up PDB must retain one correctly selected controller')
  webhook_deployment = find_resource(secrets_webhook, 'Deployment', 'vault-secrets-webhook')
  check(webhook_deployment.dig('spec', 'replicas') == 3 &&
        webhook_deployment.dig('spec', 'template', 'spec', 'affinity', 'podAntiAffinity',
                               'requiredDuringSchedulingIgnoredDuringExecution'),
        'Vault Secrets Webhook must have three replicas on distinct nodes')
  check(webhook_deployment.dig('spec', 'strategy', 'rollingUpdate') ==
          { 'maxUnavailable' => 1, 'maxSurge' => 0 },
        'Vault Secrets Webhook rolling update must work with required anti-affinity')
  webhook_pdb = find_resource(secrets_webhook, 'PodDisruptionBudget', 'vault-secrets-webhook')
  check(webhook_pdb.dig('spec', 'minAvailable') == 2,
        'Vault Secrets Webhook PDB must retain two admission replicas')

  pgo_operator = find_resource(pgo, 'Deployment', 'pgo')
  check(pgo_operator.dig('spec', 'replicas') == 3 &&
        pgo_operator.dig('spec', 'template', 'spec', 'affinity', 'podAntiAffinity',
                         'requiredDuringSchedulingIgnoredDuringExecution'),
        'PGO must have three distributed leader-elected replicas')
  check(pgo_operator.dig('spec', 'template', 'spec', 'containers', 0, 'image')
          .match?(/@sha256:[0-9a-f]{64}\z/), 'PGO operator image must be digest pinned')
  related_images = pgo_operator.dig('spec', 'template', 'spec', 'containers', 0, 'env')
                               .select { |entry| entry['name'].start_with?('RELATED_IMAGE_') }
  check(related_images.all? { |entry| entry['value'].match?(/@sha256:[0-9a-f]{64}\z/) },
        'PGO related images must be digest pinned')

  retained_crds = cert_manager.select { |r| r['kind'] == 'CustomResourceDefinition' }
  check(!retained_crds.empty? && retained_crds.all? { |r|
          r.dig('metadata', 'annotations', 'helm.sh/resource-policy') == 'keep' },
        'Pinned cert-manager chart must retain its CRDs')
  check(crds.length >= 2 && crds.all? { |r| r['kind'] == 'CustomResourceDefinition' &&
        r.dig('spec', 'group') == 'monitoring.coreos.com' }, 'Wave-1 app must render CRDs only')
  %w[servicemonitors.monitoring.coreos.com podmonitors.monitoring.coreos.com].each do |name|
    find_resource(crds, 'CustomResourceDefinition', name)
  end
  check(monitoring.none? { |r| r['kind'] == 'CustomResourceDefinition' || r['kind'] == 'Certificate' },
        'Full monitoring must not own CRDs or depend on cert-manager')
  grafana_secret = find_resource(monitoring, 'Secret', 'kube-prometheus-stack-grafana')
  check((%w[admin-password admin-user] - grafana_secret.fetch('data').keys).empty?,
        'Chart-generated Grafana administrator Secret contract changed')
  grafana = find_resource(monitoring, 'PersistentVolumeClaim', 'kube-prometheus-stack-grafana')
  check(grafana.dig('spec', 'storageClassName') == 'longhorn' &&
        grafana.dig('spec', 'resources', 'requests', 'storage') == '2Gi' &&
        grafana.dig('spec', 'accessModes') == ['ReadWriteOnce'], 'Grafana PVC is not right-sized on Longhorn')
  grafana_deployment = find_resource(monitoring, 'Deployment', 'kube-prometheus-stack-grafana')
  check(grafana_deployment.dig('spec', 'strategy') == { 'type' => 'Recreate' },
        'Grafana upgrades must not overlap writers or block on RWO cross-node attachment')
  prometheus = find_resource(monitoring, 'Prometheus', 'kube-prometheus-stack-prometheus')
  alertmanager = find_resource(monitoring, 'Alertmanager', 'kube-prometheus-stack-alertmanager')
  { prometheus => ['20Gi', 'longhorn-monitoring'],
    alertmanager => ['1Gi', 'longhorn'] }.each do |r, (size, storage_class)|
    spec = r.dig('spec', 'storage', 'volumeClaimTemplate', 'spec')
    check(spec && spec['storageClassName'] == storage_class && spec.dig('resources', 'requests', 'storage') == size &&
          spec['accessModes'] == ['ReadWriteOnce'], "#{r['kind']} PVC template is not on Longhorn")
  end
  check(prometheus.dig('spec', 'retention') == '15d' && prometheus.dig('spec', 'retentionSize') == '18GB',
        'Prometheus retention must fit inside its 20Gi claim')
  %w[serviceMonitorSelector serviceMonitorNamespaceSelector podMonitorSelector podMonitorNamespaceSelector].each do |key|
    check(prometheus.dig('spec', key) == {}, "Prometheus #{key} would filter out infra monitors")
  end
  # The wave-1 chart renders the CRDs; an offline render cannot prove they
  # become Established before wave-2 charts emit monitoring resources.
  kinds = crds.map { |r| r.dig('spec', 'names', 'kind') }
  (monitoring + longhorn).each do |r|
    next unless r.fetch('apiVersion', '').start_with?('monitoring.coreos.com/')
    check(kinds.include?(r['kind']), 'Rendered monitor has no corresponding bootstrap CRD')
  end

  storage_cm = find_resource(longhorn, 'ConfigMap', 'longhorn-storageclass')
  storage = YAML.safe_load(storage_cm.fetch('data').fetch('storageclass.yaml'))
  check(storage.dig('metadata', 'name') == 'longhorn', 'Wrong StorageClass name')
  check(storage['provisioner'] == 'driver.longhorn.io', 'Wrong storage provisioner')
  check(storage.dig('metadata', 'annotations', 'storageclass.kubernetes.io/is-default-class') == 'false',
        'Chart would add a second default StorageClass')
  check(storage.dig('parameters', 'numberOfReplicas') == '3', 'Rendered PVC replica count is not 3')
  check(storage['reclaimPolicy'] == 'Retain', 'Rendered reclaim policy is not Retain')
  check(storage.dig('parameters', 'encrypted') == 'true', 'Effective longhorn class must encrypt new volumes')
  monitoring_storage = find_resource(longhorn, 'StorageClass', 'longhorn-monitoring')
  check(monitoring_storage.dig('parameters', 'numberOfReplicas') == '2' &&
        monitoring_storage.dig('parameters', 'nodeSelector') == 'monitoring-storage' &&
        monitoring_storage.dig('parameters', 'encrypted') == 'true' &&
        monitoring_storage['reclaimPolicy'] == 'Retain',
        'Prometheus class must use two encrypted replicas on selected nodes')
  monitoring_nodes = longhorn.select { |r| r['kind'] == 'Node' }
  check(monitoring_nodes.map { |r| r.dig('metadata', 'name') }.sort == %w[omv wyse5070] &&
        monitoring_nodes.all? do |r|
          r.dig('spec', 'tags') == ['monitoring-storage'] &&
            r.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '1'
        end, 'Prometheus storage tags must wait for Longhorn to create OMV and Wyse Node CRs')
  settings_cm = find_resource(longhorn, 'ConfigMap', 'longhorn-default-setting')
  settings = YAML.safe_load(settings_cm.fetch('data').fetch('default-setting.yaml'))
  check(JSON.parse(settings.fetch('default-replica-count')) == { 'v1' => '3', 'v2' => '3' },
        'Rendered UI replica defaults do not match three-node configuration')
  ui = find_resource(longhorn, 'Service', 'longhorn-frontend')
  check(ui.dig('spec', 'type') == 'ClusterIP', 'UI unexpectedly exposed directly')
  check(ui.dig('spec', 'ports').any? { |p| p['port'] == 80 }, 'HTTPRoute backend port does not exist')
  monitor = find_resource(longhorn, 'ServiceMonitor', 'longhorn-prometheus-servicemonitor')
  backend = find_resource(longhorn, 'Service', 'longhorn-backend')
  check(monitor.dig('spec', 'selector', 'matchLabels').all? { |k, v| backend.dig('metadata', 'labels', k) == v },
        'Longhorn monitor does not select its metrics Service')
  check(monitor.dig('spec', 'endpoints').all? { |e| backend.dig('spec', 'ports').any? { |p| p['name'] == e['port'] } },
        'Longhorn monitor refers to a missing metrics port')
  openbao_server = find_resource(openbao, 'StatefulSet', 'openbao')
  openbao_container = openbao_server.dig('spec', 'template', 'spec', 'containers').find do |candidate|
    candidate['name'] == 'openbao'
  end
  check(openbao_container && openbao_container['image'] ==
        'quay.io/openbao/openbao:2.7.1@sha256:6d2b93856e3fcf7b18ad855a0b51eaba474dc8b79cf554379ea32034797d2acf',
        'OpenBao must render the reviewed digest-pinned 2.7.1 release')
  check(openbao_server.dig('spec', 'updateStrategy', 'type') == 'OnDelete',
        'OpenBao upgrade must remain explicitly activated one voter at a time')
  check(openbao_server.dig('spec', 'replicas') == 3, 'OpenBao must render three Raft server pods')
  check(openbao_server.dig('spec', 'template', 'spec', 'affinity', 'podAntiAffinity',
                           'requiredDuringSchedulingIgnoredDuringExecution'),
        'OpenBao Raft voters must be placed on separate nodes')
  openbao_pdb = find_resource(openbao, 'PodDisruptionBudget', 'openbao')
  check(openbao_pdb.dig('spec', 'maxUnavailable') == 1, 'OpenBao PDB must protect Raft quorum')
  openbao_claims = openbao_server.dig('spec', 'volumeClaimTemplates') || []
  check(openbao_claims.length == 2 && openbao_claims.all? { |claim|
          claim.dig('spec', 'storageClassName') == 'longhorn' &&
            claim.dig('spec', 'resources', 'requests', 'storage') == '1Gi'
        }, 'OpenBao must render separate right-sized data and audit PVC templates')
  snapshot = find_resource(openbao, 'CronJob', 'openbao-snapshot')
  snapshot_spec = pod_spec(snapshot)
  snapshot_container = snapshot_spec.fetch('containers').find { |candidate| candidate['name'] == 'bao-snapshot' }
  check(snapshot.dig('spec', 'schedule') == '17 2 * * *' &&
        snapshot.dig('spec', 'concurrencyPolicy') == 'Forbid',
        'OpenBao native snapshot must run daily without overlapping jobs')
  check(snapshot_spec['serviceAccountName'] == 'openbao-snapshot' &&
        snapshot_container.fetch('image').include?('@sha256:') &&
        snapshot_container.dig('resources', 'requests', 'memory') == '64Mi' &&
        snapshot_container.dig('resources', 'limits', 'memory') == '256Mi',
        'OpenBao snapshot agent identity, image or resource bounds changed')
  snapshot_config = find_resource(openbao, 'ConfigMap', 'openbao-snapshot').fetch('data')
  check(snapshot_config['BAO_ROLE'] == 'openbao-snapshot' &&
        snapshot_config['BAO_SECRET_PATH'] == 'kv/openbao-snapshots/r2-credentials' &&
        snapshot_config['S3_EXPIRE_DAYS'] == '14',
        'OpenBao snapshot auth, credential path or retention changed')
  snapshot_policy = find_resource(openbao, 'NetworkPolicy', 'openbao-snapshot-egress')
  snapshot_ports = snapshot_policy.dig('spec', 'egress').flat_map { |entry| entry.fetch('ports') }
    .map { |entry| entry['port'] }.sort
  check(snapshot_ports == [53, 53, 443, 8200],
        'OpenBao snapshot egress must remain limited to DNS, OpenBao and HTTPS')
  snapshot_alerts = find_resource(openbao, 'PrometheusRule', 'openbao-snapshot')
    .dig('spec', 'groups').flat_map { |group| group.fetch('rules') }.map { |rule| rule['alert'] }.sort
  check(snapshot_alerts == %w[OpenBaoSnapshotJobFailed OpenBaoSnapshotStale],
        'OpenBao snapshot failure/staleness alerts are incomplete')
  manager = find_resource(longhorn, 'DaemonSet', 'longhorn-manager')
  container = manager.dig('spec', 'template', 'spec', 'containers').find { |c| c['name'] == 'longhorn-manager' }
  check(container.dig('resources', 'requests', 'memory') == '256Mi', 'Manager memory request was ignored')
  check(container.dig('resources', 'limits', 'memory') == '512Mi', 'Manager memory limit was ignored')
  check(longhorn.none? { |r| %w[Gateway Ingress HelmRelease HelmRepository].include?(r['kind']) },
        'Unexpected routing or Flux resource rendered by Longhorn')
  puts 'PASS: pinned Helm renders and private templates, early monitoring CRDs, Longhorn-backed monitoring, PostgreSQL controllers'
end
