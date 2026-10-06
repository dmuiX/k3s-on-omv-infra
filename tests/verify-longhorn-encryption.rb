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

app = document('02-longhorn/app.yml')
cm = document('02-longhorn/storageclass-configmap.yaml')
sc = YAML.safe_load(cm.fetch('data').fetch('storageclass.yaml'))
sources = app.dig('spec', 'sources')

check(app['kind'] == 'Application' && app.dig('metadata', 'name') == 'longhorn',
      'Use the existing Longhorn Application, not a second owner')
check(sources.any? { |source| source['chart'] == 'longhorn' && source['targetRevision'] == '1.11.1' },
      'Longhorn must use its pinned Helm chart')
check(sources.any? { |source| source['ref'] == 'values' }, 'Longhorn Git values source missing')
check(sources.last == { 'repoURL' => 'https://github.com/dmuiX/k3s-on-omv-infra.git',
                        'targetRevision' => '454e34967db12ff4dcd869b8ca1947078eadd19e', 'path' => '02-longhorn',
                        'directory' => { 'include' => 'storageclass-configmap.yaml' } },
      'Encrypted StorageClass ConfigMap must be the final Argo source override')
check(cm['apiVersion'] == 'v1' && cm['kind'] == 'ConfigMap' &&
      cm.dig('metadata', 'name') == 'longhorn-storageclass' && cm.dig('metadata', 'namespace') == 'longhorn',
      'Override must match the chart ConfigMap exactly')
check(cm.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-options') == 'Prune=false',
      'Protect the controller-owned class template from accidental pruning')
check(cm.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '-1',
      'Sync the encrypted template before chart workloads')
check(cm.fetch('data').keys == ['storageclass.yaml'], 'No key material belongs in the ConfigMap')
check(sc['apiVersion'] == 'storage.k8s.io/v1' && sc['kind'] == 'StorageClass', 'Invalid class template')
check(sc.dig('metadata', 'name') == 'longhorn', 'Encrypt the standard longhorn class, not a second class')
check(sc.dig('metadata', 'annotations', 'storageclass.kubernetes.io/is-default-class') == 'false',
      'Keep local-path as the cluster default')
check(sc['provisioner'] == 'driver.longhorn.io' && sc['allowVolumeExpansion'] == true,
      'Incorrect CSI driver or missing expansion')
check(sc['reclaimPolicy'] == 'Retain' && sc['volumeBindingMode'] == 'Immediate', 'Wrong lifecycle policy')
params = sc.fetch('parameters')
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
%w[02-longhorn/config-app.yml 02-longhorn/storageclass-encrypted.yaml].each do |path|
  check(!File.exist?(File.join(ROOT, path)), 'Retired additional-class proposal is still selected')
end

# No workload/PVC-name changes or default-class switch are part of this rollout.
values = document('02-longhorn/values.yml')
check(values.dig('persistence', 'defaultClass') == false, 'Original class default changed')
check(values.dig('persistence', 'defaultClassReplicaCount') == 3, 'New PVC replica default must be three')
openbao = document('03-openbao/values.yml')
%w[dataStorage auditStorage].each do |storage|
  check(openbao.dig('server', storage, 'storageClass') == 'longhorn', 'Existing OpenBao PVC selection changed')
end
check(openbao.dig('server', 'ha', 'replicas') == 3, 'OpenBao must run one Raft voter per node')
puts 'PASS: encrypted standard longhorn class, multi-source ConfigMap override, three replicas, complete CSI references'
