#!/usr/bin/env ruby
# Offline contract for the Argo CD child-Application health Lua script.
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

lua_bin = ENV['LUA'] || %w[lua lua5.4 lua5.3 luajit].find { |name| system('which', name, out: File::NULL, err: File::NULL) }
abort 'Lua is required to parse and execute the Application health fixtures (set LUA=/path/to/lua)' unless lua_bin
fixtures.each do |input, expected|
  program = "local function evaluate(obj)\n#{lua}\nend\n" \
            "local result = evaluate(#{lua_literal(input)})\nio.write(result.status)\n"
  stdout, stderr, status = Open3.capture3(lua_bin, '-e', program)
  raise "Lua health test failed: #{stderr}" unless status.success?
  raise "Expected #{expected}, got #{stdout} for #{input.inspect}" unless stdout == expected
end
puts "PASS: Argo child-Application health Lua (#{fixtures.length} cases)"
