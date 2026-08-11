#!/usr/bin/env ruby
# frozen_string_literal: true

require "minitest/autorun"
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
      refute job.key?("secrets")
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
    source = last_run_step(@workflow, "prepare-runtime").fetch("run")
    assert_includes source, ".issuance.policy_sha256"
    refute_includes source, ".requirements.digest"
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
      assert_includes source, "../kandelo-authority/scripts/dev-shell.sh"
      refute_includes source, "../kandelo-source/scripts/dev-shell.sh"
      assert_reusable_rejected(kind, "candidate local action") do |changed|
        changed.dig("jobs", producer_id, "steps").insert(
          0, {"uses" => "./kandelo-source/.github/actions/setup-nix"}
        )
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
