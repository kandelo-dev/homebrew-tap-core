#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

module AbiStagingWorkflowCheck
  CHECKOUT = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
  SETUP_PYTHON = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
  SETUP_ORAS = "oras-project/setup-oras@1d808f7d7f6995cc68b7bf507bfe5c5446e1dc9d"
  UPLOAD_ARTIFACT = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
  DOWNLOAD_ARTIFACT = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
  CACHE_RESTORE = "actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
  CACHE_SAVE = "actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
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

  def require_no_automattic_ghcr(value)
    text = flatten(value).join("\n")
    require_contract(!text.match?(%r{ghcr\.io/automattic/}i),
                     "workflow names an Automattic GHCR target")
  end

  def require_no_package_secret_references(value, field)
    references = flatten(value).join("\n").scan(/\$\{\{\s*secrets\.[^}]+\}\}/).uniq
    require_contract(references.empty?,
                     "#{field} references a repository secret")
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
    identity = action.include?("@") ? "#{action.split("@", 2).fetch(0)}@" : action
    job.fetch("steps").select { |step| step["uses"]&.start_with?(identity) }
  end

  def require_pinned_oras(job, field)
    steps = job.fetch("steps")
    setups = action_steps(job, SETUP_ORAS)
    commands = run_steps(job)
    require_contract(
      setups.length == 1 && setups.fetch(0).fetch("uses") == SETUP_ORAS &&
        setups.fetch(0).fetch("with", {}) == {"version" => "1.3.3"} &&
        !commands.empty? && steps.index(setups.fetch(0)) < steps.index(commands.fetch(0)),
      "#{field} lacks one pinned ORAS setup before publication"
    )
  end

  def check_public_discovery(source)
    broad_release = %r{https://api\.github\.com/repos/\{repository\}/releases(?:["']|\s|\?)}
    require_contract(!source.match?(broad_release) &&
                     !source.include?("releases/{release_id}/assets"),
                     "public discovery restored the broad Release inventory")
    require_contract(source.is_a?(String) &&
                     source.include?("git/matching-refs/tags/") &&
                     source.include?("releases/tags/") &&
                     source.include?("tags = self._request_release_tags()") &&
                     source.include?("release = self._release_by_tag(tag)"),
                     "public discovery must use bounded request tags and exact Releases")
    true
  end

  def require_no_candidate_execution(source, field)
    require_contract(!source.match?(/(?:^|\s)(?:eval|source|curl|wget|sleep)(?:\s|$)/) &&
                     !source.match?(/abi-staging-(?:build|verify)-bottle/) &&
                     !source.match?(/(?:build|run)-[^\s]*vfs[^\s]*\.(?:sh|ts)/) &&
                     !source.match?(/execute-product-(?:work|evidence-work)/) &&
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

  def require_candidate_checkouts(job, field, include_homebrew: false)
    checkouts = action_steps(job, CHECKOUT)
    expected_count = include_homebrew ? 3 : 2
    require_contract(checkouts.length == expected_count,
                     "#{field} must check out its exact protected sources")
    tap = checkouts.find { |step| step.dig("with", "path") == "tap-authority" }
    policy = checkouts.find { |step| step.dig("with", "path") == "kandelo-authority" }
    require_contract(!tap.nil? && tap.fetch("with") == {
                       "repository" => "kandelo-dev/homebrew-tap-core",
                       "ref" => "${{ inputs.tap-commit }}",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "#{field} tap checkout is not the exact protected revision")
    require_contract(!policy.nil? && policy.fetch("with") == {
                       "repository" => "${{ inputs.kandelo-repository }}",
                       "ref" => "${{ inputs.kandelo-policy-commit }}",
                       "fetch-depth" => 1,
                       "submodules" => "recursive",
                       "persist-credentials" => false,
                       "path" => "kandelo-authority"
                     }, "#{field} Kandelo policy checkout is not immutable")
    if include_homebrew
      homebrew = checkouts.find do |step|
        step.dig("with", "path") == "homebrew-prefix/Homebrew"
      end
      require_contract(!homebrew.nil? && homebrew.fetch("with") == {
                         "repository" => "Homebrew/brew",
                         "ref" => "a92554a538e81fad0c5074443885dbcc4c36221d",
                         "fetch-depth" => 1,
                         "persist-credentials" => false,
                         "path" => "homebrew-prefix/Homebrew"
                       }, "#{field} Homebrew checkout is not the reviewed revision")
    end
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
    require_no_automattic_ghcr(workflow)
    require_contract(workflow.fetch("permissions") == {},
                     "workflow permissions must be empty")
    event = triggers(workflow)
    require_contract(event.keys.sort == %w[schedule workflow_dispatch],
                     "workflow must have only schedule and workflow_dispatch triggers")
    require_contract(event.fetch("schedule") == [{"cron" => "*/5 * * * *"}],
                     "workflow schedule must remain five minutes")
    inputs = event.dig("workflow_dispatch", "inputs")
    input = inputs.fetch("request_asset_url", nil)
    require_contract(input.is_a?(Hash) && input["required"] == false &&
                       input["type"] == "string",
                     "manual workflow must accept one optional request_asset_url")
    retry_input = inputs.fetch("retry_exhausted_builds", nil)
    require_contract(retry_input.is_a?(Hash) && retry_input["required"] == false &&
                       retry_input["default"] == false && retry_input["type"] == "boolean",
                     "manual exhausted-build retry must be an explicit false-by-default boolean")
    require_contract(inputs.keys ==
                       %w[request_asset_url retry_exhausted_builds],
                     "manual workflow gained another coordinator input")

    jobs = workflow.fetch("jobs")
    expected_jobs = %w[
      discover-plan prepare-homebrew-realm candidate verification reuse prepare-runtime
      plan-products compose-product publish-product-candidate node-product-evidence
      browser-product-evidence publish-product-evidence plan-promotion
      publish-canonical update-tap-metadata publish-admission
    ]
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
    require_contract(discover.fetch("permissions") == {
                       "contents" => "read", "packages" => "read"
                     }, "discovery must remain contents and packages read only")
    require_contract(discover.fetch("timeout-minutes").between?(1, 30),
                     "discovery timeout is not bounded")
    require_contract(
      discover.dig("outputs", "required-formulae-ready") ==
        "${{ steps.coordinate.outputs.required_formulae_ready || steps.discover.outputs.required_formulae_ready }}",
      "discovery required Formula readiness output changed"
    )
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
                       requirements_source.include?("scripts/dev-shell.sh bash -c '") &&
                       requirements_source.include?(
                         'jq -cS ".requirements.change_classes" "$1" >"$2"'
                       ) &&
                       requirements_source.include?(
                         %q(' -- "$out/request.json" "$classes")
                       ) &&
                       !requirements_source.include?(
                         %q(scripts/dev-shell.sh jq -cS '.requirements.change_classes')
                       ) &&
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
                       coordinator.fetch("env") == {
                         "HOMEBREW_GITHUB_PACKAGES_USER" => "${{ github.actor }}",
                         "HOMEBREW_GITHUB_PACKAGES_TOKEN" => "${{ github.token }}"
                       } &&
                       coordinator_source.include?("python3 -m scripts.abi_staging.cli prepare-workflow") &&
                       coordinator_source.include?('if [[ "${{ inputs.retry_exhausted_builds }}" == "true" ]]') &&
                       coordinator_source.include?('retry_args+=(--retry-exhausted-builds)') &&
                       coordinator_source.include?('"${retry_args[@]}"') &&
                       coordinator_source.include?('--kandelo-root "$GITHUB_WORKSPACE/kandelo-source"') &&
                       coordinator_source.include?('--discovery "$out/discovery.json"') &&
                       coordinator_source.include?('--formula-requirements "$RUNNER_TEMP/abi-staging-formula-requirements.json"'),
                     "coordination does not bind exact-head request requirements")
    require_contract(
      commands.reject { |step| step.equal?(coordinator) }.none? do |step|
        step.fetch("env", {}).key?("HOMEBREW_GITHUB_PACKAGES_TOKEN")
      end,
      "package read token escaped the protected inventory coordinator"
    )
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

    realm = jobs.fetch("prepare-homebrew-realm")
    realm_steps = realm.fetch("steps")
    realm_source = run_steps(realm).map { |step| step.fetch("run") }.join("\n")
    realm_uploads = action_steps(realm, UPLOAD_ARTIFACT)
    realm_restores = action_steps(realm, CACHE_RESTORE)
    realm_saves = action_steps(realm, CACHE_SAVE)
    realm_key = run_steps(realm).find do |step|
      step["id"] == "realm-cache-key"
    end
    realm_restore = realm_restores.find do |step|
      step["id"] == "restore-realm-cache"
    end
    realm_legacy_restore = realm_restores.find do |step|
      step["id"] == "restore-legacy-realm-cache"
    end
    realm_save = realm_saves.length == 1 ? realm_saves.fetch(0) : nil
    realm_setup = realm_steps.find do |step|
      step["uses"] == "./kandelo-source/.github/actions/setup-nix"
    end
    realm_prepare = run_steps(realm).find do |step|
      step.fetch("run").include?("abi-staging-prepare-shared-homebrew-realm.sh")
    end
    realm_pack = run_steps(realm).find do |step|
      step.fetch("run").include?("abi-staging-pack-homebrew-realm.sh")
    end
    realm_identity = realm_steps.find { |step| step["id"] == "realm-identity" }
    realm_upload = realm_steps.find { |step| step["id"] == "upload-realm" }
    realm_cache_path = "${{ runner.temp }}/shared-homebrew-realm.tar.zst"
    realm_cache_key = "${{ steps.realm-cache-key.outputs.cache_key }}"
    realm_legacy_cache_key =
      "${{ steps.realm-cache-key.outputs.legacy_cache_key }}"
    realm_primary_cache_miss =
      "steps.restore-realm-cache.outputs.cache-hit != 'true'"
    realm_legacy_restore_guard = realm_primary_cache_miss +
      " && steps.realm-cache-key.outputs.legacy_cache_key != ''"
    realm_cache_miss = realm_primary_cache_miss +
      " && steps.restore-legacy-realm-cache.outputs.cache-hit != 'true'"
    realm_consumer_guard =
      "needs.discover-plan.outputs.mode == 'active' && " \
      "(fromJSON(needs.discover-plan.outputs.build-matrix).include[0] != null || " \
      "fromJSON(needs.discover-plan.outputs.verify-matrix).include[0] != null)"
    realm_cache_contract =
      realm_restores.length == 2 && !realm_key.nil? &&
      realm_key.fetch("env", {}) == {
        "KANDELO_COMMIT" => "${{ needs.discover-plan.outputs.kandelo-head }}",
        "RUNNER_OS_NAME" => "${{ runner.os }}",
        "RUNNER_ARCH_NAME" => "${{ runner.arch }}"
      } && realm_key.fetch("run", "").include?(
        "abi-staging-homebrew-realm-cache-key.sh"
      ) && !realm_restore.nil? &&
      realm_restore.fetch("uses") == CACHE_RESTORE &&
      realm_restore.fetch("with", {}) == {
        "path" => realm_cache_path,
        "key" => realm_cache_key
      } && !realm_legacy_restore.nil? &&
      realm_legacy_restore.fetch("uses") == CACHE_RESTORE &&
      realm_legacy_restore.fetch("if", nil) == realm_legacy_restore_guard &&
      realm_legacy_restore.fetch("with", {}) == {
        "path" => realm_cache_path,
        "key" => realm_legacy_cache_key
      } && !realm_save.nil? && realm_save.fetch("id", nil) == "save-realm-cache" &&
      realm_save.fetch("uses") == CACHE_SAVE &&
      realm_save.fetch("if", nil) == realm_primary_cache_miss &&
      realm_save.fetch("with", {}) == {
        "path" => realm_cache_path,
        "key" => realm_cache_key
      } && !realm_setup.nil? && realm_setup.fetch("if", nil) == realm_cache_miss &&
      !realm_prepare.nil? && realm_prepare.fetch("if", nil) == realm_cache_miss &&
      !realm_pack.nil? && realm_pack.fetch("if", nil) == realm_cache_miss &&
      !realm_identity.nil? && !realm_identity.key?("if") &&
      realm_identity.fetch("run", "").include?('[[ -f "$archive" && ! -L "$archive" ]]') &&
      !realm_upload.nil? && !realm_upload.key?("if") &&
      realm_steps.index(realm_key) < realm_steps.index(realm_restore) &&
      realm_steps.index(realm_restore) < realm_steps.index(realm_legacy_restore) &&
      realm_steps.index(realm_legacy_restore) < realm_steps.index(realm_setup) &&
      realm_steps.index(realm_setup) < realm_steps.index(realm_prepare) &&
      realm_steps.index(realm_prepare) < realm_steps.index(realm_pack) &&
      realm_steps.index(realm_pack) < realm_steps.index(realm_identity) &&
      realm_steps.index(realm_identity) < realm_steps.index(realm_save) &&
      realm_steps.index(realm_save) < realm_steps.index(realm_upload)
    require_contract(
      realm.fetch("needs") == "discover-plan" &&
        realm.fetch("if") == realm_consumer_guard &&
        realm.fetch("runs-on") == "ubuntu-latest" &&
        realm.fetch("timeout-minutes").between?(1, 180) &&
        realm.fetch("permissions") == {"contents" => "read"} &&
        !realm.key?("environment") && !realm.key?("secrets") &&
        realm.fetch("outputs") == {
          "artifact-id" => "${{ steps.upload-realm.outputs.artifact-id }}",
          "artifact-digest" => "${{ steps.upload-realm.outputs.artifact-digest }}",
          "archive-sha256" => "${{ steps.realm-identity.outputs.archive_sha256 }}",
          "source-tree" => "${{ steps.realm-identity.outputs.source_tree }}"
        } && realm_source.include?("abi-staging-prepare-shared-homebrew-realm.sh") &&
        realm_source.include?("abi-staging-pack-homebrew-realm.sh") &&
        realm_source.include?("abi-staging-homebrew-realm-cache-key.sh") &&
        realm_source.include?("env -u GITHUB_TOKEN") &&
        realm_source.include?("-u ACTIONS_RUNTIME_TOKEN") &&
        realm_cache_contract &&
        realm_uploads.length == 1 &&
        realm_uploads.fetch(0).fetch("id") == "upload-realm" &&
        realm_uploads.fetch(0).dig("with", "compression-level") == 0 &&
        realm_uploads.fetch(0).dig("with", "retention-days") == 1,
      "shared Homebrew realm producer changed"
    )
    check_actions(realm)

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
      "realm-artifact-id" => "${{ needs.prepare-homebrew-realm.outputs.artifact-id }}",
      "realm-artifact-digest" => "${{ needs.prepare-homebrew-realm.outputs.artifact-digest }}",
      "realm-archive-sha256" => "${{ needs.prepare-homebrew-realm.outputs.archive-sha256 }}",
      "realm-source-tree" => "${{ needs.prepare-homebrew-realm.outputs['source-tree'] }}",
      "work-id" => "${{ matrix.work_id }}"
    }
    [[candidate, "./.github/workflows/abi-staging-candidate.yml"],
     [verification, "./.github/workflows/abi-staging-verification.yml"]].each do |job, reusable|
      require_contract(job.fetch("needs") == %w[discover-plan prepare-homebrew-realm] &&
                       job.fetch("if") == "needs.discover-plan.outputs.mode == 'active' && needs.prepare-homebrew-realm.result == 'success'" &&
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

    planner = jobs.fetch("plan-products")
    require_contract(
      planner.fetch("needs") ==
        %w[discover-plan candidate verification reuse prepare-runtime],
      "product planner must wait for all Formula public facts"
    )
    require_contract(
      planner.fetch("if") ==
        "always() && needs.discover-plan.outputs.selected == 'true' && needs.prepare-runtime.result == 'success'",
      "product planner is not exact-request and exact-runtime scoped"
    )
    require_contract(
      planner.fetch("runs-on") == "ubuntu-latest" &&
        planner.fetch("timeout-minutes") == 30 &&
        planner.fetch("permissions") == {"contents" => "read"} &&
        !planner.key?("environment") && !planner.key?("secrets") &&
        planner.fetch("steps").none? do |step|
          step["continue-on-error"] == true
        end,
      "product planner must remain contents-read only"
    )
    check_actions(planner)
    require_contract(
      planner.fetch("outputs") == {
        "product-matrix" => "${{ steps.plan.outputs.product_matrix }}",
        "node-evidence-matrix" => "${{ steps.plan.outputs.node_evidence_matrix }}",
        "browser-evidence-matrix" => "${{ steps.plan.outputs.browser_evidence_matrix }}"
      },
      "product planner outputs are not the exact protected wave"
    )
    planner_checkouts = action_steps(planner, CHECKOUT)
    require_contract(
      planner_checkouts.length == 2 &&
        planner_checkouts.any? do |step|
          step.fetch("with") == {
            "ref" => "${{ needs.discover-plan.outputs.tap-commit }}",
            "fetch-depth" => 1,
            "persist-credentials" => false,
            "path" => "tap-authority"
          }
        end && planner_checkouts.any? do |step|
          step.fetch("with") == {
            "repository" => "${{ needs.discover-plan.outputs.kandelo-repository }}",
            "ref" => "${{ needs.discover-plan.outputs.kandelo-head }}",
            "fetch-depth" => 1,
            "persist-credentials" => false,
            "path" => "kandelo-source"
          }
        end,
      "product planner does not use exact protected and inert source checkouts"
    )
    planner_setup = action_steps(planner, SETUP_PYTHON)
    require_contract(
      planner_setup.length == 1 &&
        planner_setup.fetch(0).dig("with", "python-version") == "3.13",
      "product planner lacks declared Python"
    )
    planner_steps = run_steps(planner)
    planner_source = planner_steps.map { |step| step.fetch("run") }.join("\n")
    require_contract(
      planner_steps.length == 1 && planner_steps.fetch(0).fetch("id") == "plan" &&
        planner_steps.fetch(0).fetch("working-directory") == "tap-authority" &&
        planner_source.include?("python3 -m scripts.abi_staging.cli plan-workflow-products") &&
        planner_source.include?('--coordination-root "$RUNNER_TEMP/product-planning/coordination"') &&
        planner_source.include?('--runtime-root "$RUNNER_TEMP/product-planning/runtime"') &&
        planner_source.include?('--kandelo-root "$GITHUB_WORKSPACE/kandelo-source"') &&
        planner_source.include?('--tap-root "$PWD"') &&
        planner_source.include?('--github-output "$GITHUB_OUTPUT"'),
      "product planner does not derive the protected wave"
    )
    require_no_candidate_execution(planner_source, "product planner")
    planner_uploads = action_steps(planner, UPLOAD_ARTIFACT)
    require_contract(
      planner_uploads.length == 1 &&
        planner_uploads.fetch(0).dig("with", "name") ==
          "abi-staging-product-wave-${{ needs.discover-plan.outputs.request-digest }}-${{ github.run_id }}-${{ github.run_attempt }}" &&
        planner_uploads.fetch(0).dig("with", "path") ==
          "${{ runner.temp }}/product-wave" &&
        planner_uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
        planner_uploads.fetch(0).dig("with", "compression-level") == 0,
      "product planner does not preserve its exact protected wave"
    )

    product_permissions = {
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
    product_permissions.each do |name, permissions|
      job = jobs.fetch(name)
      require_contract(job.fetch("permissions") == permissions &&
                       !job.key?("environment") && !job.key?("secrets") &&
                       job.fetch("steps").none? do |step|
                         step["continue-on-error"] == true
                       end, "#{name} changed its exact capability boundary")
      check_actions(job)
    end

    %w[prepare-runtime compose-product node-product-evidence browser-product-evidence].each do |name|
      require_contract(jobs.fetch(name).fetch("timeout-minutes") == 180,
                       "#{name} must retain the exact three-hour product bound")
    end
    %w[publish-product-candidate publish-product-evidence].each do |name|
      require_contract(jobs.fetch(name).fetch("timeout-minutes").between?(1, 30),
                       "#{name} publisher timeout is not bounded")
    end

    product_matrix = "${{ fromJSON(needs.plan-products.outputs.product-matrix) }}"
    node_matrix = "${{ fromJSON(needs.plan-products.outputs.node-evidence-matrix) }}"
    browser_matrix = "${{ fromJSON(needs.plan-products.outputs.browser-evidence-matrix) }}"
    product_matrix_jobs = %w[
      compose-product publish-product-candidate publish-product-evidence
    ]
    require_contract(
      product_matrix_jobs.all? do |name|
        jobs.dig(name, "strategy", "matrix") == product_matrix
      end && jobs.dig("node-product-evidence", "strategy", "matrix") == node_matrix &&
        jobs.dig("browser-product-evidence", "strategy", "matrix") == browser_matrix,
      "product matrices must come from the protected wave"
    )
    require_matrix(jobs.fetch("compose-product"), product_matrix, "compose-product")
    require_matrix(
      jobs.fetch("publish-product-candidate"), product_matrix,
      "publish-product-candidate"
    )
    require_matrix(jobs.fetch("node-product-evidence"), node_matrix,
                   "node-product-evidence")
    require_matrix(jobs.fetch("browser-product-evidence"), browser_matrix,
                   "browser-product-evidence")
    require_matrix(jobs.fetch("publish-product-evidence"), product_matrix,
                   "publish-product-evidence")

    prepare = jobs.fetch("prepare-runtime")
    runtime_consumer_guard =
      "needs.discover-plan.outputs.selected == 'true' && " \
      "needs.discover-plan.outputs.required-formulae-ready == 'true' && " \
      "(fromJSON(needs.discover-plan.outputs.product-matrix).include[0] != null || " \
      "fromJSON(needs.discover-plan.outputs.node-evidence-matrix).include[0] != null || " \
      "fromJSON(needs.discover-plan.outputs.browser-evidence-matrix).include[0] != null)"
    require_contract(prepare.fetch("needs") == "discover-plan" &&
                     prepare.fetch("if") == runtime_consumer_guard,
                     "runtime preparation is not consumer scoped")
    prepare_job_steps = prepare.fetch("steps")
    prepare_checkouts = action_steps(prepare, CHECKOUT)
    prepare_taps = prepare_checkouts.select do |step|
      step.dig("with", "path") == "tap-authority"
    end
    prepare_tap = prepare_taps.fetch(0, nil)
    prepare_runtime_uploads = action_steps(prepare, UPLOAD_ARTIFACT).select do |step|
      step["name"] == "Upload exact inert runtime"
    end
    require_contract(
      prepare_checkouts.length == 3 && prepare_taps.length == 1 &&
        !prepare_tap.nil? && prepare_tap.fetch("uses") == CHECKOUT &&
        prepare_tap.fetch("with") == {
          "ref" => "${{ needs.discover-plan.outputs.tap-commit }}",
          "fetch-depth" => 1,
          "persist-credentials" => false,
          "path" => "tap-authority"
        },
      "runtime lacks its exact protected tap checkout"
    )
    require_contract(
      prepare_runtime_uploads.length == 1 &&
        prepare_runtime_uploads.fetch(0).dig("with", "include-hidden-files") == true,
      "runtime upload does not preserve its exact inventory"
    )
    prepare_steps = run_steps(prepare)
    export_index = prepare_steps.index do |step|
      step["name"] == "Export exact protected runtime identity"
    end
    isolate_index = prepare_steps.index do |step|
      step["name"] == "Isolate exact runtime package resolution"
    end
    build_index = prepare_steps.index do |step|
      step["name"] == "Build one exact uncredentialed runtime"
    end
    require_contract(
      export_index && build_index && export_index < build_index,
      "runtime identity is not exported before the exact build"
    )
    export_step = prepare_steps.fetch(export_index)
    require_contract(
      prepare_job_steps.index(prepare_tap) < prepare_job_steps.index(export_step),
      "runtime protected tap checkout occurs after identity export"
    )
    export_source = export_step.fetch("run")
    require_contract(
      export_step.fetch("working-directory") == "kandelo-authority" &&
        export_source.include?("scripts/dev-shell.sh env") &&
        export_source.include?("export-runtime-realm") &&
        export_source.include?('--coordination "$RUNNER_TEMP/coordination"') &&
        export_source.include?('--tap-root "$GITHUB_WORKSPACE/tap-authority"') &&
        export_source.include?('--github-env "$GITHUB_ENV"'),
      "runtime identity export bypasses protected coordination"
    )
    require_contract(
      isolate_index && build_index && isolate_index < build_index,
      "runtime package cache is not sealed before the exact build"
    )
    isolate_source = prepare_steps.fetch(isolate_index).fetch("run")
    build_source = prepare_steps.fetch(build_index).fetch("run")
    require_contract(
      isolate_source.include?("abi-staging-runtime-package-cache") &&
        isolate_source.include?("test ! -e") &&
        isolate_source.include?("mkdir -m 0700") &&
        isolate_source.include?("WASM_POSIX_BINARY_CACHE_ROOT=") &&
        isolate_source.include?('>>"$GITHUB_ENV"'),
      "runtime package cache is not one fresh isolated directory"
    )
    require_contract(
      build_source.include?(
        'WASM_POSIX_BINARY_CACHE_ROOT=$WASM_POSIX_BINARY_CACHE_ROOT'
      ) && build_source.include?(
        '--binary-cache-root "$WASM_POSIX_BINARY_CACHE_ROOT"'
      ),
      "runtime package cache is not exact across the protected build"
    )
    require_contract(
      build_source.include?(
        'scripts/dev-shell.sh npm --prefix apps/browser-demos install --prefer-offline'
      ),
      "runtime protected browser bundler is not installed"
    )
    prepare_source = prepare_steps.map { |step| step.fetch("run") }.join("\n")
    require_contract(prepare_source.include?("abi-staging-prepare-runtime.sh") &&
                     prepare_source.include?('--source-commit "${{ needs.discover-plan.outputs.kandelo-head }}"') &&
                     build_source.include?('"$KANDELO_ABI_STAGING_BUILD_POLICY_SHA256"') &&
                     build_source.include?('"$KANDELO_ABI_STAGING_SNAPSHOT_SHA256"') &&
                     build_source.include?('"$KANDELO_ABI_STAGING_SOURCE_TREE"') &&
                     build_source.include?('"$KANDELO_ABI_STAGING_TARGET_ABI"') &&
                     !build_source.include?('$(scripts/dev-shell.sh jq') &&
                     !prepare_source.include?(".requirements.digest") &&
                     prepare_source.include?("env -u GITHUB_TOKEN") &&
                     prepare_source.include?("-u ACTIONS_RUNTIME_TOKEN"),
                     "runtime preparation is not the uncredentialed exact-head adapter")

    compose = jobs.fetch("compose-product")
    require_contract(compose.fetch("needs") ==
                       %w[discover-plan prepare-runtime plan-products] &&
                     compose.fetch("if") ==
                       "always() && needs.plan-products.result == 'success' && needs.discover-plan.outputs.selected == 'true'",
                     "product composition gates on a global Formula outcome")
    compose_source = run_steps(compose).map { |step| step.fetch("run") }.join("\n")
    compose_executor = run_steps(compose).find do |step|
      step.fetch("run").include?("execute-product-work")
    end
    require_contract(compose_executor&.fetch("working-directory") ==
                       "kandelo-authority" &&
                     compose_source.include?("scripts/dev-shell.sh env") &&
                     compose_source.include?(
                       'PYTHONPATH=$GITHUB_WORKSPACE/tap-authority'
                     ) && compose_source.include?("execute-product-work") &&
                     compose_source.include?("--validate-builder-report") &&
                     compose_source.include?('--private-out "$RUNNER_TEMP/product-private"') &&
                     compose_source.include?("env -u GITHUB_TOKEN") &&
                     compose_source.include?("-u ACTIONS_RUNTIME_TOKEN"),
                     "product composition uncredentialed executor does not enter immutable Kandelo dev shell or lacks the protected report boundary")
    compose_uploads = action_steps(compose, UPLOAD_ARTIFACT)
    require_contract(compose_uploads.length == 2 &&
                     compose_uploads.any? do |step|
                       step.dig("with", "name") ==
                         "abi-staging-product-private-${{ matrix.product_id }}-${{ matrix.work_id }}-${{ github.run_id }}-${{ github.run_attempt }}" &&
                         step.dig("with", "path") ==
                           "${{ runner.temp }}/product-private" &&
                         step.dig("with", "if-no-files-found") == "error"
                     end,
                     "product composition omits its exact private authority artifact")

    candidate_publisher = jobs.fetch("publish-product-candidate")
    require_pinned_oras(candidate_publisher, "candidate product publisher")
    require_contract(candidate_publisher.fetch("needs") ==
                       %w[discover-plan plan-products compose-product] &&
                     candidate_publisher.fetch("if") ==
                       "always() && needs.plan-products.result == 'success' && needs.discover-plan.outputs.selected == 'true'",
                     "candidate product publisher is not independently resumable")
    candidate_publish_source = run_steps(candidate_publisher)
      .map { |step| step.fetch("run") }.join("\n")
    require_contract(
      candidate_publish_source.include?("publish-workflow-product-candidate") &&
      candidate_publish_source.include?("--validate-builder-report") &&
      candidate_publish_source.include?("--private-artifact-name") &&
      candidate_publish_source.include?('--kandelo-root "$GITHUB_WORKSPACE/kandelo-source"') &&
      candidate_publish_source.include?('--kandelo-policy-root "$GITHUB_WORKSPACE/kandelo-authority"') &&
      candidate_publish_source.include?("--require-github-digest") &&
      candidate_publish_source.include?("--anonymous-readback") &&
      candidate_publish_source.include?("--immutable"),
      "candidate product publisher lacks exact inert-data validation"
    )
    publisher_checkouts = action_steps(candidate_publisher, CHECKOUT)
    require_contract(publisher_checkouts.length == 3 &&
                     publisher_checkouts.any? do |step|
                       step.dig("with", "path") == "kandelo-source" &&
                         step.dig("with", "ref") ==
                           "${{ needs.discover-plan.outputs.kandelo-head }}" &&
                         step.dig("with", "persist-credentials") == false
                     end && publisher_checkouts.any? do |step|
                       step.dig("with", "path") == "kandelo-authority" &&
                         step.dig("with", "ref") ==
                           "${{ needs.discover-plan.outputs.kandelo-policy-commit }}" &&
                         step.dig("with", "persist-credentials") == false
                     end,
                     "candidate product publisher lacks exact inert source authority")
    require_no_candidate_execution(candidate_publish_source,
                                   "candidate product publisher")

    {
      "node-product-evidence" => "node",
      "browser-product-evidence" => "browser"
    }.each do |name, host|
      job = jobs.fetch(name)
      require_contract(job.fetch("needs") ==
                         %w[discover-plan plan-products prepare-runtime publish-product-candidate] &&
                       job.fetch("if") ==
                         "always() && needs.plan-products.result == 'success' && needs.discover-plan.outputs.selected == 'true'",
                       "#{name} must gate on the protected product wave")
      source_text = run_steps(job).map { |step| step.fetch("run") }.join("\n")
      executor = run_steps(job).find do |step|
        step.fetch("run").include?("execute-product-evidence-work")
      end
      require_contract(executor&.fetch("working-directory") ==
                         "kandelo-authority" &&
                       source_text.include?("scripts/dev-shell.sh env") &&
                       source_text.include?(
                         'PYTHONPATH=$GITHUB_WORKSPACE/tap-authority'
                       ) && source_text.include?("execute-product-evidence-work") &&
                       source_text.include?("--host #{host}") &&
                       source_text.include?("env -u GITHUB_TOKEN") &&
                       source_text.include?("-u ACTIONS_RUNTIME_TOKEN"),
                       "#{name} uncredentialed executor does not enter immutable Kandelo dev shell")
    end

    evidence_publisher = jobs.fetch("publish-product-evidence")
    require_pinned_oras(evidence_publisher, "product evidence publisher")
    require_contract(evidence_publisher.fetch("needs") ==
                       %w[discover-plan plan-products node-product-evidence browser-product-evidence] &&
                     evidence_publisher.fetch("if") ==
                       "always() && needs.plan-products.result == 'success' && needs.discover-plan.outputs.selected == 'true'",
                     "product evidence publisher is not sibling-independent")
    evidence_publish_steps = run_steps(evidence_publisher)
    evidence_publish_source = evidence_publish_steps
      .map { |step| step.fetch("run") }.join("\n")
    evidence_publish_step = evidence_publish_steps.find do |step|
      step.fetch("run").include?("publish-workflow-product-evidence")
    end
    evidence_publish_env = evidence_publish_step&.fetch("env", {})
    require_contract(
      evidence_publish_source.include?("publish-workflow-product-evidence") &&
      evidence_publish_source.include?('--product-work-id "$PRODUCT_WORK_ID"') &&
      evidence_publish_source.include?("--require-terminal-results") &&
      evidence_publish_source.include?("--require-github-digest") &&
      evidence_publish_source.include?("--anonymous-readback") &&
      evidence_publish_source.include?("--immutable") &&
      evidence_publish_env["PRODUCT_WORK_ID"] == "${{ matrix.work_id }}" &&
      evidence_publish_env["WORK_ID"] == "${{ matrix.publication_work_id }}" &&
      !evidence_publish_source.match?(/PRODUCER_CONCLUSION\s*=\s*skipped/),
      "product evidence publisher accepts incomplete or skipped evidence"
    )
    require_no_candidate_execution(evidence_publish_source,
                                   "product evidence publisher")

    promotion_planner = jobs.fetch("plan-promotion")
    require_contract(
      promotion_planner.fetch("needs") == %w[
        discover-plan candidate verification reuse
      ] && promotion_planner.fetch("if") ==
        "always() && needs.discover-plan.outputs.selected == 'true' && needs.discover-plan.outputs.promotion-eligible == 'true'" &&
        promotion_planner.fetch("runs-on") == "ubuntu-latest" &&
        promotion_planner.fetch("timeout-minutes") == 30 &&
        promotion_planner.fetch("permissions") == {"contents" => "read"} &&
        !promotion_planner.key?("environment") &&
        !promotion_planner.key?("secrets"),
      "promotion planner is not exact-merge and read-only scoped"
    )
    require_contract(
      promotion_planner.fetch("outputs") == {
        "canonical-matrix" => "${{ steps.plan.outputs.canonical_matrix }}",
        "metadata-matrix" => "${{ steps.plan.outputs.metadata_matrix }}",
        "admission-matrix" => "${{ steps.plan.outputs.admission_matrix }}",
        "artifact-id" => "${{ steps.upload.outputs.artifact-id }}",
        "artifact-digest" => "${{ steps.upload.outputs.artifact-digest }}"
      },
      "promotion planner outputs are not exact protected artifacts"
    )
    check_actions(promotion_planner)
    planner_checkouts = action_steps(promotion_planner, CHECKOUT)
    planner_tap_checkout = planner_checkouts.find do |step|
      step.dig("with", "path") == "tap-authority"
    end
    planner_kandelo_checkout = planner_checkouts.find do |step|
      step.dig("with", "path") == "kandelo-source"
    end
    require_contract(
      planner_checkouts.length == 2 && planner_tap_checkout&.fetch("with") == {
        "ref" => "${{ needs.discover-plan.outputs.tap-commit }}",
        "fetch-depth" => 0,
        "persist-credentials" => false,
        "path" => "tap-authority"
      } && planner_kandelo_checkout&.fetch("with") == {
        "repository" => "${{ needs.discover-plan.outputs.kandelo-repository }}",
        "ref" => "${{ needs.discover-plan.outputs.kandelo-head }}",
        "fetch-depth" => 1,
        "persist-credentials" => false,
        "path" => "kandelo-source"
      },
      "promotion planner lacks exact protected code or inert exact-head input"
    )
    planner_commands = run_steps(promotion_planner)
    planner_source = planner_commands.map { |step| step.fetch("run") }.join("\n")
    require_contract(
      planner_commands.length == 1 &&
        planner_commands.fetch(0).fetch("working-directory") == "tap-authority" &&
        planner_source.include?("plan-workflow-promotion") &&
        planner_source.include?('--coordination-root "$RUNNER_TEMP/promotion-inputs/coordination"') &&
        planner_source.include?('--kandelo-root "$GITHUB_WORKSPACE/kandelo-source"') &&
        planner_source.include?("--require-merged") &&
        planner_source.include?("--require-history-record") &&
        planner_source.include?('--github-output "$GITHUB_OUTPUT"'),
      "promotion planner does not require exact merged/history facts"
    )
    require_no_candidate_execution(planner_source, "promotion planner")
    planner_downloads = action_steps(promotion_planner, DOWNLOAD_ARTIFACT)
    require_contract(
      planner_downloads.length == 1 &&
        planner_downloads.fetch(0).dig("with", "artifact-ids") ==
          "${{ needs.discover-plan.outputs.coordination-artifact-id }}" &&
        planner_downloads.fetch(0).dig("with", "merge-multiple") == true,
      "promotion planner does not consume exact protected coordination"
    )
    planner_uploads = action_steps(promotion_planner, UPLOAD_ARTIFACT)
    require_contract(
      planner_uploads.length == 1 &&
        planner_uploads.fetch(0).fetch("id") == "upload" &&
        planner_uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
        planner_uploads.fetch(0).dig("with", "compression-level") == 0,
      "promotion plan artifact is not exact and retained"
    )

    promotion_permissions = {
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
    promotion_permissions.each do |name, permissions|
      job = jobs.fetch(name)
      require_contract(
        job.fetch("runs-on") == "ubuntu-latest" &&
          job.fetch("timeout-minutes").between?(1, 30) &&
          job.fetch("permissions") == permissions &&
          !job.key?("environment") && !job.key?("secrets") &&
          job.fetch("steps").none? { |step| step["continue-on-error"] == true },
        "#{name} changed its exact promotion authority"
      )
      check_actions(job)
      strategy = job.fetch("strategy")
      require_contract(
        strategy.fetch("fail-fast") == false &&
          strategy.fetch("max-parallel").between?(1, 16),
        "#{name} matrix must remain bounded and sibling-independent"
      )
    end
    require_contract(
      jobs.dig("publish-canonical", "strategy", "matrix") ==
        "${{ fromJSON(needs.plan-promotion.outputs.canonical-matrix) }}" &&
        jobs.dig("update-tap-metadata", "strategy", "matrix") ==
          "${{ fromJSON(needs.plan-promotion.outputs.metadata-matrix) }}" &&
        jobs.dig("publish-admission", "strategy", "matrix") ==
          "${{ fromJSON(needs.plan-promotion.outputs.admission-matrix) }}",
      "promotion matrices do not come from the exact protected plan"
    )

    canonical_publisher = jobs.fetch("publish-canonical")
    require_pinned_oras(canonical_publisher, "canonical publisher")
    require_contract(
      canonical_publisher.fetch("needs") == %w[discover-plan plan-promotion] &&
        canonical_publisher.fetch("if") ==
          "always() && needs.plan-promotion.result == 'success'",
      "canonical publisher may run before the protected promotion plan"
    )
    canonical_source = run_steps(canonical_publisher)
      .map { |step| step.fetch("run") }.join("\n")
    require_contract(
      canonical_source.include?("publish-workflow-canonical") &&
        canonical_source.include?("--require-unchanged-layer") &&
        canonical_source.include?("--require-history-barrier") &&
        canonical_source.include?("--require-github-digest") &&
        canonical_source.include?("--anonymous-readback") &&
        canonical_source.include?("--immutable") &&
        canonical_source.include?("--request-digest") &&
        canonical_source.include?("--plan-artifact-id") &&
        canonical_source.include?("--plan-artifact-digest"),
      "canonical publisher can rewrite bytes or lose exact plan identity"
    )
    require_no_candidate_execution(canonical_source, "canonical publisher")

    metadata_writer = jobs.fetch("update-tap-metadata")
    require_contract(
      metadata_writer.fetch("needs") ==
        %w[discover-plan plan-promotion publish-canonical] &&
        metadata_writer.fetch("if") ==
          "always() && needs.plan-promotion.result == 'success'" &&
        metadata_writer.dig("strategy", "max-parallel") == 1,
      "metadata writer is globally gated or can race another Formula CAS"
    )
    metadata_source = run_steps(metadata_writer)
      .map { |step| step.fetch("run") }.join("\n")
    require_contract(
      metadata_source.include?("update-workflow-tap-metadata") &&
        metadata_source.include?("--contents-only") &&
        metadata_source.include?("--require-history-barrier") &&
        metadata_source.include?("--normal-push") &&
        metadata_source.include?("--post-write-readback") &&
        metadata_source.include?("--request-digest") &&
        metadata_source.include?('--operation "$OPERATION"') &&
        run_steps(metadata_writer).fetch(0).dig("env", "OPERATION") ==
          "${{ matrix.operation }}" &&
        metadata_source.include?("--plan-artifact-id") &&
        metadata_source.include?("--plan-artifact-digest") &&
        !metadata_source.match?(/git\s+push[^\n]*(?:--force|-f\b)/),
      "metadata writer bypasses contents-only CAS or force-pushes"
    )
    require_no_candidate_execution(metadata_source, "metadata writer")

    admission_publisher = jobs.fetch("publish-admission")
    require_pinned_oras(admission_publisher, "admission publisher")
    require_contract(
      admission_publisher.fetch("needs") == %w[
        discover-plan plan-promotion publish-canonical update-tap-metadata
      ] && admission_publisher.fetch("if") ==
        "always() && needs.plan-promotion.result == 'success'",
      "admission publisher may run before metadata/readback"
    )
    admission_source = run_steps(admission_publisher)
      .map { |step| step.fetch("run") }.join("\n")
    admission_metadata_checkout = action_steps(admission_publisher, CHECKOUT)
      .find { |step| step.dig("with", "path") == "tap-metadata" }
    require_contract(
      admission_source.include?("publish-workflow-admission") &&
        admission_metadata_checkout&.fetch("with") == {
          "ref" => "main",
          "fetch-depth" => 0,
          "persist-credentials" => false,
          "path" => "tap-metadata"
        } &&
        admission_source.include?('--metadata-root "$GITHUB_WORKSPACE/tap-metadata"') &&
        admission_source.include?("--require-metadata-readback") &&
        admission_source.include?("--require-history-barrier") &&
        admission_source.include?("--require-github-digest") &&
        admission_source.include?("--anonymous-readback") &&
        admission_source.include?("--immutable") &&
        admission_source.include?("--request-digest") &&
        admission_source.include?("--plan-artifact-id") &&
        admission_source.include?("--plan-artifact-digest"),
      "admission publisher lacks landed metadata and exact readback identity"
    )
    require_no_candidate_execution(admission_source, "admission publisher")
    jobs.each_value do |job|
      permissions = job.fetch("permissions", {})
      require_contract(
        !(permissions["contents"] == "write" && permissions["packages"] == "write"),
        "one job combines Git and package write authority"
      )
    end

    all_text = flatten(workflow).join("\n")
    require_no_package_secret_references(workflow, "workflow")
    require_contract(!all_text.match?(/(?:^|\s)sleep(?:\s|$)/),
                     "workflow retries may not sleep on runners")
    require_contract(!all_text.match?(/:[[:space:]]*(?:latest|candidate|current)(?:\s|$)/),
                     "workflow names a mutable candidate tag")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "workflow structure is incomplete: #{error.message}"
  end

  def check_reuse(workflow)
    require_no_automattic_ghcr(workflow)
    require_contract(workflow.fetch("permissions") == {},
                     "reuse workflow permissions must be empty")
    event = triggers(workflow)
    require_contract(event.keys == ["workflow_call"],
                     "reuse workflow must be workflow_call only")
    expected_inputs = %w[
      coordination-artifact-id coordination-artifact-digest tap-commit work-id
    ]
    call = event.fetch("workflow_call")
    inputs = call.fetch("inputs")
    require_contract(inputs.keys == expected_inputs && inputs.values.all? do |input|
                       input == {"required" => true, "type" => "string"}
                     end && !call.key?("secrets"), "reuse workflow inputs changed")
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
    require_pinned_oras(publisher, "reuse publisher")
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
    require_no_package_secret_references(workflow, "reuse workflow")
    require_contract(!all_text.match?(/(?:^|\s)sleep(?:\s|$)/),
                     "reuse workflow gains sleeps")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "reuse workflow structure is incomplete: #{error.message}"
  end

  def check_reusable(workflow, kind)
    require_no_automattic_ghcr(workflow)
    require_contract(%i[candidate verification].include?(kind),
                     "reusable ABI workflow kind is unsupported")
    require_contract(workflow.fetch("permissions") == {},
                     "reusable workflow permissions must be empty")
    event = triggers(workflow)
    require_contract(event.keys == ["workflow_call"],
                     "reusable ABI workflow must be workflow_call only")
    expected_inputs = %w[
      coordination-artifact-id coordination-artifact-digest kandelo-head
      kandelo-policy-commit kandelo-repository tap-commit realm-artifact-id
      realm-artifact-digest realm-archive-sha256 realm-source-tree work-id
    ]
    call = event.fetch("workflow_call")
    inputs = call.fetch("inputs")
    require_contract(inputs.keys == expected_inputs && inputs.values.all? do |input|
                       input == {"required" => true, "type" => "string"}
                     end && !call.key?("secrets"), "reusable ABI workflow inputs changed")

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
    require_candidate_checkouts(producer, producer_name, include_homebrew: true)
    downloads = action_steps(producer, DOWNLOAD_ARTIFACT)
    coordination_download = downloads.find do |step|
      step.dig("with", "path") == "${{ runner.temp }}/coordination"
    end
    realm_download = downloads.find do |step|
      step.dig("with", "path") == "${{ runner.temp }}/shared-realm"
    end
    require_contract(downloads.length == 2 &&
                     !coordination_download.nil? &&
                     coordination_download.fetch("with") == {
                       "artifact-ids" => "${{ inputs.coordination-artifact-id }}",
                       "path" => "${{ runner.temp }}/coordination",
                       "merge-multiple" => true
                     } && !realm_download.nil? &&
                     realm_download.fetch("with") == {
                       "artifact-ids" => "${{ inputs.realm-artifact-id }}",
                       "path" => "${{ runner.temp }}/shared-realm",
                       "merge-multiple" => true
                     }, "candidate producer lacks exact coordination or realm artifact")
    commands = run_steps(producer)
    restore = commands.find do |step|
      step["name"] == "Restore exact prepared Homebrew realm"
    end
    require_contract(!restore.nil? &&
                     restore.fetch("working-directory") == "kandelo-authority" &&
                     restore.fetch("env") == {
                       "EXPECTED_REALM_ARTIFACT_DIGEST" =>
                         "${{ inputs.realm-artifact-digest }}",
                       "EXPECTED_REALM_ARCHIVE_SHA256" =>
                         "${{ inputs.realm-archive-sha256 }}",
                       "EXPECTED_REALM_COMMIT" => "${{ inputs.kandelo-head }}",
                       "EXPECTED_REALM_TREE" => "${{ inputs.realm-source-tree }}"
                     } && restore.fetch("run").include?(
                       "abi-staging-restore-homebrew-realm.sh"
                     ), "candidate producer does not restore the exact shared realm")
    if kind == :candidate
      export = commands.find do |step|
        step["name"] == "Export exact candidate build identity"
      end
      realm = commands.find do |step|
        step["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      require_contract(commands.length == 4 && !export.nil? && !realm.nil? &&
                       export.fetch("working-directory") == "kandelo-authority" &&
                       export.fetch("env") == {"WORK_ID" => "${{ inputs.work-id }}"} &&
                       export.fetch("run").include?("export-build-realm") &&
                       export.fetch("run").include?('"PYTHONDONTWRITEBYTECODE=1"') &&
                       export.fetch("run").include?('--github-env "$GITHUB_ENV"') &&
                       realm.fetch("working-directory") == "kandelo-authority" &&
                       realm.fetch("env") == {
                         "HOMEBREW_BREW_COMMIT" =>
                           "a92554a538e81fad0c5074443885dbcc4c36221d",
                         "WORK_ID" => "${{ inputs.work-id }}"
                       } &&
                       realm.fetch("run").include?(
                         'realm_root="$(mktemp -d /tmp/k.XXXXXX)"'
                       ) &&
                       realm.fetch("run").include?(
                         'package_cache="$GITHUB_WORKSPACE/kandelo-source/.ci-test-binary-cache"'
                       ) &&
                       realm.fetch("run").include?(
                         'test -d "$package_cache/programs" && test ! -L "$package_cache/programs"'
                       ) &&
                       realm.fetch("run").include?(
                         [
                           "formula_cache_paths=(",
                           '  "$package_cache"',
                           '  "$package_cache/programs"',
                           '  "$GITHUB_WORKSPACE/kandelo-source/binaries"',
                           ")"
                         ].join("\n")
                       ) &&
                       realm.fetch("run").include?(
                         '/usr/bin/sudo -n /usr/bin/chown root:root -- "${formula_cache_paths[@]}"'
                       ) &&
                       realm.fetch("run").include?(
                         '/usr/bin/sudo -n /usr/bin/chmod 0555 -- "${formula_cache_paths[@]}"'
                       ) &&
                       realm.fetch("run").include?(
                         'test "$(/usr/bin/stat -c \'%u:%g:%a\' "$directory")" = "0:0:555"'
                       ) &&
                       realm.fetch("run").include?(
                         'test -d "$GITHUB_WORKSPACE/kandelo-source/binaries"'
                       ) &&
                       realm.fetch("run").include?(
                         'playwright_seed="$GITHUB_WORKSPACE/kandelo-source/.ci-homebrew-realm/ms-playwright"'
                       ) &&
                       realm.fetch("run").include?(
                         'shared_temp="$(mktemp -d /tmp/kandelo-homebrew.XXXXXX)"'
                       ) &&
                       realm.fetch("run").include?(
                         '/usr/bin/sudo -n /usr/bin/chown root:root "$shared_temp"'
                       ) &&
                       realm.fetch("run").include?(
                         '/usr/bin/sudo -n /usr/bin/chmod 1777 "$shared_temp"'
                       ) &&
                       realm.fetch("run").include?(
                         'mkdir -m 0770 "$shared_temp/cache"'
                       ) &&
                       realm.fetch("run").include?(
                         "test \"$(/usr/bin/stat -c '%u:%g:%a' \"$shared_temp/cache\")\" = \\\n" \
                           '  "$(/usr/bin/id -u):$(/usr/bin/id -g):770"'
                       ) &&
                       realm.fetch("run").include?(
                         'mkdir -m 0770 "$shared_temp/cache/downloads"'
                       ) &&
                       realm.fetch("run").include?(
                         'test "$(/usr/bin/stat -c \'%u:%g:%a\' "$shared_temp/cache/downloads")" = "$(/usr/bin/id -u):$(/usr/bin/id -g):770"'
                       ) &&
                       !realm.fetch("run").include?("protected_recipe_formula") &&
                       !realm.fetch("run").include?(
                         'if [ "$KANDELO_ABI_STAGING_FORMULA" = ruby ]; then'
                       ) &&
                       realm.fetch("run").include?(
                         'build_user="kandelo-homebrew-build"'
                       ) &&
                       realm.fetch("run").include?(
                         'recipe_user="kandelo-homebrew-recipe"'
                       ) &&
                       realm.fetch("run").include?("/usr/sbin/useradd") &&
                       realm.fetch("run").include?(
                         'echo "KANDELO_HOMEBREW_BUILD_USER=$build_user"'
                       ) &&
                       realm.fetch("run").include?(
                         'echo "KANDELO_HOMEBREW_RECIPE_USER=$recipe_user"'
                       ) &&
                       realm.fetch("run").include?(
                         'echo "KANDELO_HOMEBREW_SHARED_TEMP=$shared_temp"'
                       ) &&
                       realm.fetch("run").include?(
                         'playwright_browsers="$shared_temp/ms-playwright"'
                       ) &&
                       realm.fetch("run").include?("install chromium") &&
                       !realm.fetch("run").include?("--with-deps") &&
                       !realm.fetch("run").include?('"$realm_root/package-cache"') &&
                       realm.fetch("run").include?("homebrew-prepare-host-prefix.sh") &&
                       realm.fetch("run").include?(
                         'host_target="$(cat "$GITHUB_WORKSPACE/kandelo-source/.ci-homebrew-realm/host-target")"'
                       ) &&
                       realm.fetch("run").include?(
                         'candidate_xtask="$GITHUB_WORKSPACE/kandelo-source/target/$host_target/release/xtask"'
                       ) &&
                       realm.fetch("run").include?(
                         '/usr/bin/sudo -n /usr/bin/install -o root -g root -m 0555 --'
                       ) &&
                       realm.fetch("run").include?(
                         '"$candidate_xtask" "$candidate_xtask_staged"'
                       ) &&
                       realm.fetch("run").include?(
                         '/usr/bin/sudo -n /usr/bin/mv -f -- "$candidate_xtask_staged" "$candidate_xtask"'
                       ) &&
                       realm.fetch("run").include?(
                         'echo "WASM_POSIX_XTASK_BIN=$candidate_xtask"'
                       ) &&
                       realm.fetch("run").include?("0:0:555:1") &&
                       realm.fetch("run").include?("candidate_xtask_sha256") &&
                       realm.fetch("run").include?("candidate_platform_tools=(") &&
                       realm.fetch("run").include?(
                         '"tools/bin/wasm-fork-instrument"'
                       ) &&
                       realm.fetch("run").include?(
                         'node_bin="$(cat "$GITHUB_WORKSPACE/kandelo-source/.ci-homebrew-realm/node-bin")"'
                       ) &&
                       realm.fetch("run").include?(
                         '"tools/bin/wasm-local-root-spill"'
                       ) &&
                       realm.fetch("run").include?(
                         '/usr/bin/sudo -n /usr/bin/chown root:root --'
                       ) &&
                       realm.fetch("run").include?(
                         '"$GITHUB_WORKSPACE/kandelo-source/tools/bin"'
                       ) &&
                       !realm.fetch("run").include?("scripts/build-musl.sh") &&
                       !realm.fetch("run").include?("npm ci") &&
                       realm.fetch("run").scan("env -u GITHUB_TOKEN").length == 2 &&
                       realm.fetch("run").scan("-u ACTIONS_RUNTIME_TOKEN").length == 2,
                       "candidate producer build realm changed")
    else
      export = commands.find do |step|
        step["name"] == "Export exact candidate verification identity"
      end
      realm = commands.find do |step|
        step["name"] == "Prepare exact uncredentialed Homebrew realm"
      end
      require_contract(
        commands.length == 4 && !export.nil? && !realm.nil? &&
          export.fetch("working-directory") == "kandelo-authority" &&
          export.fetch("env") == {"WORK_ID" => "${{ inputs.work-id }}"} &&
          export.fetch("run").include?("export-verification-realm") &&
          export.fetch("run").include?('"PYTHONDONTWRITEBYTECODE=1"') &&
          export.fetch("run").include?('"PYTHONSAFEPATH=1"') &&
          export.fetch("run").include?(
            "python3 -P -m scripts.abi_staging.cli export-verification-realm"
          ) &&
          export.fetch("run").include?('--github-env "$GITHUB_ENV"') &&
          realm.fetch("working-directory") == "kandelo-authority" &&
          realm.fetch("env") == {
            "HOMEBREW_BREW_COMMIT" =>
              "a92554a538e81fad0c5074443885dbcc4c36221d",
            "WORK_ID" => "${{ inputs.work-id }}"
          } &&
          realm.fetch("run").include?(
            'candidate_workflow="$GITHUB_WORKSPACE/tap-authority/.github/workflows/abi-staging-candidate.yml"'
          ) &&
          realm.fetch("run").include?(
            '/^      - name: Prepare exact uncredentialed Homebrew realm$/'
          ) &&
          realm.fetch("run").include?('bash "$realm_script"'),
        "verification producer build realm changed"
      )
    end
    command_step = commands.find do |step|
      step.fetch("run", "").include?(producer_cli)
    end
    require_contract(!command_step.nil? &&
                     command_step.fetch("working-directory") ==
                       (kind == :candidate ? "kandelo-source" : "kandelo-authority") &&
                     command_step.fetch("env") == {
                       "WORK_ID" => "${{ inputs.work-id }}"
                     }, "candidate producer command inputs changed")
    source = command_step.fetch("run")
    require_contract(source.include?("env -u GITHUB_TOKEN") &&
                     source.include?("-u ACTIONS_RUNTIME_TOKEN") &&
                     source.include?("scripts/dev-shell.sh env") &&
                     source.include?('"PYTHONDONTWRITEBYTECODE=1"') &&
                     source.include?('"PYTHONSAFEPATH=1"') &&
                     source.include?(
                       "python3 -P -m scripts.abi_staging.cli #{producer_cli}"
                     ) &&
                     source.include?(
                       'PYTHONPATH=$GITHUB_WORKSPACE/tap-authority'
                     ) &&
                     (kind != :candidate || (
                       source.include?('"$candidate_entrypoint"') &&
                       !source.include?("/usr/bin/sudo -n -E -u") &&
                       !source.include?("/usr/bin/sg")
                     )) &&
                     (kind != :candidate || source.include?("umask 0007")) &&
                     source.include?(
                       "WASM_POSIX_XTASK_BIN=$WASM_POSIX_XTASK_BIN"
                     ) &&
                     (kind != :candidate || %w[
                       KANDELO_HOMEBREW_BUILD_USER
                       KANDELO_HOMEBREW_RECIPE_USER
                       KANDELO_HOMEBREW_SHARED_TEMP
                       KANDELO_HOMEBREW_SUDO_BIN
                       KANDELO_HOMEBREW_SYSTEMD_RUN_BIN
                       KANDELO_HOMEBREW_SYSTEMCTL_BIN
                       KANDELO_HOMEBREW_GETENT_BIN
                       KANDELO_HOMEBREW_PGREP_BIN
                       KANDELO_HOMEBREW_PKILL_BIN
                     ].all? { |name| source.include?("#{name}=$#{name}") }) &&
                     (kind != :candidate || !source.include?(
                       'if [ "$KANDELO_ABI_STAGING_FORMULA" = ruby ]; then'
                     )) &&
                     !source.include?("../kandelo-source/scripts/dev-shell.sh") &&
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
    require_pinned_oras(publisher, "reusable publisher")
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
    require_no_package_secret_references(workflow, "reusable ABI workflow")
    require_contract(!all_text.match?(/(?:^|\s)sleep(?:\s|$)/),
                     "reusable ABI workflow gains sleeps")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "reusable ABI workflow structure is incomplete: #{error.message}"
  end

  def check_maintenance(workflow)
    require_no_automattic_ghcr(workflow)
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
                     "maintenance workflow job set changed")
    job = jobs.fetch("maintain")
    require_contract(!job.key?("if"),
                     "maintenance command gained a conditional authority lane")
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
    require_pinned_oras(job, "maintenance publisher")

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
                     !source.include?("historical-repair") &&
                     !source.include?("historical_maintenance") &&
                     !source.include?("--replace"),
                     "write-capable maintenance executes candidate, historical, or mutable publication code")
    require_contract(!flatten(workflow).join("\n").include?("historical-repair"),
                     "maintenance workflow exposes deferred historical repair authority")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "maintenance workflow structure is incomplete: #{error.message}"
  end

  def check_cleanup(workflow)
    require_no_automattic_ghcr(workflow)
    require_contract(workflow.fetch("permissions") == {},
                     "cleanup workflow permissions must be empty")
    event = triggers(workflow)
    require_contract(event.keys.sort == %w[schedule workflow_dispatch] &&
                     event.fetch("schedule") == [{"cron" => "17 4 * * *"}],
                     "cleanup workflow triggers changed")
    inputs = event.fetch("workflow_dispatch").fetch("inputs")
    require_contract(inputs.keys == %w[mode target_reference reason_category justification],
                     "cleanup workflow gained a free-form selector")
    require_contract(inputs.fetch("mode").slice("required", "type", "default", "options") == {
                       "required" => true,
                       "type" => "choice",
                       "default" => "ordinary",
                       "options" => %w[ordinary immediate-purge]
                     }, "cleanup mode is not a closed choice")
    require_contract(inputs.fetch("target_reference").slice("required", "type", "default") == {
                       "required" => false,
                       "type" => "string",
                       "default" => ""
                     }, "cleanup target must remain one optional exact reference")
    require_contract(inputs.fetch("reason_category").slice("required", "type", "default", "options") == {
                       "required" => true,
                       "type" => "choice",
                       "default" => "retention-expired",
                       "options" => %w[retention-expired malicious-object legal-removal pathological-size]
                     }, "cleanup reason is not a closed choice")
    require_contract(inputs.fetch("justification").slice("required", "type", "default") == {
                       "required" => false,
                       "type" => "string",
                       "default" => ""
                     }, "cleanup justification input changed")
    require_contract(workflow.fetch("concurrency") == {
                       "group" => "abi-staging-candidate-cleanup",
                       "cancel-in-progress" => false
                     }, "cleanup concurrency changed")

    jobs = workflow.fetch("jobs")
    require_contract(jobs.keys == ["plan-cleanup"],
                     "cleanup observe-only job set changed")
    planner = jobs.fetch("plan-cleanup")
    require_contract(planner.fetch("permissions") == {
                       "contents" => "read", "packages" => "read"
                     }, "cleanup planner is not read-only")
    require_contract(!planner.key?("outputs"),
                     "cleanup observe-only planner exports writer handoff authority")
    require_contract(planner.fetch("runs-on") == "ubuntu-latest" &&
                     planner.fetch("timeout-minutes").between?(1, 30),
                     "cleanup runner or timeout changed")
    require_contract(!planner.key?("environment") && !planner.key?("secrets") &&
                     planner.fetch("steps").none? { |step| step["continue-on-error"] == true },
                     "cleanup job gained credentials or swallowed failure")
    check_actions(planner)

    planner_checkout = action_steps(planner, CHECKOUT)
    require_contract(planner_checkout.length == 1 &&
                     planner_checkout.fetch(0).fetch("with") == {
                       "ref" => "refs/heads/main",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "cleanup planner does not execute protected tap main")
    setups = action_steps(planner, SETUP_PYTHON)
    require_contract(setups.length == 1 &&
                     setups.fetch(0).dig("with", "python-version") == "3.13",
                     "cleanup job lacks declared Python")

    planner_commands = run_steps(planner)
    require_contract(planner_commands.length == 1,
                     "cleanup planner must use one protected coordinator")
    planner_step = planner_commands.fetch(0)
    planner_source = planner_step.fetch("run")
    require_contract(planner_step.fetch("working-directory") == "tap-authority" &&
                     planner_source.include?("python3 -m scripts.abi_staging.cleanup plan-live") &&
                     planner_source.include?("--enumerate-public-records") &&
                     planner_source.include?("--recheck-lifecycle") &&
                     planner_source.include?("--verify-actor-permission") &&
                     planner_source.include?("--grace-days 30") &&
                     planner_source.include?("--batch-size 16") &&
                     planner_source.include?('--target-reference "$TARGET_REFERENCE"'),
                     "cleanup planner lacks complete public pin and grace analysis")
    require_no_candidate_execution(planner_source, "cleanup planner")
    all_text = flatten(workflow).join("\n")
    require_contract(!all_text.include?("${{ secrets.") &&
                     !all_text.include?("execute-live") &&
                     !all_text.include?("immutable-tombstone") &&
                     !planner_source.match?(/(?:^|\s)(?:gh|oras)\s+[^\n]*(?:delete|remove)/i) &&
                     !planner_source.match?(/(?:^|\s)(?:rm|rmdir)\s/) &&
                     !planner_source.match?(/[?*\[]/) &&
                     !planner_source.match?(/(?:^|\s)(?:eval|source|curl|wget|sleep)(?:\s|$)/),
                     "cleanup observe-only workflow gained deletion, tombstone, secret, glob, or execution authority")

    planner_uploads = action_steps(planner, UPLOAD_ARTIFACT)
    require_contract(planner_uploads.length == 1 &&
                     planner_uploads.fetch(0).dig("with", "if-no-files-found") == "error" &&
                     planner_uploads.fetch(0).dig("with", "compression-level") == 0 &&
                     planner_uploads.fetch(0).dig("with", "retention-days") == 7,
                     "cleanup observation plan is not retained exactly")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "cleanup workflow structure is incomplete: #{error.message}"
  end

  def check_history(workflow)
    require_no_automattic_ghcr(workflow)
    require_contract(workflow.fetch("permissions") == {},
                     "history workflow permissions must be empty")
    event = triggers(workflow)
    require_contract(event.keys == ["workflow_dispatch"] &&
                     event.fetch("workflow_dispatch").nil?,
                     "history workflow must be input-free manual protected code")
    require_contract(workflow.fetch("concurrency") == {
                       "group" => "abi-staging-abi-history",
                       "cancel-in-progress" => false
                     }, "history concurrency changed")
    jobs = workflow.fetch("jobs")
    require_contract(jobs.keys == %w[plan-and-verify-policy create-history-ref verify-and-publish-history],
                     "history workflow job split changed")
    plan = jobs.fetch("plan-and-verify-policy")
    create = jobs.fetch("create-history-ref")
    verify = jobs.fetch("verify-and-publish-history")
    require_contract(plan.fetch("permissions") == {"contents" => "read"},
                     "history planner must remain contents-read only")
    require_contract(create.fetch("permissions") == {"contents" => "write"},
                     "history ref writer permissions changed")
    require_contract(verify.fetch("permissions") == {
                       "actions" => "read",
                       "contents" => "read",
                       "packages" => "write"
                     }, "history publisher permissions changed")
    require_contract(create.fetch("needs") == "plan-and-verify-policy" &&
                     create.fetch("if") == "needs.plan-and-verify-policy.outputs.write-enabled == 'true'",
                     "history ref creation is not gated by protected active mode")
    require_contract(verify.fetch("needs") == %w[plan-and-verify-policy create-history-ref] &&
                     verify.fetch("if") == "needs.plan-and-verify-policy.outputs.write-enabled == 'true'",
                     "history publication is not gated by exact creation")
    [plan, create, verify].each do |job|
      require_contract(job.fetch("runs-on") == "ubuntu-latest" &&
                       job.fetch("timeout-minutes").between?(1, 60),
                       "history runner or timeout changed")
      require_contract(!job.key?("environment") &&
                       job.fetch("steps").none? { |step| step["continue-on-error"] == true },
                       "history job gained a credentialed environment or swallowed failure")
      check_actions(job)
      setup = action_steps(job, SETUP_PYTHON)
      require_contract(setup.length == 1 &&
                       setup.fetch(0).dig("with", "python-version") == "3.13",
                       "history job lacks declared Python")
    end

    plan_checkout = plan.fetch("steps").find do |step|
      step["uses"]&.start_with?(CHECKOUT)
    end
    require_contract(plan_checkout&.fetch("with", {}) == {
                       "ref" => "refs/heads/main",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "history plan must execute protected tap main")
    plan_commands = plan.fetch("steps").select { |step| step.key?("run") }
    require_contract(plan_commands.length == 1,
                     "history planning must use one protected coordinator")
    plan_step = plan_commands.fetch(0)
    plan_source = plan_step.fetch("run")
    require_contract(plan_step.fetch("working-directory") == "tap-authority" &&
                     plan_step.fetch("env") == {"GITHUB_TOKEN" => "${{ github.token }}"} &&
                     plan_source.include?("python3 -m scripts.abi_staging.cli plan-history") &&
                     plan_source.include?('--tap-root "$PWD"') &&
                     plan_source.include?('--repository "$GITHUB_REPOSITORY"') &&
                     plan_source.include?('--github-output "$GITHUB_OUTPUT"'),
                     "history planner does not derive exact protected policy/protection")
    require_contract(!plan_source.include?("create-history-ref") &&
                     !plan_source.match?(/\b(?:git\s+push|promote|build|verify-bottle)\b/),
                     "history planner writes or executes candidate work")

    create_checkout = create.fetch("steps").find do |step|
      step["uses"]&.start_with?(CHECKOUT)
    end
    require_contract(create_checkout&.fetch("with", {}) == {
                       "ref" => "${{ needs.plan-and-verify-policy.outputs.tap-commit }}",
                       "fetch-depth" => 1,
                       "persist-credentials" => false,
                       "path" => "tap-authority"
                     }, "history writer does not use exact preactivation tap code")
    create_download = create.fetch("steps").find do |step|
      step["uses"]&.start_with?(DOWNLOAD_ARTIFACT)
    end
    require_contract(create_download&.dig("with", "artifact-ids") ==
                     "${{ needs.plan-and-verify-policy.outputs.artifact-id }}",
                     "history writer does not consume the exact protected plan")
    create_commands = create.fetch("steps").select { |step| step.key?("run") }
    require_contract(create_commands.length == 1,
                     "history ref creation must use one reviewed writer")
    create_step = create_commands.fetch(0)
    create_source = create_step.fetch("run")
    require_contract(create_step.fetch("working-directory") == "tap-authority" &&
                     create_step.fetch("env") == {"GITHUB_TOKEN" => "${{ github.token }}"} &&
                     create_source.include?("python3 -m scripts.abi_staging.cli create-history-ref") &&
                     create_source.include?('--plan "$RUNNER_TEMP/abi-history-plan/plan.json"') &&
                     create_source.include?('--tap-root "$PWD"') &&
                     create_source.include?('--repository "$GITHUB_REPOSITORY"'),
                     "history ref writer does not bind exact plan and protected code")
    require_contract(!create_source.match?(/(?:--force|\bgit\s+(?:push|update-ref)\b|\b(?:bash|sh)\s+Formula\/|\bpromote\b)/),
                     "history ref writer can force, promote, or execute candidate code")

    verify_checkouts = verify.fetch("steps").select do |step|
      step["uses"]&.start_with?(CHECKOUT)
    end
    require_contract(verify_checkouts.map { |step| step.dig("with", "ref") } == [
                       "${{ needs.plan-and-verify-policy.outputs.tap-commit }}",
                       "${{ needs.plan-and-verify-policy.outputs.branch }}"
                     ] && verify_checkouts.map { |step| step.dig("with", "path") } ==
                     %w[tap-authority history-checkout],
                     "history verification does not separate protected code and exact history")
    verify_downloads = verify.fetch("steps").select do |step|
      step["uses"]&.start_with?(DOWNLOAD_ARTIFACT)
    end
    require_contract(verify_downloads.map { |step| step.dig("with", "artifact-ids") } == [
                       "${{ needs.plan-and-verify-policy.outputs.artifact-id }}",
                       "${{ needs.create-history-ref.outputs.artifact-id }}"
                     ], "history publisher handoffs are not exact artifacts")
    require_pinned_oras(verify, "history publisher")
    verify_commands = verify.fetch("steps").select { |step| step.key?("run") }
    require_contract(verify_commands.length == 1,
                     "history verification/publication must use one coordinator")
    verify_step = verify_commands.fetch(0)
    verify_source = verify_step.fetch("run")
    require_contract(verify_step.fetch("working-directory") == "tap-authority" &&
                     verify_step.fetch("env") == {
                       "GITHUB_TOKEN" => "${{ github.token }}",
                       "HOMEBREW_GITHUB_PACKAGES_TOKEN" => "${{ github.token }}",
                       "HOMEBREW_GITHUB_PACKAGES_USER" => "${{ github.actor }}"
                     }, "history publication authority changed")
    require_contract(verify_source.include?("python3 -m scripts.abi_staging.cli verify-history") &&
                     verify_source.include?("python3 -m scripts.abi_staging.cli publish-history-record") &&
                     verify_source.include?('--history-root "$GITHUB_WORKSPACE/history-checkout"') &&
                     verify_source.include?('--creation "$RUNNER_TEMP/abi-history-create/creation.json"') &&
                     verify_source.scan("--anonymous-readback").length == 2 &&
                     verify_source.include?("--immutable"),
                     "history publisher omits public revalidation or immutability")
    require_contract(!verify_source.match?(/\b(?:promote-formula|build|brew|make|cmake|cargo|npm|sleep)\b/) &&
                     !verify_source.match?(/\b(?:eval|source|curl|wget)\b/),
                     "history publisher promotes, executes candidate code, sleeps, or bypasses transport")

    all_text = flatten(workflow).join("\n")
    require_no_package_secret_references(workflow, "history workflow")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "history workflow structure is incomplete: #{error.message}"
  end

  def check_pages_canonical(workflow)
    require_contract(workflow.fetch("permissions") == {},
                     "Pages canonical workflow permissions must be empty")
    event = triggers(workflow)
    inputs = event.dig("workflow_dispatch", "inputs")
    require_contract(event.keys == ["workflow_dispatch"] &&
                     inputs.is_a?(Hash) &&
                     inputs.keys.sort == %w[candidates kandelo_commit],
                     "Pages canonical trigger or inputs changed")
    jobs = workflow.fetch("jobs")
    require_contract(jobs.keys.sort == %w[publish update-metadata validate],
                     "Pages canonical job set changed")
    validate = jobs.fetch("validate")
    publisher = jobs.fetch("publish")
    metadata = jobs.fetch("update-metadata")
    require_contract(validate.fetch("permissions") == {},
                     "Pages candidate validator gained authority")
    require_contract(publisher.fetch("permissions") == {
                       "contents" => "read", "packages" => "write"
                     }, "Pages canonical publisher permissions changed")
    require_contract(metadata.fetch("permissions") == {"contents" => "write"},
                     "Pages metadata writer permissions changed")
    require_contract(publisher.fetch("needs") == "validate" &&
                     metadata.fetch("needs") == %w[validate publish],
                     "Pages canonical job ordering changed")
    require_contract(publisher.dig("strategy", "fail-fast") == false &&
                     publisher.dig("strategy", "max-parallel").between?(1, 16) &&
                     publisher.dig("strategy", "matrix") ==
                       "${{ fromJSON(needs.validate.outputs.matrix) }}",
                     "Pages canonical publication matrix changed")
    jobs.each_value { |job| check_actions(job) }
    publish_steps = run_steps(publisher)
    metadata_steps = run_steps(metadata)
    require_contract(publish_steps.length == 1 && metadata_steps.length == 1,
                     "Pages canonical writers gained executable steps")
    publish = publish_steps.fetch(0)
    update = metadata_steps.fetch(0)
    require_contract(publish.fetch("working-directory") == "tap-authority" &&
                     publish.fetch("env") == {
                       "HOMEBREW_GITHUB_PACKAGES_TOKEN" => "${{ github.token }}",
                       "HOMEBREW_GITHUB_PACKAGES_USER" => "${{ github.actor }}",
                       "PYTHONPATH" => "${{ github.workspace }}/tap-authority"
                     } &&
                     publish.fetch("run").include?("publish-pages-canonical") &&
                     publish.fetch("run").include?("--anonymous-readback"),
                     "Pages canonical package writer changed")
    update_text = update.fetch("run")
    require_contract(update.fetch("working-directory") == "tap-authority" &&
                     update.fetch("env") == {
                       "CANDIDATES" => "${{ inputs.candidates }}",
                       "PYTHONPATH" => "${{ github.workspace }}/tap-authority"
                     } &&
                     update_text.include?("apply-pages-canonical-metadata") &&
                     update_text.include?("--anonymous-readback") &&
                     update_text.include?("--contents-only") &&
                     update_text.include?("--normal-push") &&
                     !update_text.include?("HOMEBREW_GITHUB_PACKAGES"),
                     "Pages contents-only metadata writer changed")
    text = flatten(workflow).join("\n")
    %w[admission receipt signature product-evidence publisher-policy].each do |term|
      require_contract(!text.include?(term),
                       "Pages direct path restored #{term}")
    end
    require_no_automattic_ghcr(workflow)
    require_no_package_secret_references(workflow, "Pages canonical workflow")
    true
  rescue KeyError, NoMethodError => error
    raise Violation, "Pages canonical workflow structure is incomplete: #{error.message}"
  end
end

if $PROGRAM_NAME == __FILE__
  root = File.expand_path("..", __dir__)
  begin
    AbiStagingWorkflowCheck.check_public_discovery(
      File.read(File.join(root, "scripts/abi_staging/github_public.py"))
    )
    paths = if ARGV.empty?
      [
        [File.join(root, ".github/workflows/abi-staging-reconcile.yml"), :check],
        [File.join(root, ".github/workflows/abi-staging-candidate.yml"), :candidate],
        [File.join(root, ".github/workflows/abi-staging-reuse.yml"), :check_reuse],
        [File.join(root, ".github/workflows/abi-staging-verification.yml"), :verification],
        [File.join(root, ".github/workflows/abi-staging-maintenance.yml"), :check_maintenance],
        [File.join(root, ".github/workflows/abi-staging-abi-history.yml"), :check_history],
        [File.join(root, ".github/workflows/abi-staging-candidate-cleanup.yml"), :check_cleanup],
        [File.join(root, ".github/workflows/pages-canonicalize.yml"), :check_pages_canonical]
      ]
    else
      ARGV.map do |path|
        method = case File.basename(path)
        when "abi-staging-maintenance.yml" then :check_maintenance
        when "abi-staging-abi-history.yml" then :check_history
        when "abi-staging-candidate-cleanup.yml" then :check_cleanup
        when "abi-staging-candidate.yml" then :candidate
        when "abi-staging-reuse.yml" then :check_reuse
        when "abi-staging-verification.yml" then :verification
        when "pages-canonicalize.yml" then :check_pages_canonical
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
