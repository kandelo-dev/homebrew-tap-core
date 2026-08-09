#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

module AbiStagingWorkflowCheck
  CHECKOUT = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
  SETUP_PYTHON = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
  FULL_ACTION = %r{\A(?:\./|[^@]+@[0-9a-f]{40})\z}

  class Violation < RuntimeError; end

  module_function

  def require_contract(condition, message)
    raise Violation, message unless condition
  end

  def triggers(workflow)
    workflow.key?("on") ? workflow.fetch("on") : workflow.fetch(true)
  end

  def flatten(value, result = [])
    case value
    when Hash
      value.each do |key, child|
        result << key.to_s
        flatten(child, result)
      end
    when Array
      value.each { |child| flatten(child, result) }
    else
      result << value.to_s
    end
    result
  end

  def check_actions(job)
    job.fetch("steps").each do |step|
      next unless step.key?("uses")

      require_contract(step.fetch("uses").match?(FULL_ACTION),
                       "third-party action is not pinned to a full SHA")
    end
  end

  def check(workflow)
    require_contract(workflow.fetch("permissions") == {},
                     "workflow permissions must be empty")
    event = triggers(workflow)
    require_contract(event.keys.sort == %w[schedule workflow_dispatch],
                     "workflow must have only schedule and workflow_dispatch triggers")
    require_contract(event.fetch("schedule") == [{"cron" => "*/5 * * * *"}],
                     "workflow schedule must remain five minutes")
    input = event.dig("workflow_dispatch", "inputs", "request_asset_url")
    require_contract(input.is_a?(Hash) && input["required"] == false &&
                       input["type"] == "string",
                     "manual workflow must accept one optional request_asset_url")
    require_contract(event.fetch("workflow_dispatch").fetch("inputs").keys ==
                       ["request_asset_url"],
                     "manual workflow gained another coordinator input")

    jobs = workflow.fetch("jobs")
    require_contract(jobs.keys == ["reconcile"],
                     "workflow must have one reconcile job")
    job = jobs.fetch("reconcile")
    require_contract(job.fetch("permissions") == {"contents" => "read"},
                     "reconcile job must remain contents-read only")
    require_contract(job.fetch("runs-on") == "ubuntu-latest",
                     "reconcile runner changed")
    require_contract(job.fetch("timeout-minutes").between?(1, 15),
                     "reconcile timeout is not bounded")
    require_contract(!job.key?("environment"),
                     "reconcile job may not enter a credentialed environment")
    require_contract(job.fetch("steps").none? { |step| step["continue-on-error"] == true },
                     "reconcile workflow may not swallow a failure")
    check_actions(job)

    checkout = job.fetch("steps").find { |step| step["uses"]&.start_with?(CHECKOUT) }
    setup_python = job.fetch("steps").find { |step| step["uses"]&.start_with?(SETUP_PYTHON) }
    require_contract(!checkout.nil? && !setup_python.nil?,
                     "workflow lacks its pinned checkout or Python setup")
    require_contract(checkout.fetch("with", {}) == {
                       "ref" => "refs/heads/main",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "checkout must capture protected tap main without credentials")
    require_contract(setup_python.fetch("with", {}).fetch("python-version") == "3.13",
                     "workflow Python version changed")

    coordinator_steps = job.fetch("steps").select { |step| step.key?("run") }
    require_contract(coordinator_steps.length == 1,
                     "workflow must use one reviewed reconciliation coordinator")
    coordinator = coordinator_steps.fetch(0)
    source = coordinator.fetch("run")
    env = coordinator.fetch("env", {})
    require_contract(env == {
                       "REQUEST_ASSET_URL" => "${{ inputs.request_asset_url }}"
                     }, "manual URL is not isolated as one data value")
    require_contract(coordinator.fetch("working-directory") == "tap-authority",
                     "coordinator must execute protected tap-main code")
    require_contract(source.include?("python3 -m scripts.abi_staging.cli scan") &&
                       source.include?("python3 -m scripts.abi_staging.cli reconcile") &&
                       source.include?('--request-asset-url "$REQUEST_ASSET_URL"'),
                     "scheduled and manual paths do not share the protected CLI")
    require_contract(source.include?("GITHUB_STEP_SUMMARY") &&
                       source.include?("4194304") &&
                       source.include?("MAX_SUMMARY_DECISIONS = 256"),
                     "workflow summary is not explicitly bounded")
    require_contract(!source.match?(/\b(?:eval|source|curl|wget)\b/) &&
                       !source.match?(/bash\s+[^\n]*REQUEST_ASSET_URL/) &&
                       !source.match?(/gh\s+(?:workflow|api)/),
                     "workflow executes request data or dispatches external work")

    all_text = flatten(workflow).join("\n")
    require_contract(!all_text.match?(/\bsecrets\b/i),
                     "workflow may not use secrets")
    require_contract(!all_text.match?(/contents:\s*write|packages:\s*write/),
                     "workflow gained write authority")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "workflow structure is incomplete: #{error.message}"
  end

  def check_maintenance(workflow)
    require_contract(workflow.fetch("permissions") == {},
                     "maintenance workflow permissions must be empty")
    event = triggers(workflow)
    require_contract(event.keys == ["workflow_dispatch"],
                     "maintenance workflow must be manual-only")
    inputs = event.fetch("workflow_dispatch").fetch("inputs")
    require_contract(inputs.keys == %w[command evidence_artifact_id evidence_sha256 justification],
                     "maintenance workflow gained a free-form selector")
    command = inputs.fetch("command")
    require_contract(command.fetch("required") == true && command.fetch("type") == "choice" &&
                     command.fetch("options") == %w[authorize-capture accept-artifact-risk retry-exhausted],
                     "maintenance commands must remain a closed choice")
    %w[evidence_artifact_id evidence_sha256 justification].each do |name|
      input = inputs.fetch(name)
      require_contract(input.fetch("required") == true && input.fetch("type") == "string",
                       "maintenance input #{name} changed")
    end

    jobs = workflow.fetch("jobs")
    require_contract(jobs.keys == ["maintain"],
                     "maintenance workflow must have one protected writer")
    job = jobs.fetch("maintain")
    require_contract(job.fetch("permissions") == {
                       "actions" => "read",
                       "contents" => "read",
                       "packages" => "write"
                     }, "maintenance writer permissions changed")
    require_contract(job.fetch("runs-on") == "ubuntu-latest",
                     "maintenance runner changed")
    require_contract(job.fetch("timeout-minutes").between?(1, 30),
                     "maintenance timeout is not bounded")
    require_contract(!job.key?("environment"),
                     "maintenance job may not enter a credentialed environment")
    require_contract(job.fetch("steps").none? { |step| step["continue-on-error"] == true },
                     "maintenance workflow may not swallow validation failure")
    check_actions(job)

    checkout = job.fetch("steps").find { |step| step["uses"]&.start_with?(CHECKOUT) }
    setup_python = job.fetch("steps").find { |step| step["uses"]&.start_with?(SETUP_PYTHON) }
    require_contract(!checkout.nil? && !setup_python.nil?,
                     "maintenance workflow lacks pinned checkout or Python setup")
    require_contract(checkout.fetch("with", {}) == {
                       "ref" => "refs/heads/main",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "maintenance must execute protected tap main without Git credentials")
    require_contract(setup_python.fetch("with", {}).fetch("python-version") == "3.13",
                     "maintenance Python version changed")

    commands = job.fetch("steps").select { |step| step.key?("run") }
    require_contract(commands.length == 1,
                     "maintenance must use one reviewed coordinator")
    coordinator = commands.fetch(0)
    require_contract(coordinator.fetch("working-directory") == "tap-authority",
                     "maintenance coordinator must execute protected tap-main code")
    require_contract(coordinator.fetch("env") == {
                       "AUTHORIZATION_REFERENCE" => "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}/attempts/${{ github.run_attempt }}",
                       "EVIDENCE_ARTIFACT_ID" => "${{ inputs.evidence_artifact_id }}",
                       "EVIDENCE_SHA256" => "${{ inputs.evidence_sha256 }}",
                       "GITHUB_ACTOR" => "${{ github.actor }}",
                       "GITHUB_REPOSITORY" => "${{ github.repository }}",
                       "GITHUB_RUN_ATTEMPT" => "${{ github.run_attempt }}",
                       "GITHUB_RUN_ID" => "${{ github.run_id }}",
                       "GITHUB_TOKEN" => "${{ github.token }}",
                       "HOMEBREW_GITHUB_PACKAGES_TOKEN" => "${{ github.token }}",
                       "HOMEBREW_GITHUB_PACKAGES_USER" => "${{ github.actor }}",
                       "JUSTIFICATION" => "${{ inputs.justification }}",
                       "MAINTENANCE_COMMAND" => "${{ inputs.command }}"
                     }, "maintenance inputs are not isolated as data")
    source = coordinator.fetch("run")
    require_contract(source.include?("python3 -m scripts.abi_staging.override") &&
                     source.include?('--evidence-artifact-id "$EVIDENCE_ARTIFACT_ID"') &&
                     source.include?('--evidence-sha256 "$EVIDENCE_SHA256"') &&
                     source.include?("--verify-actor-permission") &&
                     source.include?("--immutable"),
                     "maintenance does not validate exact evidence, authority, and immutability")
    require_contract(!source.match?(/\b(?:eval|source|curl|wget|sleep)\b/) &&
                     !source.match?(/abi-staging-(?:build|verify)-bottle/) &&
                     !source.match?(/\b(?:brew|make|cmake|cargo|npm)\b/) &&
                     !source.include?("--replace"),
                     "write-capable maintenance executes candidate code or mutable publication")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "maintenance workflow structure is incomplete: #{error.message}"
  end
end

if $PROGRAM_NAME == __FILE__
  root = File.expand_path("..", __dir__)
  begin
    paths = if ARGV.empty?
      [
        [File.join(root, ".github/workflows/abi-staging-reconcile.yml"), :check],
        [File.join(root, ".github/workflows/abi-staging-maintenance.yml"), :check_maintenance]
      ]
    else
      ARGV.map do |path|
        method = File.basename(path) == "abi-staging-maintenance.yml" ? :check_maintenance : :check
        [path, method]
      end
    end
    paths.each do |path, method|
      workflow = YAML.safe_load(File.read(path), permitted_classes: [], aliases: false)
      AbiStagingWorkflowCheck.public_send(method, workflow)
    end
    puts "check_abi_staging_workflows: PASS"
  rescue Errno::ENOENT, Psych::Exception, AbiStagingWorkflowCheck::Violation => error
    warn "check_abi_staging_workflows: #{error.message}"
    exit 1
  end
end
