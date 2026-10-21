# frozen_string_literal: true

require "minitest/autorun"
require "open3"
require "pathname"
require "yaml"
require "tempfile"

# Explicit local chart cache only: never resolve a repository or contact a cluster.
class MeshRenderTest < Minitest::Test
  WORKSPACE = Pathname(__dir__).parent.parent
  INFRA = WORKSPACE / "k3s-on-omv-infra"
  LIVE = WORKSPACE / "k3s-on-omv-live"

  def render(chart, release, namespace)
    cache = ENV["CHART_CACHE_DIR"]
    skip "Set CHART_CACHE_DIR to extracted official charts for offline render checks" unless cache
    path = Pathname(cache) / chart
    assert path.join("Chart.yaml").file?, "missing extracted chart #{path}"
    metadata = YAML.load_file(path / "Chart.yaml")
    assert_equal "1.31.2", metadata["version"]
    command = ["helm", "template", release, path.to_s, "--namespace", namespace, "--skip-tests"]
    application = YAML.load_file(INFRA / "01-bootstrap/istio/app.yml")
    source = application.fetch("spec").fetch("sources").find { |item| item["chart"] == chart }
    Tempfile.create(["istio-inline-values", ".yaml"]) do |values|
      values.write(YAML.dump(source.fetch("helm").fetch("valuesObject")))
      values.flush
      command.concat(["--values", values.path])
      output, error, status = Open3.capture3(*command)
      assert status.success?, error
      YAML.load_stream(output).compact
    end
  end

  def test_base_and_istiod
    base = render("base", "istio-base", "istio-system")
    assert base.any? { |document| document["kind"] == "CustomResourceDefinition" }
    istiod = render("istiod", "istio-istiod", "istio-system")
    deployment = istiod.find { |document| document["kind"] == "Deployment" }
    assert_equal "registry.istio.io/release/pilot:1.31.2-distroless", deployment.dig("spec", "template", "spec", "containers", 0, "image")
    [base, istiod].each do |documents|
      webhook = documents.find { |document| document["kind"] == "ValidatingWebhookConfiguration" }
      assert webhook.fetch("webhooks").all? { |item| item["failurePolicy"] == "Fail" }
    end
  end

  def test_cni_and_ztunnel
    cni = render("cni", "istio-cni", "istio-system")
    ztunnel = render("ztunnel", "ztunnel", "istio-system")
    cni_pod = cni.find { |document| document["kind"] == "DaemonSet" }.dig("spec", "template", "spec")
    paths = cni_pod.fetch("volumes").map { |volume| volume.dig("hostPath", "path") }.compact
    assert_includes paths, "/var/lib/rancher/k3s/data/cni"
    assert_includes paths, "/var/lib/rancher/k3s/agent/etc/cni/net.d"
    assert_includes paths, "/var/run/ztunnel"
    config = cni.find { |document| document["kind"] == "ConfigMap" && document.dig("metadata", "name") == "istio-cni-config" }
    assert_equal "true", config.dig("data", "AMBIENT_ENABLED")
    [cni, ztunnel].each do |documents|
      pod = documents.find { |document| document["kind"] == "DaemonSet" }.dig("spec", "template", "spec")
      assert pod.fetch("tolerations").any? { |item| item["key"] == "CriticalAddonsOnly" }
      assert pod.fetch("containers").first.dig("resources", "limits")
      assert pod.fetch("containers").first["readinessProbe"]
      assert pod.fetch("volumes").any? { |volume| volume.dig("hostPath", "path") == "/var/run/ztunnel" }
    end
  end

end
