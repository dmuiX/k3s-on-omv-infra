#!/usr/bin/env ruby
# Offline only: Helm template and local Kustomize builds, never cluster queries.
require 'minitest/autorun'
require 'yaml'
require 'json'
require 'open3'
require 'tmpdir'
require 'fileutils'

class OpenbaoOIDCTest < Minitest::Test
  CHART = File.expand_path('..', __dir__)
  FIXTURES = File.join(__dir__, 'fixtures')
  INFRA = File.expand_path('../..', CHART)
  WORKSPACE = File.dirname(INFRA)
  APPS = File.join(WORKSPACE, 'k3s-on-omv-apps')
  LIVE = File.join(WORKSPACE, 'k3s-on-omv-live')
  CONSUMERS = {
    'radar' => 'apps/radar/oidc-values.yml',
    'audiobookshelf' => 'apps/audiobookshelf/values.yml',
    'longhorn' => 'infra/longhorn/oidc-values.yml'
  }.freeze

  def documents(text)
    YAML.load_stream(text).compact
  end

  def identity(doc)
    [doc.fetch('apiVersion'), doc.fetch('kind'), doc.fetch('metadata').fetch('namespace', ''), doc.fetch('metadata').fetch('name')]
  end

  def indexed(docs)
    keys = docs.map { |d| identity(d) }
    assert_equal keys.uniq, keys, 'duplicate desired resource identities'
    docs.to_h { |d| [identity(d), d] }
  end

  def render(path, chart: CHART, name: 'test', namespace: 'default')
    out, err, status = Open3.capture3('helm', 'template', name, chart, '-n', namespace, '-f', path)
    assert status.success?, err
    documents(out)
  end

  def fixture(name, suffix)
    File.join(FIXTURES, "#{name}-#{suffix}.yaml")
  end

  def oidc?(doc)
    doc['apiVersion'] == 'identity.vault.upbound.io/v1alpha1'
  end

  def scrub(text, name, values)
    oidc = values.fetch('openbaoOIDC')
    authority = ->(url) { url.delete_prefix('https://').split('/').first }
    text.gsub(authority.call(oidc.fetch('provider').fetch('issuerURL')), 'issuer.example.invalid:9443')
        .gsub(authority.call(oidc.fetch('client').fetch('redirectUris').first), "#{name}.example.invalid:9443")
  end

  def test_exact_pre_migration_objects
    CONSUMERS.each_key do |name|
      assert_equal indexed(documents(File.read(fixture(name, 'baseline')))),
                   indexed(render(fixture(name, 'values'))), name
    end
  end

  def rejection(values)
    Dir.mktmpdir('oidc-invalid-') do |dir|
      path = File.join(dir, 'values.yaml')
      File.write(path, JSON.generate(values))
      _out, err, status = Open3.capture3('helm', 'template', 'invalid', CHART, '-f', path)
      refute status.success?, 'invalid or incomplete OIDC configuration rendered'
      assert_match(/openbaoOIDC|dedicated provider|this key/, err)
    end
  end

  def test_defaults_and_missing_required_values_fail
    rejection({})
    schema = JSON.parse(File.read(File.join(CHART, 'values.schema.json')))
    integration = schema.fetch('properties').fetch('openbaoOIDC')
    # Exercise each required field in every supported object, not only the root.
    walk = lambda do |definition, value, path|
      if definition['type'] == 'object'
        definition.fetch('required', []).each do |key|
          values = Marshal.load(Marshal.dump(@required_fixture))
          target = path.reduce(values) { |v, p| v.fetch(p) }
          target.delete(key)
          rejection(values)
        end
        definition.fetch('properties').each do |key, child|
          walk.call(child, value[key], path + [key]) if value.key?(key)
        end
      elsif definition['type'] == 'array'
        value.each_with_index { |item, i| walk.call(definition.fetch('items'), item, path + [i]) }
      end
    end
    CONSUMERS.each_key do |name|
      @required_fixture = YAML.load_file(fixture(name, 'values'))
      walk.call(integration, @required_fixture.fetch('openbaoOIDC'), ['openbaoOIDC'])
    end
  end

  def test_unsafe_or_mistyped_configuration_fails
    [
      [%w[provider issuerURL], 'http://issuer.example.invalid/v1/identity/oidc/provider/radar'],
      [%w[provider issuerURL], 'https://issuer.example.invalid/wrong-provider'],
      [%w[provider issuerURL], 'https://user@issuer.example.invalid/v1/identity/oidc/provider/radar'],
      [%w[client redirectUris], ['http://radar.example.invalid/callback']],
      [%w[client redirectUris], []],
      [%w[client assignments], []],
      [%w[client key], 'different-key'],
      [%w[provider clientRefs], ['different-client']],
      [%w[key algorithm], 'none'],
      [%w[client idTokenTtl], 0],
      [%w[client arbitrarySpec], {}]
    ].each do |path, value|
      values = YAML.load_file(fixture('radar', 'values'))
      target = path[0...-1].reduce(values.fetch('openbaoOIDC')) { |v, p| v.fetch(p) }
      target[path.last] = value
      rejection(values)
    end
  end

  def test_local_consumer_adoption
    skip 'sibling Apps/Live checkouts not present' unless File.directory?(APPS) && File.directory?(LIVE)
    applications = {
      'radar' => File.join(APPS, 'radar/app.yml'),
      'audiobookshelf' => File.join(APPS, 'audiobookshelf/app.yml'),
      'longhorn' => File.join(INFRA, '06-platform/longhorn-config/app.yml')
    }
    CONSUMERS.each do |name, value_path|
      app = YAML.load_file(applications.fetch(name))
      sources = app.fetch('spec').fetch('sources')
      shared = sources.select { |s| s['path'] == 'charts/openbao-oidc' }
      assert_equal 1, shared.length
      assert_equal ["$live/#{value_path}"], shared.first.fetch('helm').fetch('valueFiles')
      assert_equal 1, sources.count { |s| s['ref'] == 'live' }
      generated = render(File.join(LIVE, value_path))
      assert_equal indexed(documents(File.read(fixture(name, 'baseline')))),
                   indexed(documents(scrub(YAML.dump_stream(*generated), name, YAML.load_file(File.join(LIVE, value_path))))), name
      # Cluster-scoped Crossplane identities must NOT acquire a namespace from
      # the chart release. The connection Secret namespace is explicit instead.
      refute generated.any? { |d| d['metadata'].key?('namespace') }
    end

    workload = render(File.join(LIVE, CONSUMERS.fetch('audiobookshelf')),
                      chart: File.join(APPS, 'audiobookshelf/chart'),
                      name: 'audiobookshelf', namespace: 'audiobookshelf')
    refute workload.any? { |d| oidc?(d) }, 'workload still owns OIDC identities'
    access = workload.select { |d| %w[Policy AuthBackendRole ServiceAccount VaultConnection VaultAuth VaultDynamicSecret].include?(d['kind']) && d['metadata']['name'].start_with?('audiobookshelf') }
    expected = documents(File.read(File.join(FIXTURES, 'audiobookshelf-access-baseline.yaml')))
    # The chart has other configurator-specific access objects: compare the six
    # previously OIDC-local resources by identity, without changing their specs.
    actual = indexed(access)
    indexed(expected).each { |id, doc| assert_equal doc, actual.fetch(id), id.inspect }
    indexed(workload + render(File.join(LIVE, CONSUMERS.fetch('audiobookshelf'))))
    assert_equal false, YAML.load_file(applications.fetch('audiobookshelf')).dig('spec', 'syncPolicy', 'automated', 'enabled')
    assert_equal YAML.load_file(File.join(LIVE, 'apps/audiobookshelf/values.yml')).dig('oidc', 'issuerURL'),
                 YAML.load_file(File.join(LIVE, 'apps/audiobookshelf/values.yml')).dig('openbaoOIDC', 'provider', 'issuerURL')

    # The remaining plain Radar resources are now a direct public directory
    # source, not a private Kustomize wrapper.
    radar = Dir.glob(File.join(APPS, 'radar/resources/*.yaml')).sort.flat_map { |path| documents(File.read(path)) }
    refute radar.any? { |d| oidc?(d) }
    indexed(radar + render(File.join(LIVE, CONSUMERS.fetch('radar'))))
    longhorn = render(File.join(LIVE, 'infra/longhorn/proxy-values.yml'),
                      chart: File.join(INFRA, 'charts/oidc-proxy'),
                      name: 'longhorn-oidc-proxy', namespace: 'longhorn')
    assert_equal %w[ConfigMap Deployment Service], longhorn.map { |d| d['kind'] }.sort
    # The shared service-route chart now owns the unchanged Longhorn proxy route.
    refute longhorn.any? { |d| oidc?(d) }
    indexed(longhorn + render(File.join(LIVE, CONSUMERS.fetch('longhorn'))))
  end
end
