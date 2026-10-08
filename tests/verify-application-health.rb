#!/usr/bin/env ruby
# Local contract for the Argo CD child-Application health Lua script.
require 'yaml'
require 'open3'

ROOT = File.expand_path('..', __dir__)
config = YAML.load_file(File.join(ROOT, '01-bootstrap/argocd-bootstrap', 'application-health-config.yml'))
lua = config.fetch('data').fetch('resource.customizations.health.argoproj.io_Application')

raise 'Failed child sync must be Degraded' unless lua.include?('status.operationState.phase == "Failed"') &&
                                                  lua.include?('status.operationState.phase == "Error"') &&
                                                  lua.include?('hs.status = "Degraded"')
raise 'OutOfSync child must not be Healthy' unless lua.include?('status.sync.status ~= "Synced"')
raise 'Wait for unknown health' unless lua.include?('Waiting for child Application health')
raise 'Wait for pending/running operations' unless lua.include?('obj.operation ~= nil') &&
                                                  lua.include?('status.operationState.phase == "Running"') &&
                                                  lua.include?('status.operationState.phase == "Terminating"')

fixtures = [
  [{}, 'Progressing'],
  [{ 'operation' => {}, 'status' => { 'health' => { 'status' => 'Healthy' },
                                     'sync' => { 'status' => 'Synced' } } }, 'Progressing'],
  [{ 'status' => { 'health' => { 'status' => 'Healthy' }, 'sync' => { 'status' => 'Synced' },
                   'operationState' => { 'phase' => 'Running' } } }, 'Progressing'],
  [{ 'status' => { 'health' => { 'status' => 'Healthy' }, 'sync' => { 'status' => 'Synced' },
                   'operationState' => { 'phase' => 'Terminating' } } }, 'Progressing'],
  [{ 'operation' => {}, 'status' => { 'health' => { 'status' => 'Healthy' },
                                     'sync' => { 'status' => 'Synced' },
                                     'operationState' => { 'phase' => 'Failed' } } }, 'Progressing'],
  [{ 'status' => { 'health' => { 'status' => 'Healthy' }, 'sync' => { 'status' => 'OutOfSync' },
                   'operationState' => { 'phase' => 'Failed' } } }, 'Degraded'],
  [{ 'status' => { 'health' => { 'status' => 'Healthy' }, 'sync' => { 'status' => 'Synced' },
                   'operationState' => { 'phase' => 'Error' } } }, 'Degraded'],
  [{ 'status' => { 'health' => { 'status' => 'Healthy' }, 'sync' => { 'status' => 'OutOfSync' } } }, 'Progressing'],
  [{ 'status' => { 'health' => { 'status' => 'Healthy' } } }, 'Progressing'],
  [{ 'status' => { 'sync' => { 'status' => 'Synced' } } }, 'Progressing'],
  [{ 'status' => { 'health' => { 'status' => 'Progressing' }, 'sync' => { 'status' => 'Synced' } } }, 'Progressing'],
  [{ 'status' => { 'health' => { 'status' => 'Degraded' }, 'sync' => { 'status' => 'OutOfSync' } } }, 'Degraded'],
  [{ 'status' => { 'health' => { 'status' => 'Healthy' }, 'sync' => { 'status' => 'Synced' },
                   'operationState' => { 'phase' => 'Succeeded' } } }, 'Healthy']
]

def lua_literal(value)
  case value
  when Hash
    '{' + value.map { |key, child| "[#{key.dump}]=#{lua_literal(child)}" }.join(', ') + '}'
  when String then value.dump
  else raise "Unexpected fixture type: #{value.class}"
  end
end

lua_bin = ENV['LUA'] || %w[lua lua5.4 lua5.3 luajit].find do |name|
  system('which', name, out: File::NULL, err: File::NULL)
end
lua_command = if lua_bin
                [lua_bin]
              elsif system('which', 'npx', out: File::NULL, err: File::NULL)
                %w[npx --yes --package fengari-node-cli@0.1.0 fengari]
              end
abort 'Lua fixtures require Lua or npx (set LUA=/path/to/lua to override)' unless lua_command
fixtures.each do |input, expected|
  program = "local function evaluate(obj)\n#{lua}\nend\n" \
            "local result = evaluate(#{lua_literal(input)})\nio.write(result.status)\n"
  stdout, stderr, status = Open3.capture3(*lua_command, '-e', program)
  raise "Lua health test failed: #{stderr}" unless status.success?
  raise "Expected #{expected}, got #{stdout} for #{input.inspect}" unless stdout == expected
end

postgres_key = 'resource.customizations.health.postgres-operator.crunchydata.com_PostgresCluster'
healthy_postgres = {
  'metadata' => {'generation' => 7},
  'status' => {
    'observedGeneration' => 7,
    'instances' => [{'replicas' => 3, 'readyReplicas' => 3}],
    'pgbackrest' => {'repos' => [{'name' => 'repo1', 'stanzaCreated' => true}]},
    'patroni' => {'systemIdentifier' => 'present'},
    'databaseRevision' => 'present', 'usersRevision' => 'present'
  }
}
platform_fixtures = [
  [healthy_postgres, 'Healthy'],
  [healthy_postgres.merge('status' => healthy_postgres['status'].merge('observedGeneration' => 6)), 'Progressing'],
  [healthy_postgres.merge('status' => healthy_postgres['status'].merge(
    'instances' => [{'replicas' => 3, 'readyReplicas' => 2}])), 'Progressing'],
  [healthy_postgres.merge('status' => healthy_postgres['status'].merge(
    'conditions' => [{'observedGeneration' => 7, 'status' => 'False',
                      'reason' => 'Invalid', 'message' => 'invalid spec'}])), 'Degraded']
]

def lua_literal_with_scalars(value)
  case value
  when Hash
    '{' + value.map { |key, child| "[#{key.dump}]=#{lua_literal_with_scalars(child)}" }.join(', ') + '}'
  when Array
    '{' + value.map { |child| lua_literal_with_scalars(child) }.join(', ') + '}'
  when String then value.dump
  when Integer then value.to_s
  when TrueClass then 'true'
  when FalseClass then 'false'
  else
    raise "Unexpected platform fixture type: #{value.class}"
  end
end

script = config.fetch('data').fetch(postgres_key)
platform_fixtures.each do |input, expected|
  program = "local function evaluate(obj)\n#{script}\nend\n" \
            "local result = evaluate(#{lua_literal_with_scalars(input)})\nio.write(result.status)\n"
  stdout, stderr, status = Open3.capture3(*lua_command, '-e', program)
  raise "Lua platform health test failed for #{postgres_key}: #{stderr}" unless status.success?
  raise "Expected #{expected}, got #{stdout} for #{postgres_key}" unless stdout == expected
end
puts "PASS: Argo health Lua (#{fixtures.length} Application + #{platform_fixtures.length} PostgresCluster cases)"
