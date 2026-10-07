#!/usr/bin/env ruby
# Render pinned Helm charts and local templates without Kubernetes access.
# Generated Secrets stay in memory and are never printed.
require 'yaml'
require 'json'
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
  chart_ref = repo.start_with?('ghcr.io/') ? "oci://#{repo}/#{chart}" : chart
  args = [HELM, 'template', release, chart_ref]
  args.concat(['--repo', repo]) unless chart_ref.start_with?('oci://')
  args.concat(['--version', source.fetch('targetRevision'), '--namespace', app.dig('spec', 'destination', 'namespace'),
               '--kube-version', '1.36.4', '--include-crds'])
  source.fetch('helm', {}).fetch('valueFiles', []).each do |path|
    prefix = %w[$values/ $infra/].find { |candidate| path.start_with?(candidate) }
    check(prefix, 'Helm values must use a reviewed Git values source')
    args.concat(['--values', File.join(ROOT, path.delete_prefix(prefix))])
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
  openbao = render('openbao', env)
  cert_manager = render('cert-manager', env)
  k8up = render('k8up', env)
  secrets_webhook = render('vault-secrets-webhook', env)
  cnpg = render('postgresql', env, 'cloudnative-pg')
  barman = render('postgresql', env, 'plugin-barman-cloud')
  auxiliary = k8up + secrets_webhook + cnpg + barman
  check((crds + monitoring + longhorn + openbao + cert_manager + auxiliary).none? do |resource|
    %w[Application ApplicationSet].include?(resource['kind'])
  end, 'A Helm chart unexpectedly rendered a nested Argo resource')

  # Public cluster resources are complete templates, but their sample defaults
  # must never create live resources unless an explicit component is selected.
  local_chart = File.join(ROOT, 'charts', 'cluster-config')
  %w[none argocd grafana longhorn openbao certificates backups postgresql restore].each do |component|
    output, status = Open3.capture2(env, HELM, 'template', "check-#{component}", local_chart,
                                     '--set', "component=#{component}", err: File::NULL, chdir: dir)
    check(status.success?, "Local #{component} template failed; chart diagnostics suppressed")
    resources = yaml_docs(output)
    check(resources.empty? == (component == 'none'), "Unexpected default render for #{component}")
    check(resources.all? { |r| r['kind'] == 'HTTPRoute' }, "#{component} route templates changed") if %w[argocd grafana longhorn openbao].include?(component)
    check(resources.any? { |r| r['kind'] == 'Certificate' } && resources.count { |r| r['kind'] == 'ClusterIssuer' } == 2,
          'Public certificate template incomplete') if component == 'certificates'
    if component == 'backups'
      schedule = find_resource(resources, 'Schedule', 'openbao-k8up-schedule')
      %w[k8up-repo-password r2-credentials].each do |name|
        secret = find_resource(resources, 'Secret', name)
        check(secret.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '-1',
              "Backup Secret/#{name} must sync before its Schedule")
      end
      check(schedule.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '0',
            'Backup Schedule must sync after its Secrets')
    end
    if component == 'postgresql'
      object_store = find_resource(resources, 'ObjectStore', 'platform-postgres-backups')
      check(object_store.dig('spec', 'retentionPolicy') == '30d' &&
            find_resource(resources, 'Secret', 'postgresql-r2-credentials'),
            'PostgreSQL Barman configuration is incomplete')
    end
    check(resources.one? { |r| r['kind'] == 'Restore' }, 'Manual restore template missing') if component == 'restore'
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

  cnpg_operator = find_resource(cnpg, 'Deployment', 'cloudnative-pg')
  barman_operator = find_resource(barman, 'Deployment', 'plugin-barman-cloud')
  [cnpg_operator, barman_operator].each do |deployment|
    check(deployment.dig('spec', 'replicas') == 2 &&
          deployment.dig('spec', 'template', 'spec', 'affinity', 'podAntiAffinity',
                         'requiredDuringSchedulingIgnoredDuringExecution'),
          "#{deployment.dig('metadata', 'name')} must have two distributed replicas")
    check(deployment.dig('spec', 'template', 'spec', 'containers', 0, 'image').match?(/@sha256:[0-9a-f]{64}\z/),
          "#{deployment.dig('metadata', 'name')} image must be digest pinned")
  end
  check(find_resource(barman, 'ConfigMap', 'plugin-barman-cloud-config').dig('data', 'SIDECAR_IMAGE')
          .match?(/@sha256:[0-9a-f]{64}\z/), 'Barman sidecar image must be digest pinned')

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
        grafana.dig('spec', 'resources', 'requests', 'storage') == '10Gi' &&
        grafana.dig('spec', 'accessModes') == ['ReadWriteOnce'], 'Grafana PVC is not on Longhorn')
  grafana_deployment = find_resource(monitoring, 'Deployment', 'kube-prometheus-stack-grafana')
  check(grafana_deployment.dig('spec', 'strategy') == { 'type' => 'Recreate' },
        'Grafana upgrades must not overlap writers or block on RWO cross-node attachment')
  prometheus = find_resource(monitoring, 'Prometheus', 'kube-prometheus-stack-prometheus')
  alertmanager = find_resource(monitoring, 'Alertmanager', 'kube-prometheus-stack-alertmanager')
  { prometheus => '20Gi', alertmanager => '5Gi' }.each do |r, size|
    spec = r.dig('spec', 'storage', 'volumeClaimTemplate', 'spec')
    check(spec && spec['storageClassName'] == 'longhorn' && spec.dig('resources', 'requests', 'storage') == size &&
          spec['accessModes'] == ['ReadWriteOnce'], "#{r['kind']} PVC template is not on Longhorn")
  end
  check(prometheus.dig('spec', 'retentionSize') == '18GB', 'Prometheus needs a bounded TSDB size')
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
  check(longhorn.none? { |r| r['kind'] == 'StorageClass' }, 'Longhorn must own its class through the ConfigMap, not a competing raw manifest')
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
  check(openbao_server.dig('spec', 'replicas') == 3, 'OpenBao must render three Raft server pods')
  check(openbao_server.dig('spec', 'template', 'spec', 'affinity', 'podAntiAffinity',
                           'requiredDuringSchedulingIgnoredDuringExecution'),
        'OpenBao Raft voters must be placed on separate nodes')
  openbao_pdb = find_resource(openbao, 'PodDisruptionBudget', 'openbao')
  check(openbao_pdb.dig('spec', 'maxUnavailable') == 1, 'OpenBao PDB must protect Raft quorum')
  check((openbao_server.dig('spec', 'volumeClaimTemplates') || []).length == 2,
        'OpenBao must render separate data and audit PVC templates')
  manager = find_resource(longhorn, 'DaemonSet', 'longhorn-manager')
  container = manager.dig('spec', 'template', 'spec', 'containers').find { |c| c['name'] == 'longhorn-manager' }
  check(container.dig('resources', 'requests', 'memory') == '256Mi', 'Manager memory request was ignored')
  check(container.dig('resources', 'limits', 'memory') == '512Mi', 'Manager memory limit was ignored')
  check(longhorn.none? { |r| %w[Gateway Ingress HelmRelease HelmRepository].include?(r['kind']) },
        'Unexpected routing or Flux resource rendered by Longhorn')
  puts 'PASS: pinned Helm renders and private templates, early monitoring CRDs, Longhorn-backed monitoring, PostgreSQL controllers'
end
