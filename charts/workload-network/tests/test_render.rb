#!/usr/bin/env ruby
# Offline Helm only; capture output and retain only NetworkPolicies.
require 'minitest/autorun'
require 'yaml'
require 'json'
require 'open3'
require 'tmpdir'

class WorkloadNetworkTest < Minitest::Test
  CHART = File.expand_path('..', __dir__)
  FIXTURES = File.join(__dir__, 'fixtures')
  WORKSPACE = File.expand_path('../../..', CHART)
  APPS = File.join(WORKSPACE, 'k3s-on-omv-apps')
  LIVE = File.join(WORKSPACE, 'k3s-on-omv-live')

  def documents(text)
    YAML.load_stream(text).compact.select { |d| d['kind'] == 'NetworkPolicy' }
  end

  def fixture(name)
    YAML.load_file(File.join(FIXTURES, name))
  end

  def render(values)
    Dir.mktmpdir('network-test-') do |dir|
      path = File.join(dir, 'values.json')
      File.write(path, JSON.generate(values))
      out, err, status = Open3.capture3('helm', 'template', 'network', CHART, '-f', path)
      assert status.success?, err
      documents(out)
    end
  end

  def indexed(docs)
    keys = docs.map { |d| [d['apiVersion'], d['kind'], d['metadata']['namespace'], d['metadata']['name']] }
    assert_equal keys.uniq, keys, 'duplicate policy identities'
    docs.zip(keys).to_h { |doc, key| [key, doc] }
  end

  def test_full_radar_objects_equal_before_migration
    baseline = documents(File.read(File.join(FIXTURES, 'radar-baseline.yaml')))
    assert_equal indexed(baseline), indexed(render(fixture('radar-values.yaml')))
    actual = YAML.load_file(File.join(LIVE, 'apps/radar/network-values.yml'))
    # Keep the captured migration baseline immutable; model only this topic's delta.
    api_rules = [
      {'to' => [{'ipBlock' => {'cidr' => '10.43.0.1/32'}}],
       'ports' => [{'protocol' => 'TCP', 'port' => 443}]},
      {'to' => %w[192.168.1.2/32 192.168.1.5/32 192.168.1.7/32].map { |ip| {'ipBlock' => {'cidr' => ip}} },
       'ports' => [{'protocol' => 'TCP', 'port' => 6443}]}
    ]
    expected = Marshal.load(Marshal.dump(baseline))
    radar = expected.find { |d| d['metadata']['name'] == 'allow-radar' }
    assert_equal({'ports' => [{'protocol' => 'TCP', 'port' => 443},
                             {'protocol' => 'TCP', 'port' => 6443}]}, radar['spec']['egress'][-2])
    radar['spec']['egress'][-2, 1] = api_rules
    expected << {
      'apiVersion' => 'networking.k8s.io/v1', 'kind' => 'NetworkPolicy',
      'metadata' => {'name' => 'allow-radar-credentials-api', 'namespace' => 'radar',
                     'annotations' => {'argocd.argoproj.io/sync-wave' => '-2'}},
      'spec' => {'podSelector' => {'matchLabels' => {'app.kubernetes.io/name' => 'radar-credentials'}},
                 'policyTypes' => ['Egress'], 'egress' => api_rules}
    }
    assert_equal indexed(expected), indexed(render(actual))
    app = YAML.load_file(File.join(APPS, 'radar/app.yml'))
    sources = app['spec']['sources'].select { |s| s['path'] == 'charts/workload-network' }
    assert_equal 1, sources.length
    assert_equal ['$live/apps/radar/network-values.yml'], sources.first['helm']['valueFiles']
    assert app['spec']['sources'].any? { |s| s['path'] == 'radar/resources' && s['repoURL'].end_with?('/k3s-on-omv-apps.git') }
    refute File.exist?(File.join(APPS, 'radar/resources/network-policy.yaml'))
  end

  def test_repeated_rules_and_selector_scope
    abs = documents(File.read(File.join(FIXTURES, 'audiobookshelf-baseline.yaml')))
    configurator = abs.find { |d| d['metadata']['name'] == 'audiobookshelf-configurator' }
    policy = {
      'name' => 'configurator', 'podSelector' => configurator['spec']['podSelector'],
      'policyTypes' => ['Ingress', 'Egress'], 'dns' => true, 'openbao' => true,
      'extraEgress' => configurator['spec']['egress'].drop(2)
    }
    network = {'namespace' => 'audiobookshelf', 'policies' => [policy]}
    rendered = render({'workloadNetwork' => network}).first
    assert_equal configurator['spec']['egress'], rendered['spec']['egress']
    # The intended destination is namespace AND OpenBao pod, never OR.
    assert_equal 1, rendered['spec']['egress'][1]['to'].length
    assert_equal %w[namespaceSelector podSelector], rendered['spec']['egress'][1]['to'].first.keys.sort
    # A workload-only default deny remains workload-only, unlike Radar's {}.
    network['defaultDeny'] = {'name' => 'deny', 'podSelector' => policy['podSelector'], 'policyTypes' => ['Ingress', 'Egress']}
    assert_equal policy['podSelector'], render({'workloadNetwork' => network}).first['spec']['podSelector']
  end

  def test_audiobookshelf_retains_all_native_policies
    out, err, status = Open3.capture3('helm', 'template', 'audiobookshelf', File.join(APPS, 'audiobookshelf/chart'),
                                     '-n', 'audiobookshelf', '-f', File.join(LIVE, 'apps/audiobookshelf/values.yml'))
    assert status.success?, err
    baseline = documents(File.read(File.join(FIXTURES, 'audiobookshelf-baseline.yaml')))
    # The baseline predates the already-merged Apps image-tag label fix.
    # Permit that non-selector metadata label only; compare complete specs.
    tag = YAML.load_file(File.join(LIVE, 'apps/audiobookshelf/values.yml')).dig('image', 'tag') ||
          YAML.load_file(File.join(APPS, 'audiobookshelf/chart/values.yaml')).dig('image', 'tag')
    baseline.each do |doc|
      labels = doc.dig('metadata', 'labels')
      labels['app.kubernetes.io/version'] = tag if labels&.key?('app.kubernetes.io/version')
    end
    assert_equal indexed(baseline), indexed(documents(out))
    peers = baseline.find { |d| d['metadata']['name'] == 'audiobookshelf-allow' }['spec']['ingress'].first['from']
    assert_equal 2, peers.length # Gateway namespace OR same-namespace configurator.
    assert_equal ['namespaceSelector'], peers[0].keys
    assert_equal ['podSelector'], peers[1].keys
  end

  def reject(values)
    Dir.mktmpdir('network-invalid-') do |dir|
      path = File.join(dir, 'values.json')
      File.write(path, JSON.generate(values))
      _out, _err, status = Open3.capture3('helm', 'template', 'invalid', CHART, '-f', path)
      refute status.success?, 'unsafe/incomplete settings rendered successfully'
    end
  end

  def test_invalid_configuration_fails
    reject({})
    [
      ->(n, p) { n.delete('namespace') },
      ->(n, p) { p.delete('podSelector') },
      ->(n, p) { p['podSelector'] = {} },
      ->(n, p) { p['podSelector'] = {'matchLabels' => {}} },
      ->(n, p) { p['podSelector'] = {'matchExpressions' => []} },
      ->(n, p) { p['policyTypes'] = [] },
      ->(n, p) { p['policyTypes'] = ['Ingress'] },
      ->(n, p) { p['policyTypes'] = ['Egress'] },
      ->(n, p) { p['gatewayIngress']['namespace'] = '' },
      ->(n, p) { p['gatewayIngress']['ports'] = [] },
      ->(n, p) { p['gatewayIngress']['ports'] = [0] },
      ->(n, p) { p['extraEgress'] = [{}] },
      ->(n, p) { p['extraEgress'] = [{'ports' => []}] },
      ->(n, p) { p['extraEgress'][0]['to'] = [{}] },
      ->(n, p) { p['extraEgress'][0]['from'] = p['extraEgress'][0].delete('to') },
      ->(n, p) { p['name'] = n['defaultDeny']['name'] },
      ->(n, p) { n['policies'] << Marshal.load(Marshal.dump(p)) }
    ].each do |mutate|
      values = fixture('radar-values.yaml')
      n = values['workloadNetwork']
      mutate.call(n, n['policies'].first)
      reject(values)
    end
  end
end
