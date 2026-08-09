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
    require_contract(checkouts.length == 3,
                     "#{field} must check out tap, candidate, and policy sources")
    tap = checkouts.find { |step| step.dig("with", "path") == "tap-authority" }
    kandelo = checkouts.find { |step| step.dig("with", "path") == "kandelo-source" }
    policy = checkouts.find { |step| step.dig("with", "path") == "kandelo-authority" }
    require_contract(!tap.nil? && tap.fetch("with") == {
                       "repository" => "kandelo-dev/homebrew-tap-core",
                       "ref" => "${{ inputs.tap-commit }}",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "#{field} tap checkout is not the exact protected revision")
    require_contract(!kandelo.nil? && kandelo.fetch("with") == {
                       "repository" => "${{ inputs.kandelo-repository }}",
                       "ref" => "${{ inputs.kandelo-head }}",
                       "fetch-depth" => 1,
                       "submodules" => "recursive",
                       "persist-credentials" => false,
                       "path" => "kandelo-source"
                     }, "#{field} Kandelo checkout is not the exact request head")
    require_contract(!policy.nil? && policy.fetch("with") == {
                       "repository" => "${{ inputs.kandelo-repository }}",
                       "ref" => "${{ inputs.kandelo-policy-commit }}",
                       "fetch-depth" => 1,
                       "submodules" => "recursive",
                       "persist-credentials" => false,
                       "path" => "kandelo-authority"
                     }, "#{field} Kandelo policy checkout is not immutable")
    setup = job.fetch("steps").select do |step|
      step["uses"]&.end_with?("/.github/actions/setup-nix")
    end
    require_contract(setup.length == 1 &&
                     setup.fetch(0).fetch("uses") ==
                       "./kandelo-authority/.github/actions/setup-nix",
                     "#{field} executes candidate-controlled setup code")
  end

  def require_protected_checkout(job, field)
    checkouts = action_steps(job, CHECKOUT)
    require_contract(checkouts.length == 1 && checkouts.fetch(0).fetch("with") == {
                       "ref" => "${{ inputs.tap-commit }}",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "#{field} must execute the exact protected tap revision")
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
    expected_jobs = %w[discover-plan candidate verification reuse]
    require_contract(jobs.keys == expected_jobs,
                     "workflow job split changed")

    discover = jobs.fetch("discover-plan")
    require_contract(discover.fetch("runs-on") == "ubuntu-latest" &&
                     !discover.key?("environment") &&
                     !discover.key?("secrets") &&
                     discover.fetch("steps").none? do |step|
                       step["continue-on-error"] == true ||
                         step["uses"]&.start_with?("./kandelo-source/")
                     end, "discovery executes with an unsafe workflow capability")
    check_actions(discover)
    require_contract(discover.fetch("permissions") == {"contents" => "read"},
                     "discovery must remain contents-read only")
    require_contract(discover.fetch("timeout-minutes").between?(1, 30),
                     "discovery timeout is not bounded")
    discovery_checkouts = action_steps(discover, CHECKOUT)
    require_contract(discovery_checkouts.length == 3,
                     "discovery must check out tap, candidate, and policy sources")
    protected_tap = discovery_checkouts.find do |step|
      step.dig("with", "path") == "tap-authority"
    end
    exact_kandelo = discovery_checkouts.find do |step|
      step.dig("with", "path") == "kandelo-source"
    end
    policy_kandelo = discovery_checkouts.find do |step|
      step.dig("with", "path") == "kandelo-authority"
    end
    require_contract(!protected_tap.nil? && protected_tap.fetch("with") == {
                       "ref" => "${{ github.sha }}",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "discovery tap checkout is not the exact protected run commit")
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
    require_contract(!policy_kandelo.nil? && policy_kandelo.fetch("if") ==
                       "steps.discover.outputs.selected == 'true'" &&
                       policy_kandelo.fetch("with") == {
                         "repository" => "${{ steps.discover.outputs.kandelo_repository }}",
                         "ref" => "${{ steps.discover.outputs.kandelo_policy_commit }}",
                         "fetch-depth" => 1,
                         "submodules" => "recursive",
                         "persist-credentials" => false,
                         "path" => "kandelo-authority"
                       }, "discovery Kandelo policy checkout is not immutable")
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
                       requirements.fetch("working-directory") == "kandelo-authority" &&
                       requirements_source.include?("scripts/dev-shell.sh") &&
                       requirements_source.include?("abi-staging requirements") &&
                       requirements_source.include?('--change-classes "$classes"') &&
                       requirements_source.include?('$GITHUB_WORKSPACE/kandelo-source/images/vfs/products/generated/catalog.json') &&
                       requirements_source.include?('$GITHUB_WORKSPACE/kandelo-source/apps/browser-demos/pages/kandelo/kernel-host/pages-vfs-products.toml') &&
                       requirements_source.include?('$GITHUB_WORKSPACE/kandelo-source/tests/vfs-products.toml'),
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
                       steps.index(exact_kandelo) < steps.index(policy_kandelo) &&
                       steps.index(policy_kandelo) < steps.index(requirements) &&
                       steps.index(requirements) < steps.index(coordinator),
                     "discovery stages exact sources in the wrong order")
    exact_setup = discover.fetch("steps").find do |step|
      step["uses"] == "./kandelo-authority/.github/actions/setup-nix"
    end
    require_contract(!exact_setup.nil? && exact_setup.fetch("if") ==
                       "steps.discover.outputs.selected == 'true'",
                     "discovery lacks immutable Kandelo policy setup")
    discovery_source = commands.map { |step| step.fetch("run") }.join("\n")
    require_contract(!discovery_source.match?(/gh\s+(?:workflow|api)/) &&
                       !discovery_source.match?(/(?:^|\s)sleep(?:\s|$)/),
                     "discovery dispatches work or sleeps on a runner")
    uploads = action_steps(discover, UPLOAD_ARTIFACT)
    require_contract(uploads.length == 1 &&
                       uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
                       uploads.fetch(0).dig("with", "compression-level") == 0,
                     "protected coordination artifact is not exact and bounded")

    writer_permissions = {"actions" => "read", "contents" => "read", "packages" => "write"}
    candidate = jobs.fetch("candidate")
    verification = jobs.fetch("verification")
    reuse = jobs.fetch("reuse")
    build_matrix = "${{ fromJSON(needs.discover-plan.outputs.build-matrix) }}"
    reuse_matrix = "${{ fromJSON(needs.discover-plan.outputs.reuse-matrix) }}"
    verify_matrix = "${{ fromJSON(needs.discover-plan.outputs.verify-matrix) }}"
    require_matrix(candidate, build_matrix, "candidate")
    require_matrix(reuse, reuse_matrix, "reuse")
    require_matrix(verification, verify_matrix, "verification")
    common_inputs = {
      "coordination-artifact-id" => "${{ needs.discover-plan.outputs.coordination-artifact-id }}",
      "coordination-artifact-digest" => "${{ needs.discover-plan.outputs.coordination-artifact-digest }}",
      "kandelo-head" => "${{ needs.discover-plan.outputs.kandelo-head }}",
      "kandelo-policy-commit" => "${{ needs.discover-plan.outputs.kandelo-policy-commit }}",
      "kandelo-repository" => "${{ needs.discover-plan.outputs.kandelo-repository }}",
      "tap-commit" => "${{ needs.discover-plan.outputs.tap-commit }}",
      "work-id" => "${{ matrix.work_id }}"
    }
    [[candidate, "./.github/workflows/abi-staging-candidate.yml"],
     [verification, "./.github/workflows/abi-staging-verification.yml"]].each do |job, reusable|
      require_contract(job.fetch("needs") == "discover-plan" &&
                       job.fetch("if") == "needs.discover-plan.outputs.mode == 'active'" &&
                       job.fetch("permissions") == writer_permissions &&
                       job.fetch("uses") == reusable &&
                       job.fetch("with") == common_inputs &&
                       !job.key?("secrets") &&
                       !job.key?("runs-on") &&
                       !job.key?("steps"),
                       "reusable workflow caller changed authority or exact inputs")
    end
    reuse_inputs = {
      "coordination-artifact-id" => "${{ needs.discover-plan.outputs.coordination-artifact-id }}",
      "coordination-artifact-digest" => "${{ needs.discover-plan.outputs.coordination-artifact-digest }}",
      "tap-commit" => "${{ needs.discover-plan.outputs.tap-commit }}",
      "work-id" => "${{ matrix.work_id }}"
    }
    require_contract(reuse.fetch("needs") == "discover-plan" &&
                     reuse.fetch("if") == "needs.discover-plan.outputs.mode == 'active'" &&
                     reuse.fetch("permissions") == writer_permissions &&
                     reuse.fetch("uses") == "./.github/workflows/abi-staging-reuse.yml" &&
                     reuse.fetch("with") == reuse_inputs &&
                     !reuse.key?("secrets") && !reuse.key?("runs-on") &&
                     !reuse.key?("steps"),
                     "reuse caller changed authority or exact inputs")

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

  def check_reuse(workflow)
    require_contract(workflow.fetch("permissions") == {},
                     "reuse workflow permissions must be empty")
    event = triggers(workflow)
    require_contract(event.keys == ["workflow_call"],
                     "reuse workflow must be workflow_call only")
    expected_inputs = %w[
      coordination-artifact-id coordination-artifact-digest tap-commit work-id
    ]
    inputs = event.fetch("workflow_call").fetch("inputs")
    require_contract(inputs.keys == expected_inputs && inputs.values.all? do |input|
                       input == {"required" => true, "type" => "string"}
                     end, "reuse workflow inputs changed")
    jobs = workflow.fetch("jobs")
    require_contract(jobs.keys == ["publish"],
                     "reuse workflow must have one protected writer")
    publisher = jobs.fetch("publish")
    require_contract(publisher.fetch("runs-on") == "ubuntu-latest" &&
                     publisher.fetch("timeout-minutes").between?(1, 30) &&
                     publisher.fetch("name") == "publish-reuse ${{ inputs.work-id }}" &&
                     publisher.fetch("permissions") == {
                       "actions" => "read", "contents" => "read", "packages" => "write"
                     } && !publisher.key?("environment") && !publisher.key?("secrets") &&
                     publisher.fetch("steps").none? do |step|
                       step["continue-on-error"] == true
                     end, "reuse publisher authority changed")
    check_actions(publisher)
    require_protected_checkout(publisher, "reuse publisher")
    setup = action_steps(publisher, SETUP_PYTHON)
    require_contract(setup.length == 1 &&
                     setup.fetch(0).dig("with", "python-version") == "3.13",
                     "reuse publisher lacks declared Python")
    commands = run_steps(publisher)
    require_contract(commands.length == 1 &&
                     commands.fetch(0).fetch("working-directory") == "tap-authority" &&
                     commands.fetch(0).fetch("env") == {
                       "COORDINATION_ARTIFACT_DIGEST" => "${{ inputs.coordination-artifact-digest }}",
                       "COORDINATION_ARTIFACT_ID" => "${{ inputs.coordination-artifact-id }}",
                       "GITHUB_TOKEN" => "${{ github.token }}",
                       "HOMEBREW_GITHUB_PACKAGES_TOKEN" => "${{ github.token }}",
                       "HOMEBREW_GITHUB_PACKAGES_USER" => "${{ github.actor }}",
                       "TAP_COMMIT" => "${{ inputs.tap-commit }}",
                       "WORK_ID" => "${{ inputs.work-id }}"
                     }, "reuse publisher inputs changed")
    source = commands.fetch(0).fetch("run")
    required_flags = [
      '--coordination-artifact-id "$COORDINATION_ARTIFACT_ID"',
      '--coordination-artifact-digest "$COORDINATION_ARTIFACT_DIGEST"',
      '--head-sha "$TAP_COMMIT"',
      "--require-github-digest", "--anonymous-readback", "--immutable"
    ]
    require_contract(source.include?("python3 -m scripts.abi_staging.cli publish-workflow-reuse") &&
                     required_flags.all? { |flag| source.include?(flag) },
                     "reuse publisher lacks exact artifact/readback identity")
    require_no_candidate_execution(source, "reuse publisher")
    uploads = action_steps(publisher, UPLOAD_ARTIFACT)
    require_contract(uploads.length == 1 &&
                     uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
                     uploads.fetch(0).dig("with", "compression-level") == 0,
                     "reuse result locator is not retained exactly")
    all_text = flatten(workflow).join("\n")
    require_contract(!all_text.match?(/\bsecrets\b/i) &&
                     !all_text.match?(/(?:^|\s)sleep(?:\s|$)/),
                     "reuse workflow gains secrets or sleeps")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "reuse workflow structure is incomplete: #{error.message}"
  end

  def check_reusable(workflow, kind)
    require_contract(%i[candidate verification].include?(kind),
                     "reusable ABI workflow kind is unsupported")
    require_contract(workflow.fetch("permissions") == {},
                     "reusable workflow permissions must be empty")
    event = triggers(workflow)
    require_contract(event.keys == ["workflow_call"],
                     "reusable ABI workflow must be workflow_call only")
    expected_inputs = %w[
      coordination-artifact-id coordination-artifact-digest kandelo-head
      kandelo-policy-commit kandelo-repository tap-commit work-id
    ]
    inputs = event.fetch("workflow_call").fetch("inputs")
    require_contract(inputs.keys == expected_inputs && inputs.values.all? do |input|
                       input == {"required" => true, "type" => "string"}
                     end, "reusable ABI workflow inputs changed")

    producer_id = kind == :candidate ? "build" : "verify"
    producer_cli = kind == :candidate ? "execute-build-work" : "execute-verification-work"
    publisher_cli = kind == :candidate ?
      "publish-workflow-candidate" : "publish-workflow-receipt"
    producer_name = kind == :candidate ? "build-candidate" : "verify-candidate"
    handoff_prefix = kind == :candidate ?
      "abi-staging-build" : "abi-staging-verification"
    jobs = workflow.fetch("jobs")
    require_contract(jobs.keys == [producer_id, "publish"],
                     "reusable ABI workflow job split changed")
    producer = jobs.fetch(producer_id)
    publisher = jobs.fetch("publish")
    [producer, publisher].each do |job|
      require_contract(job.fetch("runs-on") == "ubuntu-latest" &&
                       !job.key?("environment") && !job.key?("secrets") &&
                       job.fetch("steps").none? do |step|
                         step["continue-on-error"] == true ||
                           step["uses"]&.start_with?("./kandelo-source/")
                       end, "reusable ABI job gained an unsafe capability")
      check_actions(job)
    end

    require_contract(producer.fetch("permissions") == {"contents" => "read"} &&
                     producer.fetch("timeout-minutes") == 360 &&
                     producer.fetch("name") == "#{producer_name} ${{ inputs.work-id }}" &&
                     producer.fetch("outputs") == {
                       "artifact-id" => "${{ steps.upload.outputs.artifact-id }}",
                       "artifact-digest" => "${{ steps.upload.outputs.artifact-digest }}"
                     }, "candidate producer authority or outputs changed")
    require_candidate_checkouts(producer, producer_name)
    downloads = action_steps(producer, DOWNLOAD_ARTIFACT)
    require_contract(downloads.length == 1 &&
                     downloads.fetch(0).fetch("with") == {
                       "artifact-ids" => "${{ inputs.coordination-artifact-id }}",
                       "path" => "${{ runner.temp }}/coordination",
                       "merge-multiple" => true
                     }, "candidate producer lacks exact coordination artifact")
    commands = run_steps(producer)
    require_contract(commands.length == 1 &&
                     commands.fetch(0).fetch("working-directory") == "tap-authority" &&
                     commands.fetch(0).fetch("env") == {
                       "WORK_ID" => "${{ inputs.work-id }}"
                     }, "candidate producer command inputs changed")
    source = commands.fetch(0).fetch("run")
    require_contract(source.include?("env -u GITHUB_TOKEN") &&
                     source.include?("../kandelo-authority/scripts/dev-shell.sh") &&
                     !source.include?("../kandelo-source/scripts/dev-shell.sh") &&
                     source.include?("python3 -m scripts.abi_staging.cli #{producer_cli}") &&
                     source.include?('--run-id "$GITHUB_RUN_ID"') &&
                     source.include?('--run-attempt "$GITHUB_RUN_ATTEMPT"') &&
                     source.include?('--workflow-ref "$GITHUB_WORKFLOW_REF"'),
                     "candidate producer is not isolated repository-tool execution")
    uploads = action_steps(producer, UPLOAD_ARTIFACT)
    expected_handoff_name =
      "#{handoff_prefix}-${{ inputs.work-id }}-${{ github.run_id }}-${{ github.run_attempt }}"
    require_contract(uploads.length == 1 && uploads.fetch(0).fetch("id") == "upload" &&
                     uploads.fetch(0).fetch("if") == "always()" &&
                     uploads.fetch(0).dig("with", "name") == expected_handoff_name &&
                     uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
                     uploads.fetch(0).dig("with", "compression-level") == 0,
                     "candidate handoff lacks exact protected upload outputs")

    writer_permissions = {"actions" => "read", "contents" => "read", "packages" => "write"}
    require_contract(publisher.fetch("permissions") == writer_permissions &&
                     publisher.fetch("timeout-minutes").between?(1, 30) &&
                     publisher.fetch("needs") == producer_id &&
                     publisher.fetch("if") == "always()",
                     "reusable publisher authority or dependency changed")
    require_protected_checkout(publisher, "reusable publisher")
    setup = action_steps(publisher, SETUP_PYTHON)
    require_contract(setup.length == 1 &&
                     setup.fetch(0).dig("with", "python-version") == "3.13",
                     "reusable publisher lacks declared Python")
    commands = run_steps(publisher)
    require_contract(commands.length == 1 &&
                     commands.fetch(0).fetch("working-directory") == "tap-authority",
                     "reusable publisher bypasses protected tap code")
    command = commands.fetch(0)
    expected_env = {
      "COORDINATION_ARTIFACT_DIGEST" => "${{ inputs.coordination-artifact-digest }}",
      "COORDINATION_ARTIFACT_ID" => "${{ inputs.coordination-artifact-id }}",
      "GITHUB_TOKEN" => "${{ github.token }}",
      "HANDOFF_ARTIFACT_DIGEST" => "${{ needs.#{producer_id}.outputs.artifact-digest }}",
      "HANDOFF_ARTIFACT_ID" => "${{ needs.#{producer_id}.outputs.artifact-id }}",
      "HOMEBREW_GITHUB_PACKAGES_TOKEN" => "${{ github.token }}",
      "HOMEBREW_GITHUB_PACKAGES_USER" => "${{ github.actor }}",
      "PRODUCER_CONCLUSION" => "${{ needs.#{producer_id}.result }}",
      "TAP_COMMIT" => "${{ inputs.tap-commit }}",
      "WORK_ID" => "${{ inputs.work-id }}"
    }
    require_contract(command.fetch("env") == expected_env,
                     "publisher is not bound to direct protected job outputs")
    source = command.fetch("run")
    required_flags = [
      '--coordination-artifact-id "$COORDINATION_ARTIFACT_ID"',
      '--coordination-artifact-digest "$COORDINATION_ARTIFACT_DIGEST"',
      '--producer-conclusion "$PRODUCER_CONCLUSION"',
      '--handoff-artifact-id "$HANDOFF_ARTIFACT_ID"',
      '--handoff-artifact-digest "$HANDOFF_ARTIFACT_DIGEST"',
      '--head-sha "$TAP_COMMIT"',
      "--require-github-digest",
      "--anonymous-readback",
      "--immutable"
    ]
    require_contract(source.include?("python3 -m scripts.abi_staging.cli #{publisher_cli}") &&
                     required_flags.all? { |flag| source.include?(flag) },
                     "publisher lacks direct artifact/result/readback identity")
    require_no_candidate_execution(source, "reusable publisher")
    uploads = action_steps(publisher, UPLOAD_ARTIFACT)
    require_contract(uploads.length == 1 &&
                     uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
                     uploads.fetch(0).dig("with", "compression-level") == 0,
                     "publisher result locator is not retained exactly")

    all_text = flatten(workflow).join("\n")
    require_contract(!all_text.match?(/\bsecrets\b/i) &&
                     !all_text.match?(/(?:^|\s)sleep(?:\s|$)/),
                     "reusable ABI workflow gains secrets or sleeps")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "reusable ABI workflow structure is incomplete: #{error.message}"
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
        [File.join(root, ".github/workflows/abi-staging-candidate.yml"), :candidate],
        [File.join(root, ".github/workflows/abi-staging-reuse.yml"), :check_reuse],
        [File.join(root, ".github/workflows/abi-staging-verification.yml"), :verification],
        [File.join(root, ".github/workflows/abi-staging-maintenance.yml"), :check_maintenance]
      ]
    else
      ARGV.map do |path|
        method = case File.basename(path)
        when "abi-staging-maintenance.yml" then :check_maintenance
        when "abi-staging-candidate.yml" then :candidate
        when "abi-staging-reuse.yml" then :check_reuse
        when "abi-staging-verification.yml" then :verification
        else :check
        end
        [path, method]
      end
    end
    paths.each do |path, method|
      workflow = YAML.safe_load(File.read(path), permitted_classes: [], aliases: false)
      if %i[candidate verification].include?(method)
        AbiStagingWorkflowCheck.check_reusable(workflow, method)
      else
        AbiStagingWorkflowCheck.public_send(method, workflow)
      end
    end
    puts "check_abi_staging_workflows: PASS"
  rescue Errno::ENOENT, Psych::Exception, AbiStagingWorkflowCheck::Violation => error
    warn "check_abi_staging_workflows: #{error.message}"
    exit 1
  end
end
