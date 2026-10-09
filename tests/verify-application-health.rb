#!/usr/bin/env ruby
# Desired-state contract for generic app-of-apps sync and health propagation.
require 'yaml'
require 'open3'

ROOT = File.expand_path('..', __dir__)
config = YAML.load_file(File.join(ROOT, '01-bootstrap/argocd-bootstrap', 'application-health-config.yml'))
application_key = 'resource.customizations.health.argoproj.io_Application'
data = config.fetch('data')
raise 'Only generic Application health may be customized' unless data.keys == [application_key]

lua = data.fetch(application_key)
%w[longhorn.io postgres-operator.crunchydata.com].each do |workload_detail|
  raise "Application health must not inspect #{workload_detail}" if lua.include?(workload_detail)
end

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

fixtures = [
  [{}, ['Progressing', 'Waiting for child Application status']],
  [{'status' => {'sync' => {'status' => 'OutOfSync'},
                 'health' => {'status' => 'Healthy'}}},
   ['Progressing', 'Waiting for child Application sync: OutOfSync']],
  [{'status' => {'sync' => {'status' => 'Synced'},
                 'health' => {'status' => 'Healthy', 'message' => 'ready'}}},
   ['Healthy', 'ready']],
  [{'status' => {'operationState' => {'phase' => 'Running'},
                 'sync' => {'status' => 'Synced'},
                 'health' => {'status' => 'Healthy'}}},
   ['Progressing', 'Waiting for child Application operation to finish']],
  [{'status' => {'operationState' => {'phase' => 'Failed'},
                 'sync' => {'status' => 'Synced'},
                 'health' => {'status' => 'Healthy'}}},
   ['Degraded', 'Child Application sync Failed']],
  [{'status' => {'sync' => {'status' => 'Synced'}}},
   ['Progressing', 'Waiting for child Application health']],
  [{'status' => {'sync' => {'status' => 'Synced'},
                 'health' => {'status' => 'Degraded', 'message' => 'failed'}}},
   ['Degraded', 'failed']]
]
fixtures.each do |input, expected|
  program = "local function evaluate(obj)\n#{lua}\nend\n" \
            "local result = evaluate(#{lua_literal(input)})\nio.write(result.status .. '\\n' .. result.message)\n"
  stdout, stderr, status = Open3.capture3(*lua_command, '-e', program)
  raise "Lua health test failed: #{stderr}" unless status.success?
  raise "Expected #{expected.inspect}, got #{stdout.inspect}" unless stdout == expected.join("\n")
end

puts "PASS: generic Argo Application sync and health propagation (#{fixtures.length} cases)"
