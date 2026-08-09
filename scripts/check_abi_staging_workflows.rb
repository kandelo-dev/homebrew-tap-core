#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

module AbiStagingWorkflowCheck
  CHECKOUT = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
  SETUP_PYTHON = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
  UPLOAD_ARTIFACT = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
  DOWNLOAD_ARTIFACT = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
  FULL_ACTION = %r{\A(?:\./[^@\n]+|[^@]+@[0-9a-f]{40})\z}

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

  def run_steps(job)
    job.fetch("steps").select { |step| step.key?("run") }
  end

  def action_steps(job, action)
    job.fetch("steps").select { |step| step["uses"]&.start_with?(action) }
  end

  def require_no_candidate_execution(source, field)
    require_contract(!source.match?(/\b(?:eval|source|curl|wget|sleep)\b/) &&
                     !source.match?(/abi-staging-(?:build|verify)-bottle/) &&
                     !source.match?(/(?:^|\s)(?:bash|sh)\s+(?:handoff|verification)[^\n]*/) &&
                     !source.match?(/\b(?:brew|make|cmake|cargo|npm)\b/) &&
                     !source.match?(/oras\s+push/) &&
                     !source.include?("--replace"),
                     "#{field} executes candidate data or mutable publication")
  end

  def require_matrix(job, expression, field)
    strategy = job.fetch("strategy")
    require_contract(strategy.fetch("fail-fast") == false,
                     "#{field} matrix must not cancel independent siblings")
    require_contract(strategy.fetch("max-parallel").between?(1, 16),
                     "#{field} matrix is not bounded")
    require_contract(strategy.fetch("matrix") == expression,
                     "#{field} matrix is not protected discovery output")
  end

  def require_candidate_checkouts(job, field)
    checkouts = action_steps(job, CHECKOUT)
    require_contract(checkouts.length == 2,
                     "#{field} must check out exactly tap and Kandelo sources")
    tap = checkouts.find { |step| step.dig("with", "path") == "tap-authority" }
    kandelo = checkouts.find { |step| step.dig("with", "path") == "kandelo-source" }
    require_contract(!tap.nil? && tap.fetch("with") == {
                       "repository" => "kandelo-dev/homebrew-tap-core",
                       "ref" => "${{ needs.discover-plan.outputs.tap-commit }}",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "#{field} tap checkout is not the exact protected revision")
    require_contract(!kandelo.nil? && kandelo.fetch("with") == {
                       "repository" => "${{ needs.discover-plan.outputs.kandelo-repository }}",
                       "ref" => "${{ needs.discover-plan.outputs.kandelo-head }}",
                       "fetch-depth" => 1,
                       "submodules" => "recursive",
                       "persist-credentials" => false,
                       "path" => "kandelo-source"
                     }, "#{field} Kandelo checkout is not the exact request head")
  end

  def require_protected_checkout(job, field)
    checkouts = action_steps(job, CHECKOUT)
    require_contract(checkouts.length == 1 && checkouts.fetch(0).fetch("with") == {
                       "ref" => "refs/heads/main",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "#{field} must execute protected tap main")
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
    expected_jobs = %w[discover-plan build-candidate publish-candidate verify-candidate publish-receipt]
    require_contract(jobs.keys == expected_jobs,
                     "workflow job split changed")
    jobs.each_value do |job|
      require_contract(job.fetch("runs-on") == "ubuntu-latest",
                       "reconciliation runner changed")
      require_contract(!job.key?("environment"),
                       "reconciliation jobs may not enter a credentialed environment")
      require_contract(job.fetch("steps").none? { |step| step["continue-on-error"] == true },
                       "reconciliation workflow may not swallow a step failure")
      require_contract(!job.key?("secrets"),
                       "reconciliation jobs may not inherit secrets")
      check_actions(job)
    end

    discover = jobs.fetch("discover-plan")
    require_contract(discover.fetch("permissions") == {"contents" => "read"},
                     "discovery must remain contents-read only")
    require_contract(discover.fetch("timeout-minutes").between?(1, 30),
                     "discovery timeout is not bounded")
    discovery_checkouts = action_steps(discover, CHECKOUT)
    require_contract(discovery_checkouts.length == 2,
                     "discovery must check out protected tap and exact Kandelo sources")
    protected_tap = discovery_checkouts.find do |step|
      step.dig("with", "path") == "tap-authority"
    end
    exact_kandelo = discovery_checkouts.find do |step|
      step.dig("with", "path") == "kandelo-source"
    end
    require_contract(!protected_tap.nil? && protected_tap.fetch("with") == {
                       "ref" => "refs/heads/main",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "discovery tap checkout is not protected main")
    require_contract(!exact_kandelo.nil? && exact_kandelo.fetch("if") ==
                       "steps.discover.outputs.selected == 'true'" &&
                       exact_kandelo.fetch("with") == {
                         "repository" => "${{ steps.discover.outputs.kandelo_repository }}",
                         "ref" => "${{ steps.discover.outputs.kandelo_head }}",
                         "fetch-depth" => 1,
                         "submodules" => "recursive",
                         "persist-credentials" => false,
                         "path" => "kandelo-source"
                       }, "discovery Kandelo checkout is not the exact request head")
    setup = action_steps(discover, SETUP_PYTHON)
    require_contract(setup.length == 1 &&
                       setup.fetch(0).dig("with", "python-version") == "3.13",
                     "discovery lacks declared Python")
    commands = run_steps(discover)
    require_contract(commands.length == 3,
                     "discovery must separate request, requirements, and coordination")
    request_discovery = commands.find { |step| step["id"] == "discover" }
    requirements = commands.find { |step| step["id"] == "requirements" }
    coordinator = commands.find { |step| step["id"] == "coordinate" }
    require_contract(!request_discovery.nil? && !requirements.nil? && !coordinator.nil?,
                     "discovery command identities changed")
    source = request_discovery.fetch("run")
    require_contract(request_discovery.fetch("working-directory") == "tap-authority" &&
                       request_discovery.fetch("env") == {
                         "REQUEST_ASSET_URL" => "${{ inputs.request_asset_url }}"
                       }, "manual URL is not isolated as data")
    require_contract(source.include?("python3 -m scripts.abi_staging.cli discover-workflow-request") &&
                       source.include?('--tap-root "$PWD"') &&
                       source.include?('--request-asset-url "$REQUEST_ASSET_URL"') &&
                       source.include?('--cycle-index "$GITHUB_RUN_NUMBER"') &&
                       source.include?('--github-output "$GITHUB_OUTPUT"'),
                     "discovery does not stage one exact public request")
    require_contract(!source.match?(/\b(?:eval|source|curl|wget|sleep)\b/) &&
                       !source.match?(/bash\s+[^\n]*REQUEST_ASSET_URL/) &&
                       !source.match?(/gh\s+(?:workflow|api)/),
                     "discovery executes request data or dispatches work")
    requirements_source = requirements.fetch("run")
    require_contract(requirements.fetch("if") ==
                       "steps.discover.outputs.selected == 'true'" &&
                       requirements.fetch("working-directory") == "kandelo-source" &&
                       requirements_source.include?("scripts/dev-shell.sh") &&
                       requirements_source.include?("abi-staging requirements") &&
                       requirements_source.include?('--change-classes "$classes"') &&
                       requirements_source.include?("images/vfs/products/generated/catalog.json") &&
                       requirements_source.include?("pages-vfs-products.toml") &&
                       requirements_source.include?("tests/vfs-products.toml"),
                     "requirements do not come from exact-head product authorities")
    coordinator_source = coordinator.fetch("run")
    require_contract(coordinator.fetch("if") ==
                       "steps.discover.outputs.selected == 'true'" &&
                       coordinator.fetch("working-directory") == "tap-authority" &&
                       coordinator_source.include?("python3 -m scripts.abi_staging.cli prepare-workflow") &&
                       coordinator_source.include?('--kandelo-root "$GITHUB_WORKSPACE/kandelo-source"') &&
                       coordinator_source.include?('--discovery "$out/discovery.json"') &&
                       coordinator_source.include?('--formula-requirements "$RUNNER_TEMP/abi-staging-formula-requirements.json"'),
                     "coordination does not bind exact-head request requirements")
    steps = discover.fetch("steps")
    require_contract(steps.index(request_discovery) < steps.index(exact_kandelo) &&
                       steps.index(exact_kandelo) < steps.index(requirements) &&
                       steps.index(requirements) < steps.index(coordinator),
                     "discovery stages exact sources in the wrong order")
    exact_setup = discover.fetch("steps").find do |step|
      step["uses"] == "./kandelo-source/.github/actions/setup-nix"
    end
    require_contract(!exact_setup.nil? && exact_setup.fetch("if") ==
                       "steps.discover.outputs.selected == 'true'",
                     "discovery lacks exact-head Kandelo setup")
    discovery_source = commands.map { |step| step.fetch("run") }.join("\n")
    require_contract(!discovery_source.match?(/gh\s+(?:workflow|api)/) &&
                       !discovery_source.match?(/(?:^|\s)sleep(?:\s|$)/),
                     "discovery dispatches work or sleeps on a runner")
    uploads = action_steps(discover, UPLOAD_ARTIFACT)
    require_contract(uploads.length == 1 &&
                       uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
                       uploads.fetch(0).dig("with", "compression-level") == 0,
                     "protected coordination artifact is not exact and bounded")

    candidate_permissions = {"contents" => "read"}
    writer_permissions = {"actions" => "read", "contents" => "read", "packages" => "write"}
    build = jobs.fetch("build-candidate")
    verify = jobs.fetch("verify-candidate")
    publisher = jobs.fetch("publish-candidate")
    receipt = jobs.fetch("publish-receipt")
    require_contract(build.fetch("permissions") == candidate_permissions &&
                       verify.fetch("permissions") == candidate_permissions,
                     "candidate execution gained write authority")
    require_contract(publisher.fetch("permissions") == writer_permissions &&
                       receipt.fetch("permissions") == writer_permissions,
                     "publisher permissions changed")
    require_contract(build.fetch("timeout-minutes") == 360 &&
                       verify.fetch("timeout-minutes") == 360,
                     "candidate execution timeout must remain six hours")
    require_contract(publisher.fetch("timeout-minutes").between?(1, 30) &&
                       receipt.fetch("timeout-minutes").between?(1, 30),
                     "publisher timeout is not bounded")

    build_matrix = "${{ fromJSON(needs.discover-plan.outputs.build-matrix) }}"
    verify_matrix = "${{ fromJSON(needs.discover-plan.outputs.verify-matrix) }}"
    require_matrix(build, build_matrix, "build")
    require_matrix(publisher, build_matrix, "candidate publisher")
    require_matrix(verify, verify_matrix, "verification")
    require_matrix(receipt, verify_matrix, "receipt publisher")
    require_contract(build.fetch("needs") == "discover-plan" &&
                       verify.fetch("needs") == "discover-plan",
                     "candidate work gained a cross-class or global gate")
    require_contract(publisher.fetch("needs") == %w[discover-plan build-candidate] &&
                       receipt.fetch("needs") == %w[discover-plan verify-candidate],
                     "publisher dependency split changed")

    require_candidate_checkouts(build, "build")
    require_candidate_checkouts(verify, "verification")
    [build, verify].each do |job|
      downloads = action_steps(job, DOWNLOAD_ARTIFACT)
      require_contract(downloads.length == 1 &&
                         downloads.fetch(0).dig("with", "artifact-ids") ==
                           "${{ needs.discover-plan.outputs.coordination-artifact-id }}" &&
                         downloads.fetch(0).dig("with", "merge-multiple") == true,
                       "candidate work does not consume exact protected coordination")
      command = run_steps(job)
      require_contract(command.length == 1 &&
                         command.fetch(0).fetch("run").include?("env -u GITHUB_TOKEN") &&
                         command.fetch(0).fetch("run").include?("scripts/dev-shell.sh") &&
                         command.fetch(0).fetch("run").include?('--run-id "$GITHUB_RUN_ID"') &&
                         command.fetch(0).fetch("run").include?('--run-attempt "$GITHUB_RUN_ATTEMPT"') &&
                         command.fetch(0).fetch("run").include?('--workflow-ref "$GITHUB_WORKFLOW_REF"'),
                       "candidate work is not uncredentialed repository-tool execution")
      uploads = action_steps(job, UPLOAD_ARTIFACT)
      require_contract(uploads.length == 1 &&
                         uploads.fetch(0).fetch("if") == "always()" &&
                         uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
                         uploads.fetch(0).dig("with", "compression-level") == 0,
                       "candidate handoff upload is not exact")
    end

    require_protected_checkout(publisher, "candidate publisher")
    require_protected_checkout(receipt, "receipt publisher")
    [publisher, receipt].each do |job|
      commands = run_steps(job)
      require_contract(commands.length == 1,
                       "publisher must have one reviewed coordinator")
      command = commands.fetch(0)
      require_contract(command.fetch("working-directory") == "tap-authority",
                       "publisher must execute protected tap-main code")
      publisher_source = command.fetch("run")
      require_contract(publisher_source.include?("--require-github-digest") &&
                         publisher_source.include?("--anonymous-readback") &&
                         publisher_source.include?("--immutable") &&
                         publisher_source.include?('--run-id "$GITHUB_RUN_ID"') &&
                         publisher_source.include?('--head-sha "$GITHUB_SHA"'),
                       "publisher lacks exact run/artifact/readback identity")
      require_no_candidate_execution(publisher_source, "publisher")
      uploads = action_steps(job, UPLOAD_ARTIFACT)
      require_contract(uploads.length == 1 &&
                         uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
                         uploads.fetch(0).dig("with", "compression-level") == 0,
                       "publisher result locator is not retained exactly")
    end
    require_contract(run_steps(publisher).fetch(0).fetch("run").include?(
                       "python3 -m scripts.abi_staging.cli publish-workflow-candidate"),
                     "candidate publisher bypasses protected CLI")
    require_contract(run_steps(receipt).fetch(0).fetch("run").include?(
                       "python3 -m scripts.abi_staging.cli publish-workflow-receipt"),
                     "receipt publisher bypasses protected CLI")

    all_text = flatten(workflow).join("\n")
    require_contract(!all_text.match?(/\bsecrets\b/i),
                     "workflow may not use repository secrets")
    require_contract(!all_text.match?(/(?:^|\s)sleep(?:\s|$)/),
                     "workflow retries may not sleep on runners")
    require_contract(!all_text.match?(/:[[:space:]]*(?:latest|candidate|current)(?:\s|$)/),
                     "workflow names a mutable candidate tag")
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
