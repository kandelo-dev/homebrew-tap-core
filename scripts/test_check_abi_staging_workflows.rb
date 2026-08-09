#!/usr/bin/env ruby
# frozen_string_literal: true

require "minitest/autorun"
require "yaml"
require_relative "check_abi_staging_workflows"

class AbiStagingWorkflowCheckerTest < Minitest::Test
  ROOT = File.expand_path("..", __dir__)
  RECONCILE_WORKFLOW = File.join(ROOT, ".github/workflows/abi-staging-reconcile.yml")
  MAINTENANCE_WORKFLOW = File.join(ROOT, ".github/workflows/abi-staging-maintenance.yml")

  def setup
    @workflow = YAML.safe_load(File.read(RECONCILE_WORKFLOW), permitted_classes: [], aliases: false)
    @maintenance = YAML.safe_load(File.read(MAINTENANCE_WORKFLOW), permitted_classes: [], aliases: false)
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

  def assert_maintenance_rejected(label)
    changed = copy(@maintenance)
    yield changed
    error = assert_raises(AbiStagingWorkflowCheck::Violation, label) do
      AbiStagingWorkflowCheck.check_maintenance(changed)
    end
    refute_empty error.message
  end

  def test_reviewed_workflow_passes
    AbiStagingWorkflowCheck.check(@workflow)
    AbiStagingWorkflowCheck.check_maintenance(@maintenance)
  end

  def test_discovery_checks_out_the_exact_head_before_deriving_requirements
    steps = @workflow.dig("jobs", "discover-plan", "steps")
    kandelo_checkout = steps.find do |step|
      step["uses"]&.start_with?(AbiStagingWorkflowCheck::CHECKOUT) &&
        step.dig("with", "path") == "kandelo-source"
    end
    refute_nil kandelo_checkout
    assert_equal "${{ steps.discover.outputs.kandelo_repository }}",
                 kandelo_checkout.dig("with", "repository")
    assert_equal "${{ steps.discover.outputs.kandelo_head }}",
                 kandelo_checkout.dig("with", "ref")
    discover_index = steps.index { |step| step["id"] == "discover" }
    requirements_index = steps.index { |step| step["id"] == "requirements" }
    coordinate_index = steps.index { |step| step["id"] == "coordinate" }
    refute_nil discover_index
    refute_nil requirements_index
    refute_nil coordinate_index
    assert_operator discover_index, :<, steps.index(kandelo_checkout)
    assert_operator steps.index(kandelo_checkout), :<, requirements_index
    assert_operator requirements_index, :<, coordinate_index
  end

  def test_candidate_code_never_runs_before_the_uncredentialed_boundary
    %w[discover-plan build-candidate verify-candidate].each do |job_name|
      steps = @workflow.dig("jobs", job_name, "steps")
      authority = steps.find do |step|
        step["uses"]&.start_with?(AbiStagingWorkflowCheck::CHECKOUT) &&
          step.dig("with", "path") == "kandelo-authority"
      end
      refute_nil authority, "#{job_name} lacks immutable Kandelo policy checkout"
      output = job_name == "discover-plan" ?
        "steps.discover.outputs.kandelo_policy_commit" :
        "needs.discover-plan.outputs.kandelo-policy-commit"
      assert_equal "${{ #{output} }}",
                   authority.dig("with", "ref")
      setup = steps.find { |step| step["uses"]&.end_with?("/.github/actions/setup-nix") }
      refute_nil setup
      assert_equal "./kandelo-authority/.github/actions/setup-nix", setup["uses"]
      refute steps.any? { |step| step["uses"]&.start_with?("./kandelo-source/") }
    end

    requirements = @workflow.dig("jobs", "discover-plan", "steps").find do |step|
      step["id"] == "requirements"
    end
    assert_equal "kandelo-authority", requirements["working-directory"]
    assert_includes requirements["run"], "$GITHUB_WORKSPACE/kandelo-source/images/vfs/products/generated/catalog.json"

    %w[build-candidate verify-candidate].each do |job_name|
      command = last_run_step(@workflow, job_name).fetch("run")
      assert_includes command, "../kandelo-authority/scripts/dev-shell.sh"
      refute_includes command, "../kandelo-source/scripts/dev-shell.sh"
    end
  end

  def test_write_permission_is_rejected
    assert_rejected("write permission") do |workflow|
      workflow.dig("jobs", "build-candidate", "permissions")["contents"] = "write"
    end
    assert_rejected("verification write permission") do |workflow|
      workflow.dig("jobs", "verify-candidate", "permissions")["packages"] = "write"
    end
  end

  def test_secret_and_request_execution_are_rejected
    assert_rejected("secret") do |workflow|
      workflow.dig("jobs", "build-candidate")["env"] = {"TOKEN" => "${{ secrets.TOKEN }}"}
    end
    assert_rejected("request execution") do |workflow|
      last_run_step(workflow, "discover-plan")["run"] = 'bash "$REQUEST_ASSET_URL"'
    end
  end

  def test_request_cannot_select_checkout_or_coordinator
    assert_rejected("checkout ref") do |workflow|
      checkout = workflow.dig("jobs", "discover-plan", "steps").find { |step| step["uses"]&.start_with?(AbiStagingWorkflowCheck::CHECKOUT) }
      checkout.fetch("with")["ref"] = "${{ inputs.request_asset_url }}"
    end
    assert_rejected("coordinator") do |workflow|
      last_run_step(workflow, "discover-plan")["run"] =
        "python3 request-supplied-reconciler.py"
    end
  end

  def test_dispatch_and_pr_triggers_are_rejected
    assert_rejected("build dispatch") do |workflow|
      last_run_step(workflow, "discover-plan")["run"] += "\ngh workflow run build.yml"
    end
    assert_rejected("repository dispatch") do |workflow|
      triggers = workflow.key?("on") ? workflow["on"] : workflow[true]
      triggers["repository_dispatch"] = {}
    end
  end

  def test_candidate_and_verifier_cannot_gain_writer_authority_or_secrets
    %w[build-candidate verify-candidate].each do |job_name|
      assert_rejected("#{job_name} package writer") do |workflow|
        workflow.dig("jobs", job_name, "permissions")["packages"] = "write"
      end
      assert_rejected("#{job_name} secret inheritance") do |workflow|
        workflow.dig("jobs", job_name)["secrets"] = "inherit"
      end
    end
  end

  def test_publishers_cannot_execute_candidate_handoffs_or_combine_roles
    %w[publish-candidate publish-receipt].each do |job_name|
      assert_rejected("#{job_name} candidate execution") do |workflow|
        last_run_step(workflow, job_name)["run"] +=
          "\nbash handoff/scripts/build.sh"
      end
    end
    assert_rejected("combined build and publish") do |workflow|
      workflow.dig("jobs", "build-candidate", "permissions")["packages"] = "write"
      last_run_step(workflow, "build-candidate")["run"] +=
        "\npython3 -m scripts.abi_staging.cli publish-candidate"
    end
  end

  def test_matrices_and_timeouts_are_bounded()
    %w[build-candidate publish-candidate verify-candidate publish-receipt].each do |job_name|
      assert_rejected("#{job_name} unbounded matrix") do |workflow|
        workflow.dig("jobs", job_name, "strategy").delete("max-parallel")
      end
    end
    %w[build-candidate verify-candidate].each do |job_name|
      assert_rejected("#{job_name} timeout") do |workflow|
        workflow.dig("jobs", job_name)["timeout-minutes"] = 361
      end
    end
  end

  def test_artifact_identity_and_anonymous_readback_are_required()
    assert_rejected("missing build artifact digest bridge") do |workflow|
      step = last_run_step(workflow, "publish-candidate")
      source = step.fetch("run")
      step["run"] =
        source.sub("--require-github-digest", "")
    end
    assert_rejected("missing receipt artifact digest bridge") do |workflow|
      step = last_run_step(workflow, "publish-receipt")
      source = step.fetch("run")
      step["run"] =
        source.sub("--require-github-digest", "")
    end
    assert_rejected("missing anonymous candidate readback") do |workflow|
      step = last_run_step(workflow, "publish-candidate")
      source = step.fetch("run")
      step["run"] =
        source.sub("--anonymous-readback", "")
    end
    assert_rejected("mutable candidate tag") do |workflow|
      last_run_step(workflow, "publish-candidate")["run"] +=
        "\noras push ghcr.io/example/candidate:latest handoff.tar"
    end
  end

  def test_each_publisher_retains_its_exact_result_locator()
    %w[publish-candidate publish-receipt].each do |job_name|
      assert_rejected("#{job_name} missing result locator") do |workflow|
        workflow.dig("jobs", job_name, "steps").reject! do |step|
          step["uses"]&.start_with?(AbiStagingWorkflowCheck::UPLOAD_ARTIFACT)
        end
      end
    end
  end

  def test_candidate_execution_binds_run_facts_and_preserves_failed_handoffs
    assert_rejected("missing build run identity") do |workflow|
      step = last_run_step(workflow, "build-candidate")
      step["run"] = step.fetch("run").sub('--run-id "$GITHUB_RUN_ID"', "")
    end
    assert_rejected("nested coordination download") do |workflow|
      download = workflow.dig("jobs", "build-candidate", "steps").find do |step|
        step["uses"]&.start_with?(AbiStagingWorkflowCheck::DOWNLOAD_ARTIFACT)
      end
      download.fetch("with").delete("merge-multiple")
    end
    assert_rejected("lost failed build handoff") do |workflow|
      upload = workflow.dig("jobs", "build-candidate", "steps").find do |step|
        step["uses"]&.start_with?(AbiStagingWorkflowCheck::UPLOAD_ARTIFACT)
      end
      upload.delete("if")
    end
  end

  def test_background_failure_does_not_gate_required_work_and_no_job_sleeps()
    assert_rejected("background gate") do |workflow|
      workflow.dig("jobs", "verify-candidate")["needs"] << "background-complete"
    end
    assert_rejected("sleeping retry") do |workflow|
      last_run_step(workflow, "discover-plan")["run"] += "\nsleep 60"
    end
  end

  def test_maintenance_rejects_pr_trigger_and_non_main_code
    assert_maintenance_rejected("pull request trigger") do |workflow|
      triggers = workflow.key?("on") ? workflow["on"] : workflow[true]
      triggers["pull_request"] = {}
    end
    assert_maintenance_rejected("non-main protected code") do |workflow|
      checkout = workflow.dig("jobs", "maintain", "steps").find do |step|
        step["uses"]&.start_with?(AbiStagingWorkflowCheck::CHECKOUT)
      end
      checkout.fetch("with")["ref"] = "${{ github.sha }}"
    end
  end

  def test_maintenance_rejects_candidate_execution_with_write_authority
    assert_maintenance_rejected("candidate execution") do |workflow|
      workflow.dig("jobs", "maintain", "steps").last["run"] +=
        "\nbash scripts/abi-staging-build-bottle.sh"
    end
  end

  def test_maintenance_rejects_free_form_subject_or_guard_selection
    assert_maintenance_rejected("free-form Formula") do |workflow|
      triggers = workflow.key?("on") ? workflow["on"] : workflow[true]
      triggers.dig("workflow_dispatch", "inputs")["formula"] = {
        "required" => true, "type" => "string"
      }
    end
    assert_maintenance_rejected("arbitrary guard") do |workflow|
      triggers = workflow.key?("on") ? workflow["on"] : workflow[true]
      triggers.dig("workflow_dispatch", "inputs")["guard_code"] = {
        "required" => true, "type" => "string"
      }
    end
  end

  def test_maintenance_requires_permission_query_and_exact_artifact_digest
    assert_maintenance_rejected("unverified permission") do |workflow|
      workflow.dig("jobs", "maintain", "steps").last["run"] =
        workflow.dig("jobs", "maintain", "steps").last["run"]
          .sub("--verify-actor-permission", "")
    end
    assert_maintenance_rejected("artifact without digest") do |workflow|
      triggers = workflow.key?("on") ? workflow["on"] : workflow[true]
      triggers.dig("workflow_dispatch", "inputs").delete("evidence_sha256")
    end
  end

  def test_maintenance_rejects_guessed_candidate_and_mutable_receipt
    assert_maintenance_rejected("guessed candidate") do |workflow|
      triggers = workflow.key?("on") ? workflow["on"] : workflow[true]
      triggers.dig("workflow_dispatch", "inputs")["candidate_sha256"] = {
        "required" => false, "type" => "string"
      }
    end
    assert_maintenance_rejected("mutable receipt") do |workflow|
      workflow.dig("jobs", "maintain", "steps").last["run"] =
        workflow.dig("jobs", "maintain", "steps").last["run"]
          .sub("--immutable", "--replace")
    end
  end

  def test_maintenance_cannot_swallow_validation_failure
    assert_maintenance_rejected("continue on error") do |workflow|
      workflow.dig("jobs", "maintain", "steps").last["continue-on-error"] = true
    end
  end
end
