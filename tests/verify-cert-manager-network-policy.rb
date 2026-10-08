#!/usr/bin/env ruby
require 'yaml'
require 'open3'

ROOT = File.expand_path('..', __dir__)
CHART = File.join(ROOT, '02-controllers/cert-manager/network-policy')

def check(condition, message)
  raise message unless condition
end

output, status = Open3.capture2('helm', 'template', 'cert-manager-network-policy', CHART,
                                '--set-json', 'clusterNetwork.kubernetesApiServerEndpointCIDRs=["192.0.2.2/32","192.0.2.5/32","192.0.2.7/32"]',
                                err: File::NULL)
check(status.success?, 'Dedicated cert-manager NetworkPolicy chart did not render')
_, short_status = Open3.capture2('helm', 'template', 'invalid', CHART,
                                 '--set-json', 'clusterNetwork.kubernetesApiServerEndpointCIDRs=["192.0.2.2/32","192.0.2.5/32"]',
                                 err: File::NULL)
_, invalid_status = Open3.capture2('helm', 'template', 'invalid', CHART,
                                   '--set-json', 'clusterNetwork.kubernetesApiServerEndpointCIDRs=["192.0.2.2/32","192.0.2.5/32","999.0.2.7/32"]',
                                   err: File::NULL)
check(!short_status.success? && !invalid_status.success?,
      'Dedicated chart must require exactly three valid IPv4 /32 endpoints')
resources = YAML.load_stream(output).compact
check(resources.length == 1, 'Dedicated chart must own exactly one resource')
policy = resources.first
check(policy['kind'] == 'NetworkPolicy' &&
      policy.dig('metadata', 'name') == 'cert-manager-controller-egress' &&
      policy.dig('metadata', 'namespace') == 'cert-manager',
      'Dedicated chart must render the uniquely owned cert-manager NetworkPolicy')
check(policy.dig('metadata', 'name') != 'cert-manager-openbao-pki-egress',
      'Dedicated policy must not collide with the predecessor OpenBao PKI Application')
check(policy.dig('spec', 'podSelector', 'matchLabels') == {
        'app.kubernetes.io/name' => 'cert-manager',
        'app.kubernetes.io/component' => 'controller'
      }, 'NetworkPolicy must select only cert-manager controller pods')
ports = policy.dig('spec', 'egress').flat_map { |entry| entry.fetch('ports') }
              .map { |port| port['port'] }.sort
check(ports == [53, 53, 443, 6443, 8200], 'Unexpected cert-manager egress ports')
api_rule = policy.dig('spec', 'egress').find do |entry|
  entry.fetch('ports').map { |port| [port['protocol'], port['port']] } == [['TCP', 6443]]
end
check(api_rule.fetch('to').map { |peer| peer.dig('ipBlock', 'cidr') }.sort ==
      %w[192.0.2.2/32 192.0.2.5/32 192.0.2.7/32],
      'K3s API egress must use only rendered /32 endpoint CIDRs')
puts 'PASS: cert-manager owns a private-value-rendered, port-limited K3s API egress policy'
