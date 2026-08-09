#!/usr/bin/env ruby
# frozen_string_literal: true

require "minitest/autorun"
require "yaml"
require_relative "check_abi_staging_workflows"

class AbiStagingWorkflowCheckerTest < Minitest::Test
  ROOT = File.expand_path("..", __dir__)
  WORKFLOW = File.join(ROOT, ".github/workflows/abi-staging-reconcile.yml")

  def setup
    @workflow = YAML.safe_load(File.read(WORKFLOW), permitted_classes: [], aliases: false)
  end

  def copy
    Marshal.load(Marshal.dump(@workflow))
  end

  def assert_rejected(label)
    changed = copy
    yield changed
    error = assert_raises(AbiStagingWorkflowCheck::Violation, label) do
      AbiStagingWorkflowCheck.check(changed)
    end
    refute_empty error.message
  end

  def test_reviewed_workflow_passes
    AbiStagingWorkflowCheck.check(@workflow)
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
end
