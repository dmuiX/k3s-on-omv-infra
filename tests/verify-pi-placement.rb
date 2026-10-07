#!/usr/bin/env ruby
# Offline contract for the restricted Raspberry Pi workload-placement taint.
require 'yaml'

ROOT = File.expand_path('..', __dir__)
EXPECTED = {
  'key' => 'workload-placement',
  'operator' => 'Equal',
  'value' => 'restricted',
  'effect' => 'NoSchedule'
}.freeze

def check(condition, message)
  raise message unless condition
end

def yaml(path)
  YAML.load_file(File.join(ROOT, path))
end

def permits_pi?(tolerations)
  Array(tolerations).include?(EXPECTED)
end

cert_manager = yaml('02-controllers/cert-manager/values.yml')
check(permits_pi?(cert_manager['tolerations']), 'cert-manager controller cannot use raspi4')
check(permits_pi?(cert_manager.dig('webhook', 'tolerations')), 'cert-manager webhook cannot use raspi4')
check(permits_pi?(cert_manager.dig('cainjector', 'tolerations')), 'cert-manager cainjector cannot use raspi4')
check(permits_pi?(cert_manager.dig('startupapicheck', 'tolerations')), 'cert-manager startup check cannot use raspi4')

k8up = yaml('02-controllers/k8up/values.yml')
check(permits_pi?(k8up['tolerations']), 'K8up controller cannot use raspi4')
check(permits_pi?(k8up.dig('cleanup', 'tolerations')), 'K8up cleanup cannot use raspi4')
longhorn = yaml('02-controllers/longhorn/values.yml')
check(permits_pi?(longhorn.dig('global', 'tolerations')),
      'Longhorn system workloads cannot use raspi4')
check(longhorn.dig('defaultSettings', 'taintToleration') == 'workload-placement=restricted:NoSchedule',
      'Longhorn-created instance managers cannot use raspi4')
check(permits_pi?(yaml('03-core/openbao/values.yml').dig('server', 'tolerations')),
      'OpenBao cannot place its third voter on raspi4')
check(permits_pi?(yaml('04-secrets/vault-secrets-webhook/values.yml')['tolerations']),
      'Vault Secrets Webhook cannot use raspi4')
check(permits_pi?(yaml('06-data/postgresql/values-cloudnativepg.yml')['tolerations']),
      'CloudNativePG operator cannot use raspi4')
check(permits_pi?(yaml('06-data/postgresql/values-barman.yml')['tolerations']),
      'Barman plugin cannot use raspi4')
check(permits_pi?(yaml('06-data/postgresql/cluster/cluster.yaml').dig('spec', 'affinity', 'tolerations')),
      'PostgreSQL instances cannot place the third member on raspi4')

[
  ['04-secrets/openbao-access-config/workload/initial-job.yaml', %w[spec template spec tolerations]],
  ['04-secrets/openbao-access-config/workload/cronjob.yaml', %w[spec jobTemplate spec template spec tolerations]],
  ['05-platform/openbao-pki/workload/initial-job.yaml', %w[spec template spec tolerations]],
  ['05-platform/openbao-pki/workload/cronjob.yaml', %w[spec jobTemplate spec template spec tolerations]]
].each do |path, keys|
  value = keys.reduce(yaml(path)) { |current, key| current.fetch(key) }
  check(permits_pi?(value), "#{path} cannot use raspi4")
end

monitoring = yaml('03-core/kube-prometheus-stack/values.yml')
check(!permits_pi?(monitoring['tolerations']), 'Monitoring stack must not tolerate the restricted Pi taint')
node_terms = monitoring.dig('prometheus-node-exporter', 'affinity', 'nodeAffinity',
                            'requiredDuringSchedulingIgnoredDuringExecution', 'nodeSelectorTerms')
excludes_pi = Array(node_terms).any? do |term|
  Array(term['matchExpressions']).any? do |expression|
    expression == {'key' => 'kubernetes.io/hostname', 'operator' => 'NotIn', 'values' => ['raspi4']}
  end
end
check(excludes_pi, 'Prometheus node exporter must exclude raspi4')

puts 'PASS: infra workloads explicitly tolerate restricted raspi4; monitoring excludes it'
