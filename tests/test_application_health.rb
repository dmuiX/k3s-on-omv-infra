# Execute the actual Helm-embedded Lua through Argo CD's offline health evaluator.
require "minitest/autorun"
require "yaml"
require "json"
require "tmpdir"
require "open3"

class ApplicationHealthTest < Minitest::Test
  ROOT = File.expand_path("..", __dir__)
  KEY = "resource.customizations.health.argoproj.io_Application"

  def application(multi: true)
    sources = [{"repoURL" => "https://example.com/infra.git", "path" => "resources", "targetRevision" => "main"}]
    sources << {"repoURL" => "https://example.com/live.git", "ref" => "live", "targetRevision" => "main"} if multi
    source_spec = multi ? {"sources" => sources} : {"source" => sources.first}
    revisions = multi ? {"revisions" => ["infra-commit", "live-commit"]} : {"revision" => "infra-commit"}
    spec = source_spec.merge("destination" => {"name" => "in-cluster", "namespace" => "test"})
    {
      "apiVersion" => "argoproj.io/v1alpha1", "kind" => "Application",
      "metadata" => {"name" => "test", "namespace" => "argocd"},
      "spec" => spec,
      "status" => {
        "sync" => revisions.merge("status" => "Synced", "comparedTo" => Marshal.load(Marshal.dump(spec))),
        "health" => {"status" => "Healthy"},
        "operationState" => {
          "phase" => "Succeeded", "operation" => {"sync" => {}},
          "syncResult" => Marshal.load(Marshal.dump(source_spec.merge(revisions)))
        }
      }
    }
  end

  def assess(app)
    Dir.mktmpdir("argocd-health-test") do |dir|
      definition = YAML.load_file(File.join(ROOT, "01-bootstrap/argocd-config/app.yml"))
      values = definition.fetch("spec").fetch("sources").find { |source| source["chart"] == "argo-cd" }.fetch("helm").fetch("valuesObject")
      cm = {"apiVersion" => "v1", "kind" => "ConfigMap", "metadata" => {"name" => "argocd-cm"},
            "data" => {KEY => values.fetch("configs").fetch("cm").fetch(KEY)}}
      cm_path = File.join(dir, "cm.yaml")
      app_path = File.join(dir, "app.yaml")
      File.write(cm_path, YAML.dump(cm))
      File.write(app_path, YAML.dump(app))
      output, error, status = Open3.capture3("argocd", "admin", "settings", "resource-overrides", "health",
                                           app_path, "--argocd-cm-path", cm_path, "--load-cluster-settings=false")
      assert status.success?, "Health evaluator failed: #{error}\n#{output}"
      output
    end
  end

  def assert_health(expected, app)
    assert_match(/\b#{expected}\b/, assess(app))
  end

  def test_current_successful_sync_preserves_real_health
    [true, false].each do |multi|
      %w[Healthy Progressing Degraded Missing Suspended].each do |health|
        app = application(multi: multi)
        app["status"]["health"]["status"] = health
        assert_health(health, app)
      end
    end
  end

  def test_current_successful_full_sync_allows_drift
    [true, false].each do |multi|
      app = application(multi: multi)
      app["status"]["sync"]["status"] = "OutOfSync"
      assert_health("Healthy", app)
    end
  end

  def test_operation_states_override_cached_healthy_status
    {"Running" => "Progressing", "Terminating" => "Progressing", "Failed" => "Degraded", "Error" => "Degraded"}.each do |phase, expected|
      app = application
      app["status"]["operationState"]["phase"] = phase
      assert_health(expected, app)
    end
    app = application
    app["operation"] = {"sync" => {}}
    assert_health("Progressing", app)
  end

  def test_missing_or_unknown_status_blocks
    app = application
    app.delete("status")
    assert_health("Progressing", app)
    app = application
    app["status"].delete("operationState")
    assert_health("Progressing", app)
    app = application
    app["status"]["sync"]["status"] = "Unknown"
    assert_health("Progressing", app)
  end

  def test_old_success_does_not_release_new_revision
    [true, false].each do |multi|
      app = application(multi: multi)
      app["status"]["sync"]["status"] = "OutOfSync"
      if multi
        app["status"]["sync"]["revisions"][1] = "new-live-commit"
      else
        app["status"]["sync"]["revision"] = "new-infra-commit"
      end
      assert_health("Progressing", app)
    end
  end

  def test_synced_no_op_revision_does_not_require_redundant_sync
    app = application
    app["status"]["sync"]["revisions"][0] = "unrelated-commit"
    assert_health("Healthy", app)
  end

  def test_unreconciled_source_or_destination_change_blocks
    app = application
    app["spec"]["sources"][0]["path"] = "different-path"
    assert_health("Progressing", app)
    app = application
    app["spec"]["destination"]["namespace"] = "different-namespace"
    assert_health("Progressing", app)
  end

  def test_same_revision_with_changed_helm_values_is_not_drift
    app = application
    app["status"]["sync"]["status"] = "OutOfSync"
    app["spec"]["sources"][0]["helm"] = {"values" => "replicas: 3"}
    app["status"]["sync"]["comparedTo"] = Marshal.load(Marshal.dump(app["spec"]))
    assert_health("Progressing", app)
  end

  def test_partial_or_dry_run_success_cannot_justify_drift
    [{"resources" => [{"kind" => "Deployment", "name" => "one"}]}, {"dryRun" => true}].each do |sync|
      app = application
      app["status"]["sync"]["status"] = "OutOfSync"
      app["status"]["operationState"]["operation"]["sync"] = sync
      assert_health("Progressing", app)
    end
  end

  def test_missing_revision_evidence_blocks_drift
    app = application
    app["status"]["sync"]["status"] = "OutOfSync"
    app["status"]["operationState"]["syncResult"].delete("revisions")
    assert_health("Progressing", app)
  end

  def test_comparison_and_spec_errors_block
    %w[ComparisonError InvalidSpecError SyncError].each do |type|
      app = application
      app["status"]["conditions"] = [{"type" => type, "message" => "Test error"}]
      assert_health("Degraded", app)
    end
  end
end
