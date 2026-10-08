#!/usr/bin/env ruby
# Offline Kubernetes render/least-privilege checks; never applies to a cluster.
require 'yaml'
require 'json'
require 'open3'

root = File.expand_path('..', __dir__)
app = YAML.load_file(File.join(root, '04-secrets/openbao-access-config', 'app.yml'))
raise 'Wrong OpenBao config Application wave' unless app.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '4'
raise 'Unexpected Argo Kustomize source' unless app.dig('spec', 'source') == {
  'repoURL' => 'https://github.com/dmuiX/k3s-on-omv-infra.git',
  'targetRevision' => '4d18067735ac2796b8edbbaa54f2db5fa05eff9c',
  'path' => '04-secrets/openbao-access-config/workload'
}
output, stderr, result = Open3.capture3('kubectl', 'kustomize', File.join(root, '04-secrets/openbao-access-config', 'workload'))
raise "Kustomize failed: #{stderr}" unless result.success?
resources = YAML.load_stream(output).compact
find = ->(kind) { resources.find { |r| r['kind'] == kind } || raise("Missing #{kind}") }
sa = find.call('ServiceAccount')
raise 'Dedicated SA not configured' unless sa.dig('metadata', 'name') == 'openbao-access-config' && sa['automountServiceAccountToken'] == false
config = find.call('ConfigMap')
raise 'ConfigMap must precede the initial Job' unless config.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '-1'
raise 'Expected Git-managed policies, roles and reconciler' unless config.fetch('data').keys.sort == %w[config.json human-admin.hcl openbao-snapshot.hcl reconcile.py vault-secrets-webhook-read.hcl]
role_config = JSON.parse(config.dig('data', 'config.json'))
roles = role_config.fetch('roles').to_h { |role| [role.fetch('name'), role] }
raise 'Unexpected webhook identity' unless roles.dig('vault-secrets-webhook', 'service_account') == 'vault-secrets-webhook' &&
                                             role_config['kv_mount'] == 'kv' && role_config['human_auth_mount'] == 'userpass'
raise 'Unexpected snapshot identity' unless roles.fetch('openbao-snapshot') == {
  'name' => 'openbao-snapshot', 'service_account' => 'openbao-snapshot', 'namespace' => 'openbao',
  'policies' => ['openbao-snapshot'], 'token_ttl' => '15m'
}
raise 'Unexpected managed policies' unless role_config.fetch('managed_policies').sort ==
                                           %w[human-admin openbao-snapshot vault-secrets-webhook-read]
raise 'Unexpected webhook policy' unless config.dig('data', 'vault-secrets-webhook-read.hcl').include?('path "kv/data/*"')
snapshot_acl = config.dig('data', 'openbao-snapshot.hcl')
raise 'Snapshot ACL is broader than native snapshot and one credential object' unless
  snapshot_acl.scan(/^path /).length == 2 &&
  snapshot_acl.include?('path "sys/storage/raft/snapshot"') &&
  snapshot_acl.include?('path "kv/data/openbao-snapshots/r2-credentials"') &&
  snapshot_acl.scan('capabilities = ["read"]').length == 2
policy = find.call('NetworkPolicy')
raise 'Wrong NetworkPolicy selector' unless policy.dig('spec', 'podSelector', 'matchLabels', 'app.kubernetes.io/name') == 'openbao-access-config'
raise 'Unrestricted ingress' unless policy.dig('spec', 'ingress') == []
[job = find.call('Job'), cron = find.call('CronJob')].each do |resource|
  pod = resource['kind'] == 'Job' ? resource.dig('spec', 'template') : resource.dig('spec', 'jobTemplate', 'spec', 'template')
  spec = pod.fetch('spec')
  raise 'Default ServiceAccount used' unless spec['serviceAccountName'] == 'openbao-access-config' && spec['automountServiceAccountToken'] == false
  raise 'No projected token' unless spec.fetch('volumes').any? { |v| v.dig('projected', 'sources', 0, 'serviceAccountToken', 'path') == 'token' }
  raise 'ConfigMap name not rewritten by Kustomize' unless spec.fetch('volumes').any? { |v| v.dig('configMap', 'name') == config.dig('metadata', 'name') }
  container = spec.fetch('containers').first
  raise 'No resource bounds' unless container.dig('resources', 'requests', 'cpu') && container.dig('resources', 'limits', 'memory')
  raise 'Image not pinned to digest' unless container.fetch('image').include?('@sha256:')
  raise 'Container can escalate' unless container.dig('securityContext', 'allowPrivilegeEscalation') == false
end
raise 'Initial Job is not an Argo sync hook' unless job.dig('metadata', 'annotations', 'argocd.argoproj.io/hook') == 'Sync'
raise 'CronJob must run after the initial Job' unless cron.dig('metadata', 'annotations', 'argocd.argoproj.io/sync-wave') == '1'
raise 'CronJob overlap not forbidden' unless cron.dig('spec', 'concurrencyPolicy') == 'Forbid'
puts 'PASS: OpenBao ACL ConfigMap, isolated ServiceAccount, scoped network, initial Job and CronJob render'
