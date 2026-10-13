#!/usr/bin/env ruby
# Offline checks only. Never connect to Kubernetes or read/generate key material.
require 'yaml'

ROOT = File.expand_path('..', __dir__)

def document(path)
  YAML.safe_load(File.read(File.join(ROOT, path)))
end

def check(condition, message)
  raise message unless condition
end

app = document('02-controllers/longhorn/app.yml')
sc = document('02-controllers/longhorn/storageclass.yaml')
sources = app.dig('spec', 'sources')

check(app['kind'] == 'Application' && app.dig('metadata', 'name') == 'longhorn',
      'Use the existing Longhorn Application, not a second owner')
check(sources.any? { |source| source['chart'] == 'longhorn' && source['targetRevision'] == '1.13.0' },
      'Longhorn must use the pinned release containing the encrypted-volume cleanup fix')
check(sources.any? { |source| source['ref'] == 'values' }, 'Longhorn Git values source missing')
authored_source = sources.last
check(authored_source['repoURL'] == 'https://github.com/dmuiX/k3s-on-omv-infra.git' &&
      authored_source['targetRevision'] == 'main' &&
      authored_source['path'] == '02-controllers/longhorn' &&
      authored_source.dig('directory', 'include') ==
        '{storageclass.yaml,node-default-tags.yaml,monitoring-storage.yaml,monitoring-network-policy.yaml}',
      'Longhorn node annotations and StorageClasses must be the final branch-tracked Argo source')
check(sc.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '-1',
      'Sync the encrypted class before chart workloads')
check(sc['apiVersion'] == 'storage.k8s.io/v1' && sc['kind'] == 'StorageClass', 'Invalid class template')
check(sc.dig('metadata', 'name') == 'longhorn', 'Encrypt the standard longhorn class, not a second class')
check(sc.dig('metadata', 'annotations', 'storageclass.kubernetes.io/is-default-class') == 'false',
      'Keep local-path as the cluster default')
check(sc['provisioner'] == 'driver.longhorn.io' && sc['allowVolumeExpansion'] == true,
      'Incorrect CSI driver or missing expansion')
check(sc['reclaimPolicy'] == 'Retain' && sc['volumeBindingMode'] == 'Immediate', 'Wrong lifecycle policy')
params = sc.fetch('parameters')
expected_params = {
  'numberOfReplicas' => '3', 'staleReplicaTimeout' => '30', 'fromBackup' => '',
  'fsType' => 'ext4', 'dataLocality' => 'disabled', 'unmapMarkSnapChainRemoved' => 'ignored',
  'disableRevisionCounter' => 'true', 'dataEngine' => 'v1', 'backupTargetName' => 'default',
  'encrypted' => 'true',
  'csi.storage.k8s.io/provisioner-secret-name' => 'longhorn-volume-encryption',
  'csi.storage.k8s.io/provisioner-secret-namespace' => 'longhorn',
  'csi.storage.k8s.io/node-publish-secret-name' => 'longhorn-volume-encryption',
  'csi.storage.k8s.io/node-publish-secret-namespace' => 'longhorn',
  'csi.storage.k8s.io/node-stage-secret-name' => 'longhorn-volume-encryption',
  'csi.storage.k8s.io/node-stage-secret-namespace' => 'longhorn',
  'csi.storage.k8s.io/node-expand-secret-name' => 'longhorn-volume-encryption',
  'csi.storage.k8s.io/node-expand-secret-namespace' => 'longhorn'
}
check(params == expected_params, 'Direct adoption must preserve every StorageClass parameter exactly')
check(params['encrypted'] == 'true' && params['dataEngine'] == 'v1' && params['fsType'] == 'ext4',
      'V1 encrypted filesystem provisioning not explicit')
check(params['numberOfReplicas'] == '3', 'New encrypted volumes must use three Longhorn replicas')
check(params.values.all? { |value| value.is_a?(String) }, 'StorageClass parameters must be strings')
%w[provisioner node-publish node-stage node-expand].each do |operation|
  check(params["csi.storage.k8s.io/#{operation}-secret-name"] == 'longhorn-volume-encryption',
        "Wrong/missing #{operation} Secret name")
  check(params["csi.storage.k8s.io/#{operation}-secret-namespace"] == 'longhorn',
        "Wrong/missing #{operation} Secret namespace")
end
check(params.keys.grep(/secret-(name|namespace)\z/).size == 8, 'Unexpected CSI credential reference set')
check(!params.key?('CRYPTO_KEY_VALUE') && !sc.key?('data') && !sc.key?('stringData'),
      'Key material must not be rendered into the public class')
check(params.values.none? { |value| value.start_with?('vault:') }, 'No OpenBao dependency for volume unlock')
%w[02-controllers/longhorn/config-app.yml 02-controllers/longhorn/storageclass-encrypted.yaml
   02-controllers/longhorn/storageclass-configmap.yaml].each do |path|
  check(!File.exist?(File.join(ROOT, path)), 'Retired additional-class proposal is still selected')
end

# No workload/PVC-name changes or default-class switch are part of this rollout.
values = document('02-controllers/longhorn/values.yml')
check(values.dig('persistence', 'createStorageClass') == false,
      'The chart must not create a duplicate StorageClass template')
check(values.dig('persistence', 'defaultClass') == false, 'Original class default changed')
check(values.dig('persistence', 'defaultClassReplicaCount') == 3, 'New PVC replica default must be three')
check(!values.fetch('defaultSettings').key?('guaranteedInstanceManagerCPU'),
      'Default instance-manager CPU reservations must not be redundantly overridden')
monitoring_policy = document('02-controllers/longhorn/monitoring-network-policy.yaml')
check(monitoring_policy['kind'] == 'NetworkPolicy' &&
      monitoring_policy.dig('spec', 'podSelector', 'matchLabels') == {'app' => 'longhorn-manager'} &&
      monitoring_policy.dig('spec', 'ingress', 0, 'from', 0, 'namespaceSelector', 'matchLabels',
                            'kubernetes.io/metadata.name') == 'kube-prometheus-stack' &&
      monitoring_policy.dig('spec', 'ingress', 0, 'from', 0, 'podSelector', 'matchLabels',
                            'app.kubernetes.io/name') == 'prometheus' &&
      monitoring_policy.dig('spec', 'ingress', 0, 'ports') == [{'protocol' => 'TCP', 'port' => 9500}],
      'Longhorn 1.12 internal policy must retain only the reviewed Prometheus metrics ingress')
openbao = document('03-core/openbao/values.yml')
%w[dataStorage auditStorage].each do |storage|
  check(openbao.dig('server', storage, 'storageClass') == 'longhorn' &&
        openbao.dig('server', storage, 'size') == '1Gi',
        'OpenBao must use right-sized encrypted Longhorn claims')
end
check(openbao.dig('server', 'ha', 'replicas') == 3, 'OpenBao must run one Raft voter per node')
puts 'PASS: one directly owned encrypted Longhorn class, three replicas, complete CSI references'
