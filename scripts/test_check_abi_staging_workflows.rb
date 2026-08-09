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

  def test_write_permission_is_rejected
    assert_rejected("write permission") do |workflow|
      workflow.dig("jobs", "reconcile", "permissions")["contents"] = "write"
    end
  end

  def test_secret_and_request_execution_are_rejected
    assert_rejected("secret") do |workflow|
      workflow.dig("jobs", "reconcile")["env"] = {"TOKEN" => "${{ secrets.TOKEN }}"}
    end
    assert_rejected("request execution") do |workflow|
      workflow.dig("jobs", "reconcile", "steps").last["run"] = 'bash "$REQUEST_ASSET_URL"'
    end
  end

  def test_request_cannot_select_checkout_or_coordinator
    assert_rejected("checkout ref") do |workflow|
      checkout = workflow.dig("jobs", "reconcile", "steps").find { |step| step["uses"]&.start_with?(AbiStagingWorkflowCheck::CHECKOUT) }
      checkout.fetch("with")["ref"] = "${{ inputs.request_asset_url }}"
    end
    assert_rejected("coordinator") do |workflow|
      workflow.dig("jobs", "reconcile", "steps").last["run"] =
        "python3 request-supplied-reconciler.py"
    end
  end

  def test_dispatch_and_pr_triggers_are_rejected
    assert_rejected("build dispatch") do |workflow|
      workflow.dig("jobs", "reconcile", "steps").last["run"] += "\ngh workflow run build.yml"
    end
    assert_rejected("repository dispatch") do |workflow|
      triggers = workflow.key?("on") ? workflow["on"] : workflow[true]
      triggers["repository_dispatch"] = {}
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
