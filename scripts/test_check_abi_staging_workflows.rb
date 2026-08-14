#!/usr/bin/env ruby
# frozen_string_literal: true

require "minitest/autorun"
require "fileutils"
require "open3"
require "tmpdir"
require "yaml"
require_relative "check_abi_staging_workflows"

class AbiStagingWorkflowCheckerTest < Minitest::Test
  ROOT = File.expand_path("..", __dir__)

  def load_workflow(name)
    YAML.safe_load(
      File.read(File.join(ROOT, ".github/workflows", name)),
      permitted_classes: [],
      aliases: false
    )
  end

  def setup
    @workflow = load_workflow("abi-staging-reconcile.yml")
    @candidate = load_workflow("abi-staging-candidate.yml")
    @reuse = load_workflow("abi-staging-reuse.yml")
    @verification = load_workflow("abi-staging-verification.yml")
    @maintenance = load_workflow("abi-staging-maintenance.yml")
    @history = load_workflow("abi-staging-abi-history.yml")
    @cleanup = load_workflow("abi-staging-candidate-cleanup.yml")
    @public_discovery = File.read(
      File.join(ROOT, "scripts/abi_staging/github_public.py")
    )
  end

  def copy(value = @workflow)
    Marshal.load(Marshal.dump(value))
  end

  def last_run_step(workflow, job_name)
    workflow.dig("jobs", job_name, "steps").reverse.find { |step| step.key?("run") }
  end

  def package_publication_environments(value, result = [])
    case value
    when Hash
      result << value if value.key?("HOMEBREW_GITHUB_PACKAGES_TOKEN") &&
                         value.key?("HOMEBREW_GITHUB_PACKAGES_USER")
      value.each_value { |child| package_publication_environments(child, result) }
    when Array
      value.each { |child| package_publication_environments(child, result) }
    end
    result
  end

  def assert_rejected(label)
    changed = copy
    yield changed
    error = assert_raises(AbiStagingWorkflowCheck::Violation, label) do
      AbiStagingWorkflowCheck.check(changed)
    end
    refute_empty error.message
  end

  def assert_rejected_matching(label, pattern)
    changed = copy
    yield changed
    error = assert_raises(AbiStagingWorkflowCheck::Violation, label) do
      AbiStagingWorkflowCheck.check(changed)
    end
    assert_match pattern, error.message
  end

  def assert_reusable_rejected(kind, label)
    original = kind == :candidate ? @candidate : @verification
    changed = copy(original)
    yield changed
    error = assert_raises(AbiStagingWorkflowCheck::Violation, label) do
      AbiStagingWorkflowCheck.check_reusable(changed, kind)
    end
    refute_empty error.message
  end

  def assert_maintenance_rejected(label)
    changed = copy(@maintenance)
    yield changed
    error = assert_raises(AbiStagingWorkflowCheck::Violation, label) do
      AbiStagingWorkflowCheck.check_maintenance(changed)
    end
    refute_empty error.message
  end

  def assert_reuse_rejected(label)
    changed = copy(@reuse)
    yield changed
    error = assert_raises(AbiStagingWorkflowCheck::Violation, label) do
      AbiStagingWorkflowCheck.check_reuse(changed)
    end
    refute_empty error.message
  end

  def assert_history_rejected(label)
    changed = copy(@history)
    yield changed
    error = assert_raises(AbiStagingWorkflowCheck::Violation, label) do
      AbiStagingWorkflowCheck.check_history(changed)
    end
    refute_empty error.message
  end

  def assert_cleanup_rejected(label)
    changed = copy(@cleanup)
    yield changed
    error = assert_raises(AbiStagingWorkflowCheck::Violation, label) do
      AbiStagingWorkflowCheck.check_cleanup(changed)
    end
    refute_empty error.message
  end

  def test_reviewed_workflows_pass
    AbiStagingWorkflowCheck.check(@workflow)
    AbiStagingWorkflowCheck.check_public_discovery(@public_discovery)
    AbiStagingWorkflowCheck.check_reusable(@candidate, :candidate)
    AbiStagingWorkflowCheck.check_reuse(@reuse)
    AbiStagingWorkflowCheck.check_reusable(@verification, :verification)
    AbiStagingWorkflowCheck.check_maintenance(@maintenance)
    AbiStagingWorkflowCheck.check_history(@history)
    AbiStagingWorkflowCheck.check_cleanup(@cleanup)
  end

  def test_public_discovery_rejects_broad_release_listing
    broad = @public_discovery.sub(
      "tags = self._request_release_tags()",
      <<~PYTHON.strip
        repository = self.policy.issuer_repository
        tags = self._pages(
            f"https://api.github.com/repos/{repository}/releases"
        )
      PYTHON
    )
    refute_equal @public_discovery, broad
    error = assert_raises(AbiStagingWorkflowCheck::Violation) do
      AbiStagingWorkflowCheck.check_public_discovery(broad)
    end
    assert_match(/broad Release inventory/, error.message)
  end

  def test_cleanup_planning_remains_read_only
    assert_cleanup_rejected("planner gained package writes") do |workflow|
      workflow.dig("jobs", "plan-cleanup", "permissions")["packages"] = "write"
    end
    assert_cleanup_rejected("writer reactivation") do |workflow|
      workflow.fetch("jobs")["delete-candidates"] = {
        "runs-on" => "ubuntu-latest",
        "permissions" => {"packages" => "write"},
        "steps" => []
      }
    end
  end

  def test_cleanup_checked_in_workflow_is_observe_only
    assert_equal ["plan-cleanup"], @cleanup.fetch("jobs").keys
    text = AbiStagingWorkflowCheck.flatten(@cleanup).join("\n")
    refute_includes text, "execute-live"
    refute_includes text, "packages: write"
  end

  def test_cleanup_checker_rejects_reactivation_of_writes_or_execution
    observe_only = copy(@cleanup)
    AbiStagingWorkflowCheck.check_cleanup(observe_only)

    assert_cleanup_rejected("observe-only planner gained package writes") do |workflow|
      workflow.dig("jobs", "plan-cleanup", "permissions")["packages"] = "write"
    end
    assert_cleanup_rejected("observe-only workflow gained execute-live") do |workflow|
      last_run_step(workflow, "plan-cleanup")["run"] +=
        "\npython3 -m scripts.abi_staging.cleanup execute-live\n"
    end
    assert_cleanup_rejected("observe-only workflow gained tombstone publication") do |workflow|
      last_run_step(workflow, "plan-cleanup")["run"] +=
        "\npython3 -m scripts.abi_staging.cleanup execute-live --immutable-tombstone\n"
    end
  end

  def test_cleanup_rejects_broad_delete_glob_candidate_execution_and_sleep
    assert_cleanup_rejected("broad package delete") do |workflow|
      last_run_step(workflow, "plan-cleanup")["run"] +=
        "\ngh api --method DELETE /orgs/example/packages/container/all\n"
    end
    assert_cleanup_rejected("glob target") do |workflow|
      event = workflow.key?("on") ? workflow["on"] : workflow[true]
      event.dig("workflow_dispatch", "inputs", "target_reference")["default"] = "*"
    end
    assert_cleanup_rejected("candidate execution") do |workflow|
      last_run_step(workflow, "plan-cleanup")["run"] +=
        "\nbash scripts/abi-staging-build-bottle.sh\n"
    end
    assert_cleanup_rejected("sleeping cleanup runner") do |workflow|
      last_run_step(workflow, "plan-cleanup")["run"] += "\nsleep 60\n"
    end
    assert_cleanup_rejected("personal token fallback") do |workflow|
      last_run_step(workflow, "plan-cleanup")["env"]["GH_TOKEN"] =
        "${{ secrets.PERSONAL_ACCESS_TOKEN }}"
    end
  end

  def test_discovery_uses_exact_candidate_data_and_immutable_policy_code
    steps = @workflow.dig("jobs", "discover-plan", "steps")
    candidate = steps.find { |step| step.dig("with", "path") == "kandelo-source" }
    authority = steps.find { |step| step.dig("with", "path") == "kandelo-authority" }
    requirements = steps.find { |step| step["id"] == "requirements" }
    refute_nil candidate
    refute_nil authority
    assert_equal "${{ steps.discover.outputs.kandelo_head }}", candidate.dig("with", "ref")
    assert_equal "${{ steps.discover.outputs.kandelo_policy_commit }}",
                 authority.dig("with", "ref")
    assert_equal "kandelo-authority", requirements["working-directory"]
    assert_includes requirements["run"],
                    "$GITHUB_WORKSPACE/kandelo-source/images/vfs/products/generated/catalog.json"
    assert_operator steps.index(candidate), :<, steps.index(authority)
    assert_operator steps.index(authority), :<, steps.index(requirements)
  end

  def test_requirements_change_classes_exclude_dev_shell_startup_output
    requirements = @workflow.dig("jobs", "discover-plan", "steps").find do |step|
      step["id"] == "requirements"
    end
    refute_nil requirements

    Dir.mktmpdir("abi-staging-requirements-") do |root|
      authority = File.join(root, "authority")
      runner = File.join(root, "runner")
      FileUtils.mkdir_p(File.join(authority, "scripts"))
      FileUtils.mkdir_p(File.join(runner, "abi-staging-coordination"))
      File.write(
        File.join(runner, "abi-staging-coordination", "request.json"),
        %({"requirements":{"change_classes":["abi","kernel","host"]}}\n)
      )
      dev_shell = File.join(authority, "scripts", "dev-shell.sh")
      File.write(dev_shell, <<~'SH')
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'kandelo dev shell startup output\n'
        case "$1" in
          bash|jq)
            exec "$@"
            ;;
          rustc)
            printf 'host: test-target\n'
            ;;
          cargo)
            classes=
            while (($#)); do
              if [[ $1 == --change-classes ]]; then
                classes=$2
                break
              fi
              shift
            done
            [[ -n $classes ]]
            jq -e '. == ["abi", "kernel", "host"]' "$classes" >/dev/null
            ;;
          *)
            exit 2
            ;;
        esac
      SH
      File.chmod(0o755, dev_shell)

      stdout, stderr, status = Open3.capture3(
        {"GITHUB_WORKSPACE" => root, "RUNNER_TEMP" => runner},
        "bash", "-euo", "pipefail", "-c", requirements.fetch("run"),
        chdir: authority
      )
      assert status.success?, "#{stdout}\n#{stderr}"
      classes = File.join(runner, "abi-staging-change-classes.json")
      assert_equal %( ["abi","kernel","host"]\n).delete_prefix(" "), File.read(classes)
    end
  end

  def test_top_level_calls_only_same_commit_bounded_reusable_workflows
    expected = {
      "candidate" => [
        "./.github/workflows/abi-staging-candidate.yml",
        "${{ fromJSON(needs.discover-plan.outputs.build-matrix) }}"
      ],
      "reuse" => [
        "./.github/workflows/abi-staging-reuse.yml",
        "${{ fromJSON(needs.discover-plan.outputs.reuse-matrix) }}"
      ],
      "verification" => [
        "./.github/workflows/abi-staging-verification.yml",
        "${{ fromJSON(needs.discover-plan.outputs.verify-matrix) }}"
      ]
    }
    expected.each do |name, (reusable, matrix)|
      job = @workflow.dig("jobs", name)
      assert_equal reusable, job["uses"]
      assert_equal matrix, job.dig("strategy", "matrix")
      assert_equal "${{ needs.discover-plan.outputs.coordination-artifact-id }}",
                   job.dig("with", "coordination-artifact-id")
      assert_equal "${{ needs.discover-plan.outputs.coordination-artifact-digest }}",
                   job.dig("with", "coordination-artifact-digest")
      assert_equal({
        "HOMEBREW_GITHUB_PACKAGES_TOKEN" =>
          "${{ secrets.HOMEBREW_GITHUB_PACKAGES_TOKEN }}"
      }, job["secrets"])
    end
  end

  def test_only_protected_publishers_receive_the_dedicated_package_token
    event = @candidate.key?("on") ? @candidate.fetch("on") : @candidate.fetch(true)
    assert_equal({"required" => true},
                 event.dig("workflow_call", "secrets",
                           "HOMEBREW_GITHUB_PACKAGES_TOKEN"))

    build_text = AbiStagingWorkflowCheck.flatten(
      @candidate.dig("jobs", "build")
    ).join("\n")
    refute_includes build_text, "secrets.HOMEBREW_GITHUB_PACKAGES_TOKEN"

    publisher = last_run_step(@candidate, "publish")
    assert_equal "${{ secrets.HOMEBREW_GITHUB_PACKAGES_TOKEN }}",
                 publisher.dig("env", "HOMEBREW_GITHUB_PACKAGES_TOKEN")
    assert_equal "${{ vars.HOMEBREW_GITHUB_PACKAGES_USER }}",
                 publisher.dig("env", "HOMEBREW_GITHUB_PACKAGES_USER")

    assert_reusable_rejected(:candidate, "publisher restored the Actions token") do |workflow|
      last_run_step(workflow, "publish").fetch("env")[
        "HOMEBREW_GITHUB_PACKAGES_TOKEN"
      ] = "${{ github.token }}"
    end
    assert_reusable_rejected(:candidate, "producer received the package token") do |workflow|
      last_run_step(workflow, "build").fetch("env")[
        "HOMEBREW_GITHUB_PACKAGES_TOKEN"
      ] = "${{ secrets.HOMEBREW_GITHUB_PACKAGES_TOKEN }}"
    end
  end

  def test_all_protected_publication_lanes_use_the_dedicated_package_credential
    workflows = [@workflow, @candidate, @reuse, @verification, @maintenance, @history]
    environments = workflows.flat_map do |workflow|
      package_publication_environments(workflow)
    end
    refute_empty environments
    environments.each do |environment|
      assert_equal "${{ secrets.HOMEBREW_GITHUB_PACKAGES_TOKEN }}",
                   environment.fetch("HOMEBREW_GITHUB_PACKAGES_TOKEN")
      assert_equal "${{ vars.HOMEBREW_GITHUB_PACKAGES_USER }}",
                   environment.fetch("HOMEBREW_GITHUB_PACKAGES_USER")
    end
  end

  def test_product_jobs_split_uncredentialed_execution_from_protected_writes
    expected = {
      "prepare-runtime" => {"contents" => "read"},
      "plan-products" => {"contents" => "read"},
      "compose-product" => {"contents" => "read"},
      "publish-product-candidate" => {
        "actions" => "read", "contents" => "read", "packages" => "write"
      },
      "node-product-evidence" => {"contents" => "read"},
      "browser-product-evidence" => {"contents" => "read"},
      "publish-product-evidence" => {
        "actions" => "read", "contents" => "read", "packages" => "write"
      }
    }
    expected.each do |name, permissions|
      job = @workflow.dig("jobs", name)
      refute_nil job, "missing #{name}"
      assert_equal permissions, job["permissions"]
      refute job.key?("secrets")
    end
    %w[prepare-runtime compose-product node-product-evidence browser-product-evidence].each do |name|
      assert_equal 180, @workflow.dig("jobs", name, "timeout-minutes")
    end
    assert_equal 30, @workflow.dig("jobs", "plan-products", "timeout-minutes")
  end

  def test_promotion_jobs_split_git_and_package_authority
    expected = {
      "plan-promotion" => {"contents" => "read"},
      "publish-canonical" => {
        "actions" => "read", "contents" => "read", "packages" => "write"
      },
      "update-tap-metadata" => {
        "actions" => "read", "contents" => "write"
      },
      "publish-admission" => {
        "actions" => "read", "contents" => "read", "packages" => "write"
      }
    }
    expected.each do |name, permissions|
      job = @workflow.dig("jobs", name)
      refute_nil job, "missing #{name}"
      assert_equal permissions, job["permissions"]
      refute job.key?("secrets")
    end
    %w[publish-canonical update-tap-metadata publish-admission].each do |name|
      strategy = @workflow.dig("jobs", name, "strategy")
      assert_equal false, strategy["fail-fast"]
      assert_operator strategy["max-parallel"], :<=, 16
    end
    assert_equal %w[discover-plan candidate verification reuse],
                 @workflow.dig("jobs", "plan-promotion", "needs")
    planner_tap = @workflow.dig("jobs", "plan-promotion", "steps").find do |step|
      step.dig("with", "path") == "tap-authority"
    end
    assert_equal 0, planner_tap.dig("with", "fetch-depth")
  end

  def test_admission_reconstructs_the_landed_metadata_from_public_main
    job = @workflow.dig("jobs", "publish-admission")
    metadata = job.fetch("steps").find do |step|
      step.dig("with", "path") == "tap-metadata"
    end
    refute_nil metadata
    assert_equal "main", metadata.dig("with", "ref")
    assert_equal 0, metadata.dig("with", "fetch-depth")
    assert_equal false, metadata.dig("with", "persist-credentials")
    assert_includes last_run_step(@workflow, "publish-admission").fetch("run"),
                    '--metadata-root "$GITHUB_WORKSPACE/tap-metadata"'
  end

  def test_promotion_workflow_mutations_are_rejected
    assert_rejected("promotion enabled during candidate canary") do |workflow|
      workflow.dig("jobs", "plan-promotion")["if"] = "success()"
    end
    assert_rejected("open PR promotion") do |workflow|
      workflow.dig("jobs", "plan-promotion")["if"] =
        "always() && needs.discover-plan.outputs.selected == 'true'"
    end
    assert_rejected("main commit scan trigger") do |workflow|
      event = workflow.key?("on") ? workflow["on"] : workflow[true]
      event["push"] = {"branches" => ["main"]}
    end
    assert_rejected("history Boolean") do |workflow|
      step = last_run_step(workflow, "plan-promotion")
      step["run"] = step["run"].sub("--require-history-record", "--history-ready")
    end
    assert_rejected("promotion before protected history") do |workflow|
      workflow.dig("jobs", "publish-canonical", "needs").delete("plan-promotion")
    end
    assert_rejected("shallow promotion history") do |workflow|
      checkout = workflow.dig("jobs", "plan-promotion", "steps").find do |step|
        step.dig("with", "path") == "tap-authority"
      end
      checkout["with"]["fetch-depth"] = 1
    end
    assert_rejected("changed layer") do |workflow|
      step = last_run_step(workflow, "publish-canonical")
      step["run"] = step["run"].sub("--require-unchanged-layer", "")
    end
    %w[publish-canonical update-tap-metadata publish-admission].each do |job|
      assert_rejected("#{job} skips the protected history barrier") do |workflow|
        step = last_run_step(workflow, job)
        step["run"] = step["run"].sub("--require-history-barrier", "")
      end
    end
    assert_rejected("combined package and Git writer") do |workflow|
      workflow.dig("jobs", "publish-canonical", "permissions")["contents"] = "write"
    end
    assert_rejected("candidate execution in metadata writer") do |workflow|
      last_run_step(workflow, "update-tap-metadata")["run"] +=
        "\nbash scripts/abi-staging-build-bottle.sh\n"
    end
    assert_rejected("unbounded promotion matrix") do |workflow|
      workflow.dig("jobs", "publish-canonical").delete("strategy")
    end
    assert_rejected("all Formula completion gate") do |workflow|
      workflow.dig("jobs", "publish-admission")["if"] =
        "needs.publish-canonical.result == 'success' && needs.update-tap-metadata.result == 'success'"
    end
    assert_rejected("metadata force push") do |workflow|
      last_run_step(workflow, "update-tap-metadata")["run"] +=
        "\ngit push --force origin HEAD:main\n"
    end
    assert_rejected("admission before metadata readback") do |workflow|
      workflow.dig("jobs", "publish-admission", "needs").delete("update-tap-metadata")
    end
    assert_rejected("background failure cancels sibling") do |workflow|
      workflow.dig("jobs", "publish-canonical", "strategy")["fail-fast"] = true
    end
  end

  def test_workflow_rejects_automattic_ghcr_targets
    text = AbiStagingWorkflowCheck.flatten(@workflow).join("\n")
    refute_match %r{ghcr\.io/Automattic/}i, text
    assert_rejected_matching("Automattic GHCR target", /Automattic GHCR/) do |workflow|
      workflow["name"] += " ghcr.io/Automattic/kandelo"
    end
    %i[candidate verification].each do |kind|
      assert_reusable_rejected(kind, "#{kind} Automattic GHCR target") do |workflow|
        workflow["name"] += " ghcr.io/Automattic/kandelo"
      end
    end
    assert_reuse_rejected("reuse Automattic GHCR target") do |workflow|
      workflow["name"] += " ghcr.io/Automattic/kandelo"
    end
    assert_maintenance_rejected("maintenance Automattic GHCR target") do |workflow|
      workflow["name"] += " ghcr.io/Automattic/kandelo"
    end
    assert_history_rejected("history Automattic GHCR target") do |workflow|
      workflow["name"] += " ghcr.io/Automattic/kandelo"
    end
    assert_cleanup_rejected("cleanup Automattic GHCR target") do |workflow|
      workflow["name"] += " ghcr.io/Automattic/kandelo"
    end
  end

  def test_product_planner_mutations_are_rejected_at_the_planning_boundary
    assert_rejected_matching(
      "planner skips a Formula lane", /product planner must wait for all Formula public facts/
    ) do |workflow|
      workflow.dig("jobs", "plan-products", "needs").delete("reuse")
    end
    assert_rejected_matching(
      "planner gains write authority", /product planner must remain contents-read only/
    ) do |workflow|
      workflow.dig("jobs", "plan-products", "permissions")["packages"] = "write"
    end
    assert_rejected_matching(
      "planner omits protected projection", /product planner does not derive the protected wave/
    ) do |workflow|
      step = last_run_step(workflow, "plan-products")
      step["run"] = step["run"].sub("plan-workflow-products", "prepare-workflow")
    end
    assert_rejected_matching(
      "planner executes candidate code", /product planner executes candidate data/
    ) do |workflow|
      last_run_step(workflow, "plan-products")["run"] <<
        "\nbash kandelo-source/images/vfs/scripts/build-node-vfs-image.sh\n"
    end
    assert_rejected_matching(
      "composition bypasses the wave", /product matrices must come from the protected wave/
    ) do |workflow|
      workflow.dig("jobs", "compose-product", "strategy")["matrix"] =
        "${{ fromJSON(needs.discover-plan.outputs.product-matrix) }}"
    end
    assert_rejected_matching(
      "evidence bypasses the wave", /node-product-evidence must gate on the protected product wave/
    ) do |workflow|
      workflow.dig("jobs", "node-product-evidence", "needs").delete("plan-products")
    end
  end

  def test_runtime_binds_the_protected_request_policy_identity
    steps = @workflow.dig("jobs", "prepare-runtime", "steps")
    tap = steps.find do |step|
      step.dig("with", "path") == "tap-authority"
    end
    export = steps.find do |step|
      step["name"] == "Export exact protected runtime identity"
    end
    build = steps.find do |step|
      step["name"] == "Build one exact uncredentialed runtime"
    end

    refute_nil tap
    assert_equal "${{ needs.discover-plan.outputs.tap-commit }}",
                 tap.dig("with", "ref")
    assert_equal false, tap.dig("with", "persist-credentials")
    refute_nil export
    refute_nil build
    assert_operator steps.index(export), :<, steps.index(build)
    assert_equal "kandelo-authority", export["working-directory"]
    assert_includes export.fetch("run"), "export-runtime-realm"
    assert_includes export.fetch("run"), '--github-env "$GITHUB_ENV"'
    source = build.fetch("run")
    assert_includes source, '"$KANDELO_ABI_STAGING_BUILD_POLICY_SHA256"'
    assert_includes source, '"$KANDELO_ABI_STAGING_SNAPSHOT_SHA256"'
    assert_includes source, '"$KANDELO_ABI_STAGING_SOURCE_TREE"'
    assert_includes source, '"$KANDELO_ABI_STAGING_TARGET_ABI"'
    refute_includes source, "source_tree=\"$("
    refute_includes source, "target_abi=\"$("
    refute_includes source, "snapshot=\"$("
    refute_includes source, "build_policy=\"$("
    refute_includes source, ".requirements.digest"

    assert_rejected_matching(
      "runtime omits protected tap checkout",
      /runtime lacks its exact protected tap checkout/
    ) do |workflow|
      workflow.dig("jobs", "prepare-runtime", "steps").reject! do |step|
        step.dig("with", "path") == "tap-authority"
      end
    end
    assert_rejected_matching(
      "runtime delays protected tap checkout",
      /runtime protected tap checkout occurs after identity export/
    ) do |workflow|
      runtime_steps = workflow.dig("jobs", "prepare-runtime", "steps")
      protected_tap = runtime_steps.delete_at(runtime_steps.index do |step|
        step.dig("with", "path") == "tap-authority"
      end)
      build_index = runtime_steps.index do |step|
        step["name"] == "Build one exact uncredentialed runtime"
      end
      runtime_steps.insert(build_index + 1, protected_tap)
    end
    assert_rejected_matching(
      "runtime overwrites protected tap checkout",
      /runtime lacks its exact protected tap checkout/
    ) do |workflow|
      runtime_steps = workflow.dig("jobs", "prepare-runtime", "steps")
      export_index = runtime_steps.index do |step|
        step["name"] == "Export exact protected runtime identity"
      end
      runtime_steps.insert(export_index, {
        "name" => "Overwrite tap authority with candidate code",
        "uses" => "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "with" => {
          "repository" => "${{ needs.discover-plan.outputs.kandelo-repository }}",
          "ref" => "${{ needs.discover-plan.outputs.kandelo-head }}",
          "fetch-depth" => 1,
          "persist-credentials" => false,
          "path" => "tap-authority"
        }
      })
    end
    assert_rejected_matching(
      "runtime omits protected identity export",
      /runtime identity is not exported before the exact build/
    ) do |workflow|
      workflow.dig("jobs", "prepare-runtime", "steps").reject! do |step|
        step["name"] == "Export exact protected runtime identity"
      end
    end
  end

  def test_package_publishers_install_one_pinned_oras_before_writing
    expected = "oras-project/setup-oras@1d808f7d7f6995cc68b7bf507bfe5c5446e1dc9d"
    publishers = [
      [:candidate, @candidate, "publish"],
      [:verification, @verification, "publish"],
      [:reuse, @reuse, "publish"],
      [:reconcile, @workflow, "publish-product-candidate"],
      [:reconcile, @workflow, "publish-product-evidence"],
      [:reconcile, @workflow, "publish-canonical"],
      [:reconcile, @workflow, "publish-admission"],
      [:maintenance, @maintenance, "maintain"],
      [:history, @history, "verify-and-publish-history"]
    ]

    publishers.each do |kind, workflow, job_name|
      steps = workflow.dig("jobs", job_name, "steps")
      setup = steps.find { |step| step["uses"] == expected }
      command = last_run_step(workflow, job_name)
      refute_nil setup, "missing pinned ORAS in #{kind}:#{job_name}"
      assert_equal({"version" => "1.3.3"}, setup["with"])
      assert_operator steps.index(setup), :<, steps.index(command)
    end

    assert_reusable_rejected(:candidate, "publisher omits ORAS") do |workflow|
      workflow.dig("jobs", "publish", "steps").reject! do |step|
        step["uses"] == expected
      end
    end
    assert_reusable_rejected(:candidate, "publisher overlays ORAS") do |workflow|
      steps = workflow.dig("jobs", "publish", "steps")
      command_index = steps.index { |step| step.key?("run") }
      steps.insert(command_index, {
        "name" => "Set up alternate ORAS",
        "uses" => "oras-project/setup-oras@38de303aac69abb66f3e6255b7198bff35f323e3",
        "with" => {"version" => "1.3.1"}
      })
    end
    assert_rejected_matching(
      "product publisher omits ORAS", /publisher lacks one pinned ORAS/
    ) do |workflow|
      workflow.dig("jobs", "publish-product-candidate", "steps").reject! do |step|
        step["uses"] == expected
      end
    end
    assert_maintenance_rejected("maintenance publisher omits ORAS") do |workflow|
      workflow.dig("jobs", "maintain", "steps").reject! do |step|
        step["uses"] == expected
      end
    end
    assert_history_rejected("history publisher omits ORAS") do |workflow|
      workflow.dig("jobs", "verify-and-publish-history", "steps").reject! do |step|
        step["uses"] == expected
      end
    end
  end

  def test_runtime_binds_one_fresh_isolated_package_cache
    steps = @workflow.dig("jobs", "prepare-runtime", "steps")
    isolate = steps.find do |step|
      step["name"] == "Isolate exact runtime package resolution"
    end
    build = steps.find do |step|
      step["name"] == "Build one exact uncredentialed runtime"
    end

    refute_nil isolate
    refute_nil build
    assert_operator steps.index(isolate), :<, steps.index(build)

    isolate_source = isolate.fetch("run")
    assert_includes isolate_source, "abi-staging-runtime-package-cache"
    assert_includes isolate_source, "test ! -e"
    assert_includes isolate_source, "mkdir -m 0700"
    assert_includes isolate_source, "WASM_POSIX_BINARY_CACHE_ROOT="
    assert_includes isolate_source, '>>"$GITHUB_ENV"'

    build_source = build.fetch("run")
    assert_includes build_source,
                    'WASM_POSIX_BINARY_CACHE_ROOT=$WASM_POSIX_BINARY_CACHE_ROOT'
    assert_includes build_source,
                    '--binary-cache-root "$WASM_POSIX_BINARY_CACHE_ROOT"'

    assert_rejected_matching(
      "runtime omits isolated cache", /runtime package cache is not sealed/
    ) do |workflow|
      workflow.dig("jobs", "prepare-runtime", "steps").reject! do |step|
        step["name"] == "Isolate exact runtime package resolution"
      end
    end
    assert_rejected_matching(
      "runtime omits exact cache handoff", /runtime package cache is not exact/
    ) do |workflow|
      step = workflow.dig("jobs", "prepare-runtime", "steps").find do |candidate|
        candidate["name"] == "Build one exact uncredentialed runtime"
      end
      step["run"] = step.fetch("run").sub(
        /\s+--binary-cache-root\s+"\$WASM_POSIX_BINARY_CACHE_ROOT"\s+\\/, ""
      )
    end
  end

  def test_product_workflow_mutations_are_rejected
    assert_rejected("composition writer") do |workflow|
      workflow.dig("jobs", "compose-product", "permissions")["packages"] = "write"
    end
    assert_rejected("publisher candidate execution") do |workflow|
      last_run_step(workflow, "publish-product-candidate")["run"] <<
        "\nbash kandelo-source/images/vfs/scripts/build-node-vfs-image.sh\n"
    end
    assert_rejected("missing report validation") do |workflow|
      step = last_run_step(workflow, "publish-product-candidate")
      step["run"] = step["run"].sub("--validate-builder-report", "")
    end
    assert_rejected("missing private product authority") do |workflow|
      step = last_run_step(workflow, "compose-product")
      step["run"] = step["run"].sub(
        /\s+--private-out\s+"\$RUNNER_TEMP\/product-private"\s+\\/, ""
      )
    end
    assert_rejected("publisher omits private product authority") do |workflow|
      step = last_run_step(workflow, "publish-product-candidate")
      step["run"] = step["run"].sub(
        /\s+--private-artifact-name\s+"[^"]+"\s+\\/, ""
      )
    end
    assert_rejected("missing browser evidence") do |workflow|
      workflow.fetch("jobs").delete("browser-product-evidence")
    end
    assert_rejected("skipped evidence accepted") do |workflow|
      step = last_run_step(workflow, "publish-product-evidence")
      step["run"] << "\nPRODUCER_CONCLUSION=skipped\n"
    end
    assert_rejected("evidence publisher loses parent product identity") do |workflow|
      step = last_run_step(workflow, "publish-product-evidence")
      step["run"] = step["run"].sub(
        /\s+--product-work-id\s+"\$PRODUCT_WORK_ID"\s+\\/, ""
      )
    end
    assert_rejected("global product transaction") do |workflow|
      workflow.dig("jobs", "compose-product").delete("strategy")
    end
    assert_rejected("sleeping product") do |workflow|
      last_run_step(workflow, "node-product-evidence")["run"] << "\nsleep 60\n"
    end
  end

  def test_candidate_code_never_runs_before_the_uncredentialed_boundary
    [[:candidate, @candidate, "build"],
     [:verification, @verification, "verify"]].each do |kind, workflow, producer_id|
      steps = workflow.dig("jobs", producer_id, "steps")
      setup = steps.find { |step| step["uses"]&.end_with?("/.github/actions/setup-nix") }
      assert_equal "./kandelo-authority/.github/actions/setup-nix", setup["uses"]
      refute steps.any? { |step| step["uses"]&.start_with?("./kandelo-source/") }
      source = last_run_step(workflow, producer_id).fetch("run")
      expected_directory = kind == :candidate ? "kandelo-source" : "kandelo-authority"
      assert_equal expected_directory,
                   last_run_step(workflow, producer_id)["working-directory"]
      assert_includes source, "scripts/dev-shell.sh env"
      assert_includes source,
                      'PYTHONPATH=$GITHUB_WORKSPACE/tap-authority'
      assert_includes source, "-u ACTIONS_RUNTIME_TOKEN"
      refute_includes source, "../kandelo-source/scripts/dev-shell.sh"
      assert_reusable_rejected(kind, "candidate local action") do |changed|
        changed.dig("jobs", producer_id, "steps").insert(
          0, {"uses" => "./kandelo-source/.github/actions/setup-nix"}
        )
      end
    end
  end

  def test_candidate_build_provisions_one_pinned_uncredentialed_homebrew_realm
    steps = @candidate.dig("jobs", "build", "steps")
    homebrew = steps.find do |step|
      step.dig("with", "path") == "homebrew-prefix/Homebrew"
    end
    export = steps.find do |step|
      step["name"] == "Export exact candidate build identity"
    end
    realm = steps.find do |step|
      step["name"] == "Prepare exact uncredentialed Homebrew realm"
    end
    execute = steps.find do |step|
      step.fetch("run", "").include?("execute-build-work")
    end

    refute_nil homebrew
    assert_equal "Homebrew/brew", homebrew.dig("with", "repository")
    assert_match(/\A[0-9a-f]{40}\z/, homebrew.dig("with", "ref"))
    assert_equal false, homebrew.dig("with", "persist-credentials")
    refute_nil export
    refute_nil realm
    refute_nil execute
    assert_equal "kandelo-source", execute["working-directory"]
    assert_operator steps.index(homebrew), :<, steps.index(realm)
    assert_operator steps.index(export), :<, steps.index(realm)
    assert_operator steps.index(realm), :<, steps.index(execute)

    export_source = export.fetch("run")
    assert_includes export_source, "export-build-realm"
    assert_includes export_source, '--work-id "$WORK_ID"'
    assert_includes export_source, '--github-env "$GITHUB_ENV"'
    assert_includes export_source, '"PYTHONDONTWRITEBYTECODE=1"'

    realm_source = realm.fetch("run")
    assert_includes realm_source, 'realm_root="$(mktemp -d /tmp/k.XXXXXX)"'
    assert_includes realm_source, "homebrew-prepare-host-prefix.sh"
    assert_includes realm_source, "scripts/build-musl.sh"
    assert_includes realm_source, "packages/registry/kernel/build-kernel.sh"
    assert_includes realm_source, "scripts/build-fork-instrument-tool.sh"
    assert_includes realm_source, "scripts/build-local-root-spill-tool.sh"
    assert_includes realm_source, "MemoryFileSystem.create"
    assert_includes realm_source, "host/wasm/rootfs.vfs"
    assert_includes realm_source, "async function main(): Promise<void>"
    refute_match(/^const memory = await import\(/, realm_source)
    refute_match(/^const image = await fs\.saveImage\(/, realm_source)
    refute_includes realm_source, "formula_test_packages"
    refute_includes realm_source, "--fetch-only resolve"
    assert_includes realm_source,
                    'playwright_browsers="$shared_temp/ms-playwright"'
    assert_includes realm_source, "PLAYWRIGHT_BROWSERS_PATH"
    assert_includes realm_source, "WASM_POSIX_BINARY_CACHE_ROOT"
    assert_includes realm_source,
                    'package_cache="$GITHUB_WORKSPACE/kandelo-source/.ci-test-binary-cache"'
    assert_includes realm_source,
                    'mkdir -m 0700 "$package_cache" "$package_cache/programs"'
    assert_includes realm_source, "formula_cache_paths=("
    assert_includes realm_source, '"$package_cache"'
    assert_includes realm_source, '"$package_cache/programs"'
    assert_includes realm_source,
                    '/usr/bin/sudo -n /usr/bin/chown root:root -- "${formula_cache_paths[@]}"'
    assert_includes realm_source,
                    '/usr/bin/sudo -n /usr/bin/chmod 0555 -- "${formula_cache_paths[@]}"'
    assert_includes realm_source,
                    'test "$(/usr/bin/stat -c \'%u:%g:%a\' "$directory")" = "0:0:555"'
    assert_includes realm_source, 'mkdir -m 0700 "$GITHUB_WORKSPACE/kandelo-source/binaries"'
    assert_includes realm_source, "build-deps program-index-selected"
    assert_includes realm_source, "rootfs \"$formula_test_index\""
    assert_includes realm_source, 'build_user="kandelo-homebrew-build"'
    assert_includes realm_source, 'recipe_user="kandelo-homebrew-recipe"'
    assert_includes realm_source, "/usr/sbin/useradd"
    assert_includes realm_source, 'shared_temp="$(mktemp -d /tmp/kandelo-homebrew.XXXXXX)"'
    assert_includes realm_source, 'chmod 1777 "$shared_temp"'
    assert_includes realm_source, 'echo "KANDELO_HOMEBREW_BUILD_USER=$build_user"'
    assert_includes realm_source, 'echo "KANDELO_HOMEBREW_RECIPE_USER=$recipe_user"'
    assert_includes realm_source, 'echo "KANDELO_HOMEBREW_SHARED_TEMP=$shared_temp"'
    refute_includes realm_source, '"$realm_root/package-cache"'
    assert_includes realm_source,
                    '/usr/bin/sudo -n /usr/bin/install -o root -g root -m 0555 --'
    assert_includes realm_source,
                    '/usr/bin/sudo -n /usr/bin/mv -f -- "$candidate_xtask_staged" "$candidate_xtask"'
    assert_includes realm_source,
                    'echo "WASM_POSIX_XTASK_BIN=$candidate_xtask"'
    assert_includes realm_source, "candidate_platform_tools=("
    assert_includes realm_source, '"tools/bin/wasm-fork-instrument"'
    assert_includes realm_source, '"tools/bin/wasm-local-root-spill"'
    assert_includes realm_source,
                    '/usr/bin/sudo -n /usr/bin/chown root:root --'
    assert_includes realm_source, '"$GITHUB_WORKSPACE/kandelo-source"'
    assert_includes realm_source, "env -u GITHUB_TOKEN"
    assert_includes realm_source, "-u ACTIONS_RUNTIME_TOKEN"
    assert_includes realm_source, "-u GITHUB_ENV"
    realm_cd = realm_source.index('cd "$GITHUB_WORKSPACE/kandelo-source"')
    realm_shell = realm_source.index("env -u GITHUB_TOKEN")
    refute_nil realm_cd
    refute_nil realm_shell
    assert_operator realm_cd, :<, realm_shell

    execute_source = execute.fetch("run")
    %w[
      HOMEBREW_BREW_FILE HOMEBREW_BREW_COMMIT HOMEBREW_CACHE HOMEBREW_TEMP
      KANDELO_HOMEBREW_RESOLVED_TAPS_FILE PLAYWRIGHT_BROWSERS_PATH
      WASM_POSIX_BINARY_CACHE_ROOT WASM_POSIX_XTASK_BIN
      KANDELO_HOMEBREW_BUILD_USER KANDELO_HOMEBREW_RECIPE_USER
      KANDELO_HOMEBREW_SHARED_TEMP KANDELO_HOMEBREW_SUDO_BIN
      KANDELO_HOMEBREW_SYSTEMD_RUN_BIN KANDELO_HOMEBREW_SYSTEMCTL_BIN
      KANDELO_HOMEBREW_GETENT_BIN KANDELO_HOMEBREW_PGREP_BIN
      KANDELO_HOMEBREW_PKILL_BIN
    ].each do |name|
      assert_includes execute_source, "#{name}=$#{name}"
    end
    assert_includes execute_source, '"PYTHONDONTWRITEBYTECODE=1"'
    assert_includes execute_source, '"PYTHONSAFEPATH=1"'
    assert_includes execute_source,
                    "python3 -P -m scripts.abi_staging.cli execute-build-work"
    assert_reusable_rejected(:candidate, "candidate Homebrew ref drift") do |workflow|
      checkout = workflow.dig("jobs", "build", "steps").find do |step|
        step.dig("with", "path") == "homebrew-prefix/Homebrew"
      end
      checkout.fetch("with")["ref"] = "0" * 40
    end
    assert_reusable_rejected(:candidate, "candidate Playwright cache drift") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      step["run"] = step.fetch("run").sub(
        'playwright_browsers="$shared_temp/ms-playwright"',
        'playwright_browsers="$realm_root/playwright"',
      )
    end
    assert_reusable_rejected(:candidate, "candidate realm retains token") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      step["run"] = step.fetch("run").sub("-u ACTIONS_RUNTIME_TOKEN", "")
    end
    assert_reusable_rejected(:candidate, "candidate Homebrew realm root is not socket-safe") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      step["run"] = step.fetch("run").sub(
        'realm_root="$(mktemp -d /tmp/k.XXXXXX)"',
        'realm_root="$RUNNER_TEMP/abi-staging-build-realm-$WORK_ID"'
      )
    end
    assert_reusable_rejected(:candidate, "candidate Formula cache leaves the portable runtime") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      step["run"] = step.fetch("run").sub(
        'package_cache="$GITHUB_WORKSPACE/kandelo-source/.ci-test-binary-cache"',
        'package_cache="$realm_root/package-cache"'
      )
    end
    assert_reusable_rejected(:candidate, "candidate Formula cache remains runner-private") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      step["run"] = step.fetch("run").sub(
        '/usr/bin/sudo -n /usr/bin/chmod 0555 -- "${formula_cache_paths[@]}"',
        ':'
      )
    end
    assert_reusable_rejected(:candidate, "candidate Formula checker retains Cargo hard link") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      step["run"] = step.fetch("run").sub(
        '"$candidate_xtask" "$candidate_xtask_staged"',
        '"$candidate_xtask"'
      )
    end
    assert_reusable_rejected(:candidate, "candidate tap recipe platform remains mutable") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      step["run"] = step.fetch("run").sub("candidate_platform_tools=(", "mutable_platform_tools=(")
    end
    assert_reusable_rejected(:candidate, "candidate omits isolated recipe identity") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      step["run"] = step.fetch("run").sub(
        'recipe_user="kandelo-homebrew-recipe"',
        'recipe_user=""'
      )
    end
    assert_reusable_rejected(:candidate, "candidate omits Formula test projection") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      step["run"] = step.fetch("run").sub(
        "build-deps program-index-selected",
        "build-deps program-index"
      )
    end
    assert_reusable_rejected(:candidate, "candidate shadows protected Python") do |workflow|
      step = workflow.dig("jobs", "build", "steps").find do |candidate|
        candidate.fetch("run", "").include?("execute-build-work")
      end
      step["run"] = step.fetch("run").sub("python3 -P -m", "python3 -m")
    end
  end

  def test_uncredentialed_executors_enter_the_immutable_kandelo_dev_shell
    lanes = [
      [:candidate, @candidate, "build", "execute-build-work"],
      [:verification, @verification, "verify", "execute-verification-work"],
      [:reconcile, @workflow, "compose-product", "execute-product-work"],
      [:reconcile, @workflow, "node-product-evidence",
       "execute-product-evidence-work"],
      [:reconcile, @workflow, "browser-product-evidence",
       "execute-product-evidence-work"]
    ]

    lanes.each do |kind, workflow, job_name, command|
      step = workflow.dig("jobs", job_name, "steps").find do |candidate|
        candidate.fetch("run", "").include?(command)
      end
      refute_nil step, "missing #{job_name} executor"
      expected_directory = kind == :candidate ? "kandelo-source" : "kandelo-authority"
      assert_equal expected_directory, step["working-directory"]
      source = step.fetch("run")
      assert_includes source, "scripts/dev-shell.sh env"
      assert_includes source,
                      'PYTHONPATH=$GITHUB_WORKSPACE/tap-authority'
      refute_includes source, "../kandelo-source/scripts/dev-shell.sh"

      next unless %i[candidate verification].include?(kind)

      assert_reusable_rejected(kind, "executor enters the tap as a flake") do |changed|
        changed_step = changed.dig("jobs", job_name, "steps").find do |candidate|
          candidate.fetch("run", "").include?(command)
        end
        changed_step["working-directory"] = "tap-authority"
      end
      assert_reusable_rejected(kind, "executor retains artifact credentials") do |changed|
        changed_step = changed.dig("jobs", job_name, "steps").find do |candidate|
          candidate.fetch("run", "").include?(command)
        end
        changed_step["run"] = changed_step.fetch("run").sub(
          /\s+-u ACTIONS_RUNTIME_TOKEN\s+\\/, ""
        )
      end
    end

    %w[compose-product node-product-evidence browser-product-evidence].each do |job_name|
      assert_rejected_matching(
        "#{job_name} enters the tap as a flake",
        /uncredentialed executor does not enter immutable Kandelo dev shell/
      ) do |workflow|
        step = workflow.dig("jobs", job_name, "steps").find do |candidate|
          candidate.fetch("run", "").include?("execute-")
        end
        step["working-directory"] = "tap-authority"
      end
    end
  end

  def test_reuse_writer_cannot_execute_candidate_or_accept_mutable_coordination
    assert_reuse_rejected("candidate execution") do |workflow|
      last_run_step(workflow, "publish")["run"] << "\nbash handoff/run.sh\n"
    end
    assert_reuse_rejected("missing digest") do |workflow|
      last_run_step(workflow, "publish")["run"] =
        last_run_step(workflow, "publish")["run"].gsub(
          '--coordination-artifact-digest "$COORDINATION_ARTIFACT_DIGEST"', ""
        )
    end
  end

  def test_candidate_jobs_cannot_gain_write_authority_or_secrets
    [[:candidate, "build"], [:verification, "verify"]].each do |kind, producer_id|
      assert_reusable_rejected(kind, "producer writer") do |workflow|
        workflow.dig("jobs", producer_id, "permissions")["packages"] = "write"
      end
      assert_reusable_rejected(kind, "producer secrets") do |workflow|
        workflow.dig("jobs", producer_id)["secrets"] = "inherit"
      end
    end
  end

  def test_direct_upload_outputs_bind_each_publisher_to_its_producer
    [[:candidate, "build"], [:verification, "verify"]].each do |kind, producer_id|
      workflow = kind == :candidate ? @candidate : @verification
      upload = workflow.dig("jobs", producer_id, "steps").find do |step|
        step["uses"]&.start_with?(AbiStagingWorkflowCheck::UPLOAD_ARTIFACT)
      end
      assert_equal "upload", upload["id"]
      assert_includes upload.dig("with", "name"), "${{ github.run_attempt }}"
      publisher = workflow.dig("jobs", "publish")
      assert_equal producer_id, publisher["needs"]
      env = last_run_step(workflow, "publish").fetch("env")
      assert_equal "${{ needs.#{producer_id}.outputs.artifact-id }}",
                   env["HANDOFF_ARTIFACT_ID"]
      assert_equal "${{ needs.#{producer_id}.outputs.artifact-digest }}",
                   env["HANDOFF_ARTIFACT_DIGEST"]
      assert_equal "${{ needs.#{producer_id}.result }}", env["PRODUCER_CONCLUSION"]

      assert_reusable_rejected(kind, "guessed artifact") do |changed|
        last_run_step(changed, "publish").fetch("env")["HANDOFF_ARTIFACT_ID"] = "1001"
      end
      assert_reusable_rejected(kind, "missing digest output") do |changed|
        changed.dig("jobs", producer_id, "outputs").delete("artifact-digest")
      end
      assert_reusable_rejected(kind, "cross-job publisher") do |changed|
        changed.dig("jobs", "publish")["needs"] = "discover"
      end
      assert_reusable_rejected(kind, "prior-attempt artifact name") do |changed|
        changed_upload = changed.dig("jobs", producer_id, "steps").find do |step|
          step["id"] == "upload"
        end
        changed_upload.fetch("with")["name"] = "artifact-${{ inputs.work-id }}"
      end
    end
  end

  def test_publishers_cannot_execute_candidate_handoffs_or_drop_readback_guards
    %i[candidate verification].each do |kind|
      assert_reusable_rejected(kind, "candidate execution") do |workflow|
        last_run_step(workflow, "publish")["run"] += "\nbash handoff/scripts/build.sh"
      end
      assert_reusable_rejected(kind, "missing digest guard") do |workflow|
        step = last_run_step(workflow, "publish")
        step["run"] = step.fetch("run").sub("--require-github-digest", "")
      end
      assert_reusable_rejected(kind, "missing anonymous readback") do |workflow|
        step = last_run_step(workflow, "publish")
        step["run"] = step.fetch("run").sub("--anonymous-readback", "")
      end
    end
  end

  def test_request_cannot_select_code_and_workflows_do_not_sleep
    assert_rejected("request-selected checkout") do |workflow|
      checkout = workflow.dig("jobs", "discover-plan", "steps").find do |step|
        step.dig("with", "path") == "tap-authority"
      end
      checkout.fetch("with")["ref"] = "${{ inputs.request_asset_url }}"
    end
    assert_rejected("sleeping coordinator") do |workflow|
      last_run_step(workflow, "discover-plan")["run"] += "\nsleep 60"
    end
    assert_reusable_rejected(:candidate, "sleeping retry") do |workflow|
      last_run_step(workflow, "build")["run"] += "\nsleep 60"
    end
  end

  def test_dispatch_pr_trigger_and_secret_references_are_rejected
    assert_rejected("pull request trigger") do |workflow|
      event = workflow.key?("on") ? workflow["on"] : workflow[true]
      event["pull_request"] = {}
    end
    assert_rejected("secret") do |workflow|
      workflow.dig("jobs", "candidate")["secrets"] = "inherit"
    end
  end

  def test_maintenance_rejects_pr_trigger_and_non_main_code
    assert_maintenance_rejected("pull request trigger") do |workflow|
      event = workflow.key?("on") ? workflow["on"] : workflow[true]
      event["pull_request"] = {}
    end
    assert_maintenance_rejected("non-main protected code") do |workflow|
      checkout = workflow.dig("jobs", "maintain", "steps").find do |step|
        step["uses"]&.start_with?(AbiStagingWorkflowCheck::CHECKOUT)
      end
      checkout.fetch("with")["ref"] = "${{ github.sha }}"
    end
  end

  def test_maintenance_rejects_candidate_execution_and_free_form_selection
    assert_maintenance_rejected("candidate execution") do |workflow|
      workflow.dig("jobs", "maintain", "steps").last["run"] +=
        "\nbash scripts/abi-staging-build-bottle.sh"
    end
    assert_maintenance_rejected("free-form Formula") do |workflow|
      event = workflow.key?("on") ? workflow["on"] : workflow[true]
      event.dig("workflow_dispatch", "inputs")["formula"] = {
        "required" => true, "type" => "string"
      }
    end
  end

  def test_maintenance_requires_permission_query_digest_and_immutability
    assert_maintenance_rejected("unverified permission") do |workflow|
      step = workflow.dig("jobs", "maintain", "steps").last
      step["run"] = step["run"].sub("--verify-actor-permission", "")
    end
    assert_maintenance_rejected("artifact without digest") do |workflow|
      event = workflow.key?("on") ? workflow["on"] : workflow[true]
      event.dig("workflow_dispatch", "inputs").delete("evidence_sha256")
    end
    assert_maintenance_rejected("mutable receipt") do |workflow|
      step = workflow.dig("jobs", "maintain", "steps").last
      step["run"] = step["run"].sub("--immutable", "--replace")
    end
  end

  def test_maintenance_checked_in_workflow_does_not_authorize_historical_repair
    event = @maintenance.key?("on") ? @maintenance["on"] : @maintenance[true]
    refute_includes event.dig("workflow_dispatch", "inputs", "command", "options"),
                    "historical-repair"
    assert_equal ["maintain"], @maintenance.fetch("jobs").keys
  end

  def test_maintenance_checker_rejects_plan_only_historical_repair_authority
    deferred = copy(@maintenance)
    event = deferred.key?("on") ? deferred["on"] : deferred[true]
    event.dig("workflow_dispatch", "inputs", "command", "options").delete(
      "historical-repair"
    )
    deferred.fetch("jobs").delete("authorize-historical-repair")
    deferred.dig("jobs", "maintain").delete("if")
    AbiStagingWorkflowCheck.check_maintenance(deferred)

    changed = copy(deferred)
    changed_event = changed.key?("on") ? changed["on"] : changed[true]
    changed_event.dig("workflow_dispatch", "inputs", "command", "options") <<
      "historical-repair"
    error = assert_raises(AbiStagingWorkflowCheck::Violation) do
      AbiStagingWorkflowCheck.check_maintenance(changed)
    end
    assert_match(/closed choice/, error.message)

    changed = copy(deferred)
    changed.fetch("jobs")["authorize-historical-repair"] = {
      "runs-on" => "ubuntu-latest",
      "permissions" => {"packages" => "write"},
      "steps" => []
    }
    error = assert_raises(AbiStagingWorkflowCheck::Violation) do
      AbiStagingWorkflowCheck.check_maintenance(changed)
    end
    assert_match(/job/, error.message)

    changed = copy(deferred)
    last_run_step(changed, "maintain")["run"] +=
      "\npython3 -m scripts.abi_staging.historical_maintenance authorize\n"
    error = assert_raises(AbiStagingWorkflowCheck::Violation) do
      AbiStagingWorkflowCheck.check_maintenance(changed)
    end
    assert_match(/historical/, error.message)
  end

  def test_history_workflow_preserves_protection_and_ref_ordering
    assert_history_rejected("create before protection preflight") do |workflow|
      steps = workflow.dig("jobs", "plan-and-verify-policy", "steps")
      command = steps.find { |step| step.key?("run") }
      command["run"] = command["run"].sub("plan-history", "create-history-ref")
    end
    assert_history_rejected("administration permission") do |workflow|
      workflow.dig("jobs", "create-history-ref", "permissions")["administration"] = "write"
    end
    assert_history_rejected("force update") do |workflow|
      step = last_run_step(workflow, "create-history-ref")
      step["run"] += "\ngit push --force origin HEAD:refs/heads/abi/7\n"
    end
    assert_history_rejected("candidate selected ref") do |workflow|
      checkout = workflow.dig("jobs", "create-history-ref", "steps").find do |step|
        step["uses"]&.start_with?(AbiStagingWorkflowCheck::CHECKOUT)
      end
      checkout.fetch("with")["ref"] = "${{ inputs.candidate_sha }}"
    end
    assert_history_rejected("promotion in history workflow") do |workflow|
      last_run_step(workflow, "verify-and-publish-history")["run"] +=
        "\npython3 -m scripts.abi_staging.cli promote-formula\n"
    end
    assert_history_rejected("candidate execution in writer") do |workflow|
      last_run_step(workflow, "create-history-ref")["run"] +=
        "\nbash Formula/build.sh\n"
    end
    assert_history_rejected("missing public verification") do |workflow|
      step = last_run_step(workflow, "verify-and-publish-history")
      step["run"] = step["run"].sub("--anonymous-readback", "")
    end
    assert_history_rejected("swallowed failure") do |workflow|
      workflow.dig("jobs", "create-history-ref", "steps").last["continue-on-error"] = true
    end
    assert_history_rejected("undeclared Python") do |workflow|
      setup = workflow.dig("jobs", "create-history-ref", "steps").find do |step|
        step["uses"]&.start_with?(AbiStagingWorkflowCheck::SETUP_PYTHON)
      end
      setup.fetch("with")["python-version"] = "3.12"
    end
  end
end
