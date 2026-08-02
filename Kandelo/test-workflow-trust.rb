#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"
require "digest"
require "json"

candidate_root = ARGV.shift
abort "usage: test-workflow-trust.rb [candidate-root]" unless ARGV.empty?

ROOT = candidate_root ? File.expand_path(candidate_root) : File.expand_path("..", __dir__)
WORKFLOW_ROOT = File.join(ROOT, ".github/workflows")
CONTRACT_PATH = File.join(WORKFLOW_ROOT, "contract-checks.yml")
BASE_CONTRACT_PATH = File.join(WORKFLOW_ROOT, "base-contract-checks.yml")
CLOSED_SELECTION_PATH =
  File.join(WORKFLOW_ROOT, "publish-closed-selection.yml")
PREFIX_CAMPAIGN_PATH =
  File.join(WORKFLOW_ROOT, "prefix-campaign-bottles.yml")
PREFIX_CAMPAIGN_AUTHORITY_PATH =
  File.join(ROOT, "Kandelo/prefix-campaign-authority.json")
PREFIX_CAMPAIGN_CONTROLLER_PATH =
  File.join(ROOT, "scripts/prefix-campaign-controller.py")
EXPECTED_WORKFLOW_FILES = %w[
  base-contract-checks.yml
  contract-checks.yml
  dry-run-bottles.yml
  maintain-bottles.yml
  prefix-campaign-bottles.yml
  publish-bottles.yml
  publish-closed-selection.yml
  publish-main-shell-mirror.yml
  repository-namespace-canary.yml
].freeze
CALLER_PERMISSIONS = {
  "actions" => "read",
  "contents" => "write",
  "packages" => "write",
}.freeze
FIRST_PUBLICATION_PERMISSIONS = {
  "actions" => "read",
  "contents" => "read",
  "packages" => "write",
}.freeze
MAIN_SHELL_MIRROR_PERMISSIONS = {
  "actions" => "read",
  "contents" => "write",
}.freeze
CLOSED_SELECTION_PERMISSIONS = MAIN_SHELL_MIRROR_PERMISSIONS
CHECKOUT_ACTION = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
DOWNLOAD_ACTION =
  "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
UPLOAD_ACTION =
  "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
NIX_INSTALLER_ACTION =
  "DeterminateSystems/nix-installer-action@" \
  "ef8a148080ab6020fd15196c2084a2eea5ff2d25"
RUBY_ACTION = "ruby/setup-ruby@d45b1a4e94b71acab930e56e79c6aa188764e7f9"
# Write publication executes and consumes packages from one reviewed Kandelo
# main commit. Its rootfs package generation is admitted separately by a
# content-addressed release tag, so preserved staging data can never become
# caller authority.
#
# WHY: the credential-free dry run may advance first so it can prove a new
# publisher before that main commit owns an admitted package generation.
# While split, credentialed callers retain their complete older tuple and
# fail the publisher's current-main check. A full rotation converges both
# pins only after a fresh generation is admitted.
CURRENT_KANDELO_WORKFLOW_SHA = "6024539d7849bb5f0d9c235b97218e60f03a2fef"
CURRENT_KANDELO_CONSUMER_SHA = CURRENT_KANDELO_WORKFLOW_SHA
DRY_RUN_KANDELO_WORKFLOW_SHA = "6024539d7849bb5f0d9c235b97218e60f03a2fef"
# WHY: the lifecycle caller must remain pinned to reviewed Kandelo main. TA0,
# the catalog, and the canary are separate final immutable authorities.
MAIN_SHELL_MIRROR_KANDELO_SHA =
  "0b0945f5f78b5e7577d08fafffc540408a501cb1"
MAIN_SHELL_MIRROR_TAP_CATALOG_SHA = "6ad0e3dbc60e5572c4288c86919238f71c1bc110"
MAIN_SHELL_MIRROR_AUTHORITY_SHA =
  "08f8f32c94bee8d6fc2948e453e53ece29b1c8e1"
MAIN_SHELL_MIRROR_CANARY_SHA = "d8bdda662f6d80cf3dcdbe8451edb12bb33bbafc"
PACKAGE_GENERATION_WASM32_TAG = "package-generation-rootfs-wasm32-abi-v42-sha256-d098e1f6b90cdc7a214a59f7dcfb5b87c2dda077572177ed4f1a4a8eeca46a1c"
# WHY: a closed selection writes an immutable release. The protected tap
# caller must select exactly the Kandelo main commit that owns every executable
# publication step; a mutable ref would let those steps change after review.
CLOSED_SELECTION_KANDELO_SHA =
  "662f00c44f3e1d0ebc0d1a573df101e721b73006"

def check(condition, message)
  raise message unless condition
end

def load_workflow(path)
  workflow = YAML.safe_load(File.read(path), aliases: false)
  check(workflow.is_a?(Hash), "#{File.basename(path)} is not a workflow mapping")
  workflow
end

def load_json(path)
  value = JSON.parse(File.read(path), create_additions: false)
  check(value.is_a?(Hash), "#{File.basename(path)} is not a JSON mapping")
  value
end

def workflow_events(workflow)
  events = workflow.key?("on") ? workflow["on"] : workflow[true]
  check(events.is_a?(Hash), "workflow on: value is not a mapping")
  events
end

def normalized_keys(mapping, label)
  check(mapping.is_a?(Hash), "#{label} is not a mapping")
  keys = mapping.keys.map { |key| key == true ? "on" : key.to_s }
  check(keys.uniq.length == keys.length, "#{label} has ambiguous keys")
  keys
end

def values_for_key(node, wanted, values = [])
  case node
  when Hash
    node.each do |key, value|
      values << value if key.to_s == wanted
      values_for_key(value, wanted, values)
    end
  when Array
    node.each { |value| values_for_key(value, wanted, values) }
  end
  values
end

def exact_permissions?(actual, expected)
  actual.is_a?(Hash) && actual.transform_keys(&:to_s) == expected
end

def expression(source)
  "$" + "{{ #{source} }}"
end

def compact_expression(source)
  source.is_a?(String) ? source.split.join(" ") : source
end

def deep_copy(value)
  Marshal.load(Marshal.dump(value))
end

PUBLISH_RUN_NAME = [
  "Publish Kandelo bottles /",
  expression("github.event.client_payload.formulae"),
  "/",
  expression("github.event.client_payload.dispatch_token || 'untracked'"),
].join(" ").freeze

def expect_rejection(label)
  rejected = false
  begin
    yield
  rescue KeyError, RuntimeError
    rejected = true
  end
  check(rejected, "self-test accepted #{label}")
end

WRITE_PUBLISH_INPUTS = {
  "kandelo-repository" => "Automattic/kandelo",
  "kandelo-ref" => CURRENT_KANDELO_CONSUMER_SHA,
  "tap-repository" => "kandelo-dev/homebrew-tap-core",
  "tap-name" => "kandelo-dev/tap-core",
  "tap-ref" => expression("github.event.client_payload.tap_sha"),
  "formulae" => expression("github.event.client_payload.formulae"),
  "arches" => expression("github.event.client_payload.arches || 'wasm32'"),
  "release-tag" => expression("github.event.client_payload.release_tag || ''"),
  "expected-cache-keys" => expression("github.event.client_payload.expected_cache_keys || ''"),
  "force" => expression("github.event.client_payload.force || false"),
  "dry-run" => false,
  "package-generation-wasm32" => PACKAGE_GENERATION_WASM32_TAG,
}.freeze

VFS_PUBLISH_INPUTS = WRITE_PUBLISH_INPUTS.merge({
  "require-vfs-acceptance" => expression(
    "github.event.client_payload.require_vfs_acceptance || false"
  ),
}).freeze

DRY_RUN_PUBLISH_INPUTS = {
  "kandelo-repository" => expression(
    "github.event.client_payload.kandelo_repository || 'Automattic/kandelo'"
  ),
  "kandelo-ref" => expression("github.event.client_payload.kandelo_ref || 'main'"),
  "tap-repository" => expression(
    "github.event.client_payload.tap_repository || 'kandelo-dev/homebrew-tap-core'"
  ),
  "tap-name" => expression(
    "github.event.client_payload.tap_name || 'kandelo-dev/tap-core'"
  ),
  "tap-ref" => expression("github.event.client_payload.tap_ref || 'main'"),
  "formulae" => expression("github.event.client_payload.formulae"),
  "arches" => expression("github.event.client_payload.arches || 'wasm32'"),
  "release-tag" => expression("github.event.client_payload.release_tag || ''"),
  "expected-cache-keys" => expression("github.event.client_payload.expected_cache_keys || ''"),
  "force" => expression("github.event.client_payload.force || false"),
  "dry-run" => true,
}.freeze

PAT_PUBLISH_INPUTS = VFS_PUBLISH_INPUTS.merge({
  "github-packages-user" => expression("vars.HOMEBREW_GITHUB_PACKAGES_USER"),
  "require-github-packages-token" => true,
}).freeze
PAT_PUBLISH_SECRETS = {
  "HOMEBREW_GITHUB_PACKAGES_TOKEN" =>
    expression("secrets.HOMEBREW_GITHUB_PACKAGES_TOKEN"),
}.freeze

FIRST_PUBLICATION_KANDELO_SHA = "6024539d7849bb5f0d9c235b97218e60f03a2fef"
RETIRED_PAT_KANDELO_WORKFLOW_SHA = "acc54b0d0fb5ffc1e742d437081a58bfd163e785"
PREVIOUS_KANDELO_WORKFLOW_SHA = "a71ab7a03cef9cb456e24c7b5f46bbc42122d9c4"
RETIRED_KANDELO_WORKFLOW_SHA = "c3f91d622c3c878e15783c67e99e483e54ab25c1"
SELF_TEST_KANDELO_WORKFLOW_SHA = "1111111111111111111111111111111111111111"

CALLER_SPECS = {
  "publish" => {
    path: File.join(WORKFLOW_ROOT, "publish-bottles.yml"),
    name: "Publish Kandelo bottles",
    run_name: PUBLISH_RUN_NAME,
    event: "publish-kandelo-bottles",
    job: "publish",
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@#{CURRENT_KANDELO_WORKFLOW_SHA}",
    inputs: VFS_PUBLISH_INPUTS,
  },
  "dry-run" => {
    path: File.join(WORKFLOW_ROOT, "dry-run-bottles.yml"),
    name: "Dry run Kandelo bottles",
    event: "dry-run-kandelo-bottles",
    job: "dry-run",
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@#{DRY_RUN_KANDELO_WORKFLOW_SHA}",
    inputs: DRY_RUN_PUBLISH_INPUTS,
  },
  "maintenance" => {
    path: File.join(WORKFLOW_ROOT, "maintain-bottles.yml"),
    name: "Maintain Kandelo bottles",
    event: "maintain-kandelo-bottles",
    job: "maintain",
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-maintenance.yml@#{CURRENT_KANDELO_WORKFLOW_SHA}",
    inputs: {
      "mode" => expression("github.event.client_payload.mode || 'rebuild'"),
      "kandelo-ref" => CURRENT_KANDELO_CONSUMER_SHA,
      "tap-ref" => expression("github.event.client_payload.tap_sha"),
      "formulae" => expression("github.event.client_payload.formulae"),
      "arches" => expression("github.event.client_payload.arches || 'wasm32'"),
      "release-tag" => expression("github.event.client_payload.release_tag || ''"),
      "expected-cache-keys" => expression(
        "github.event.client_payload.expected_cache_keys || ''"
      ),
      "package-generation-wasm32" => PACKAGE_GENERATION_WASM32_TAG,
      "force" => expression("github.event.client_payload.force || false"),
      "rollback-reason" => expression("github.event.client_payload.rollback_reason || ''"),
      "rollback-ref" => expression("github.event.client_payload.rollback_ref || ''"),
      "deleted-package-url" => expression(
        "github.event.client_payload.deleted_package_url || ''"
      ),
      "deletion-reason" => expression("github.event.client_payload.deletion_reason || ''"),
    }.freeze,
  },
  "main-shell-mirror" => {
    path: File.join(WORKFLOW_ROOT, "publish-main-shell-mirror.yml"),
    name: "Publish Homebrew main-shell mirror",
    event: "publish-homebrew-main-shell-mirror",
    job: "publish",
    permissions: MAIN_SHELL_MIRROR_PERMISSIONS,
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-main-shell-mirror-publish.yml@#{MAIN_SHELL_MIRROR_KANDELO_SHA}",
    inputs: {
      "kandelo-ref" => MAIN_SHELL_MIRROR_KANDELO_SHA,
      "tap-catalog-ref" => MAIN_SHELL_MIRROR_TAP_CATALOG_SHA,
      "mirror-authority-ref" => MAIN_SHELL_MIRROR_AUTHORITY_SHA,
      "canary-ref" => MAIN_SHELL_MIRROR_CANARY_SHA,
    }.freeze,
  },
  "first-publication" => {
    path: File.join(WORKFLOW_ROOT, "repository-namespace-canary.yml"),
    name: "Publish first libyaml GHCR child",
    event: "publish-first-homebrew-child",
    job: "first-publication",
    permissions: FIRST_PUBLICATION_PERMISSIONS,
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-repository-namespace-canary.yml@#{FIRST_PUBLICATION_KANDELO_SHA}",
    inputs: {
      "kandelo-ref" => FIRST_PUBLICATION_KANDELO_SHA,
      "tap-ref" => expression("github.sha"),
      "formula" => "libyaml",
      "arch" => "wasm32",
      "dry-run-run-id" => expression(
        "github.event.client_payload.dry_run_run_id"
      ),
      "dry-run-run-attempt" => expression(
        "github.event.client_payload.dry_run_run_attempt"
      ),
      "dry-run-child-artifact-digest" => expression(
        "github.event.client_payload.dry_run_child_artifact_digest"
      ),
      "expected-child-manifest-digest" => expression(
        "github.event.client_payload.expected_child_manifest_digest"
      ),
    }.freeze,
  },
}.freeze

def caller_specs_for_sha(kandelo_sha)
  specs = deep_copy(CALLER_SPECS)
  specs.fetch("publish")[:reusable] =
    "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@#{kandelo_sha}"
  specs.fetch("dry-run")[:reusable] =
    "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@#{kandelo_sha}"
  specs.fetch("maintenance")[:reusable] =
    "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-maintenance.yml@#{kandelo_sha}"
  inputs = deep_copy(VFS_PUBLISH_INPUTS)
  inputs["kandelo-ref"] = kandelo_sha
  specs.fetch("publish")[:inputs] = inputs.freeze
  maintenance_inputs = deep_copy(specs.fetch("maintenance").fetch(:inputs))
  maintenance_inputs["kandelo-ref"] = kandelo_sha
  specs.fetch("maintenance")[:inputs] = maintenance_inputs.freeze
  specs.fetch("publish").delete(:secrets)
  specs.freeze
end

def pat_caller_specs_for_sha(kandelo_sha)
  specs = deep_copy(caller_specs_for_sha(kandelo_sha))
  specs.fetch("publish")[:inputs] = PAT_PUBLISH_INPUTS
  specs.fetch("publish")[:secrets] = PAT_PUBLISH_SECRETS
  specs.freeze
end

CALLER_PROFILES = {
  "current" => CALLER_SPECS,
}.freeze

BASE_MATERIALIZE_RUN = <<~'BASH'
  set -euo pipefail
  [[ "$HEAD_REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || {
    echo "::error::invalid pull-request head repository"; exit 2;
  }
  [[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || {
    echo "::error::invalid pull-request head SHA"; exit 2;
  }

  candidate_root="$RUNNER_TEMP/tap-contract-candidate"
  rm -rf "$candidate_root"
  tree_json="$RUNNER_TEMP/tap-contract-tree.json"
  gh api --method GET \
    -H "Accept: application/vnd.github+json" \
    "repos/${HEAD_REPOSITORY}/git/trees/${HEAD_SHA}?recursive=1" \
    >"$tree_json"
  jq -e '
    .truncated == false and
    ([.tree[] |
      select(.path | startswith(".github/workflows/")) |
      select(.type != "tree") |
      {path, mode, type}] | sort_by(.path)) == [
        {path: ".github/workflows/base-contract-checks.yml", mode: "100644", type: "blob"},
        {path: ".github/workflows/contract-checks.yml", mode: "100644", type: "blob"},
        {path: ".github/workflows/dry-run-bottles.yml", mode: "100644", type: "blob"},
        {path: ".github/workflows/maintain-bottles.yml", mode: "100644", type: "blob"},
        {path: ".github/workflows/prefix-campaign-bottles.yml", mode: "100644", type: "blob"},
        {path: ".github/workflows/publish-bottles.yml", mode: "100644", type: "blob"},
        {path: ".github/workflows/publish-closed-selection.yml", mode: "100644", type: "blob"},
        {path: ".github/workflows/publish-main-shell-mirror.yml", mode: "100644", type: "blob"},
        {path: ".github/workflows/repository-namespace-canary.yml", mode: "100644", type: "blob"}
      ] and
    ([.tree[] |
      select(.path == "Kandelo/prefix-campaign-authority.json") |
      {path, mode, type}]) == [
        {path: "Kandelo/prefix-campaign-authority.json", mode: "100644", type: "blob"}
      ] and
    ([.tree[] |
      select(.path == "Kandelo/test-workflow-trust.rb") |
      {path, mode, type}]) == [
        {path: "Kandelo/test-workflow-trust.rb", mode: "100644", type: "blob"}
      ] and
    ([.tree[] |
      select(.path == "Kandelo/test-workflow-trust.sh") |
      {path, mode, type}]) == [
        {path: "Kandelo/test-workflow-trust.sh", mode: "100755", type: "blob"}
      ] and
    ([.tree[] |
      select(.path == "scripts/prefix-campaign-controller.py") |
      {path, mode, type}]) == [
        {path: "scripts/prefix-campaign-controller.py", mode: "100644", type: "blob"}
      ] and
    ([.tree[] |
      select(.path == "scripts/prefix-campaign-source.py") |
      {path, mode, type}]) == [
        {path: "scripts/prefix-campaign-source.py", mode: "100644", type: "blob"}
      ] and
    ([.tree[] |
      select(.path == "scripts/test_prefix_campaign_controller.py") |
      {path, mode, type}]) == [
        {path: "scripts/test_prefix_campaign_controller.py", mode: "100644", type: "blob"}
      ] and
    ([.tree[] |
      select(.path == "scripts/test_prefix_campaign_source.py") |
      {path, mode, type}]) == [
        {path: "scripts/test_prefix_campaign_source.py", mode: "100644", type: "blob"}
      ]
  ' "$tree_json" >/dev/null || {
    echo "::error::candidate workflow or trust-root file set changed"; exit 2;
  }
  expected_manifest_blob="$(
    git rev-parse HEAD:Kandelo/campaigns/prefix-v1/manifest.json
  )"
  expected_source_tree="$(
    git rev-parse HEAD:Kandelo/campaigns/prefix-v1/source
  )"
  jq -e \
    --arg manifest_blob "$expected_manifest_blob" \
    --arg source_tree "$expected_source_tree" '
      ([.tree[] |
        select(.path ==
          "Kandelo/campaigns/prefix-v1/manifest.json") |
        {mode, sha, type}]) == [
          {mode: "100644", sha: $manifest_blob, type: "blob"}
        ] and
      ([.tree[] |
        select(.path ==
          "Kandelo/campaigns/prefix-v1/source") |
        {mode, sha, type}]) == [
          {mode: "040000", sha: $source_tree, type: "tree"}
        ]
    ' "$tree_json" >/dev/null || {
      echo "::error::candidate prefix source authority changed"; exit 2;
    }
  paths=(
    .github/workflows/base-contract-checks.yml
    .github/workflows/contract-checks.yml
    .github/workflows/dry-run-bottles.yml
    .github/workflows/maintain-bottles.yml
    .github/workflows/prefix-campaign-bottles.yml
    .github/workflows/publish-bottles.yml
    .github/workflows/publish-closed-selection.yml
    .github/workflows/publish-main-shell-mirror.yml
    .github/workflows/repository-namespace-canary.yml
    Kandelo/prefix-campaign-authority.json
    Kandelo/test-workflow-trust.rb
    Kandelo/test-workflow-trust.sh
    scripts/prefix-campaign-controller.py
    scripts/prefix-campaign-source.py
    scripts/test_prefix_campaign_controller.py
    scripts/test_prefix_campaign_source.py
  )
  for path in "${paths[@]}"; do
    destination="$candidate_root/$path"
    mkdir -p "$(dirname "$destination")"
    gh api --method GET \
      -H "Accept: application/vnd.github.raw+json" \
      "repos/${HEAD_REPOSITORY}/contents/${path}?ref=${HEAD_SHA}" \
      >"$destination"
  done

  for path in "${paths[@]}"; do
    cmp -s "$GITHUB_WORKSPACE/$path" "$candidate_root/$path" || {
      echo "::error::base-owned trust contract changed: $path"; exit 2;
    }
  done
  printf 'KANDELO_TAP_CONTRACT_CANDIDATE=%s\n' "$candidate_root" >>"$GITHUB_ENV"
BASH

def check_workflow_file_set
  actual = Dir.children(WORKFLOW_ROOT).sort
  check(actual == EXPECTED_WORKFLOW_FILES,
        "workflow file set changed: expected #{EXPECTED_WORKFLOW_FILES.inspect}, got #{actual.inspect}")
end

def check_caller(workflow, spec, label)
  expected_top_level_keys = %w[jobs name on]
  expected_top_level_keys << "run-name" if spec.key?(:run_name)
  check(normalized_keys(workflow, label).sort == expected_top_level_keys.sort,
        "#{label} has unexpected top-level configuration")
  check(workflow["name"] == spec.fetch(:name), "#{label} name changed")
  if spec.key?(:run_name)
    check(workflow["run-name"] == spec.fetch(:run_name),
          "#{label} run identity changed")
  end
  check(workflow_events(workflow) == {
    "repository_dispatch" => { "types" => [spec.fetch(:event)] },
  }, "#{label} must expose only its reviewed repository_dispatch event")

  jobs = workflow["jobs"]
  check(jobs.is_a?(Hash) && jobs.keys == [spec.fetch(:job)],
        "#{label} has an unexpected job set")
  job = jobs.fetch(spec.fetch(:job))
  expected_secrets = spec.fetch(:secrets, {})
  expected_job_keys = %w[permissions uses with]
  expected_job_keys << "secrets" unless expected_secrets.empty?
  check(normalized_keys(job, "#{label} job").sort == expected_job_keys.sort,
        "#{label} caller job is not data-only")
  check(exact_permissions?(job["permissions"], spec.fetch(:permissions, CALLER_PERMISSIONS)),
        "#{label} permission ceiling changed")
  check(job["uses"] == spec.fetch(:reusable), "#{label} reusable workflow target changed")
  check(job["with"] == spec.fetch(:inputs), "#{label} caller inputs changed")
  check(job.fetch("secrets", {}) == expected_secrets, "#{label} caller secrets changed")

  check(values_for_key(workflow, "uses") == [spec.fetch(:reusable)],
        "#{label} executable workflow set changed")
  %w[run steps env defaults].each do |key|
    check(values_for_key(workflow, key).empty?, "#{label} contains caller-local #{key}")
  end
  expected_secret_nodes = expected_secrets.empty? ? [] : [expected_secrets]
  check(values_for_key(workflow, "secrets") == expected_secret_nodes,
        "#{label} may pass only its reviewed named secrets")
end

def check_closed_selection_caller(workflow)
  label = "closed-selection workflow"
  check(normalized_keys(workflow, label).sort == %w[jobs name on],
        "#{label} has unexpected top-level configuration")
  check(workflow["name"] == "Publish Homebrew closed selection",
        "#{label} name changed")
  check(
    workflow_events(workflow) == {
      "workflow_dispatch" => {
        "inputs" => {
          "selection_plan" => {
            "description" =>
              "Canonical compact JSON selecting one complete closure",
            "required" => true,
            "type" => "string",
          },
          "selection_plan_sha256" => {
            "description" => "SHA-256 of the canonical selection plan",
            "required" => true,
            "type" => "string",
          },
        },
      },
    },
    "#{label} dispatch inputs changed"
  )

  jobs = workflow["jobs"]
  check(jobs.is_a?(Hash) && jobs.keys == ["publish"],
        "#{label} has an unexpected job set")
  job = jobs.fetch("publish")
  check(normalized_keys(job, "#{label} job").sort ==
          %w[permissions uses with],
        "#{label} job is not data-only")
  check(exact_permissions?(job["permissions"], CLOSED_SELECTION_PERMISSIONS),
        "#{label} permission ceiling changed")
  reusable =
    "Automattic/kandelo/.github/workflows/" \
    "reusable-homebrew-closed-selection-publish.yml@" \
    "#{CLOSED_SELECTION_KANDELO_SHA}"
  check(job["uses"] == reusable,
        "#{label} reusable workflow target changed")
  check(
    job["with"] == {
      "kandelo-ref" => CLOSED_SELECTION_KANDELO_SHA,
      "selection-plan" => expression("inputs.selection_plan"),
      "selection-plan-sha256" =>
        expression("inputs.selection_plan_sha256"),
    },
    "#{label} forwarding contract changed"
  )
  check(values_for_key(workflow, "uses") == [reusable],
        "#{label} executable workflow set changed")
  %w[run steps env defaults secrets].each do |key|
    check(values_for_key(workflow, key).empty?,
          "#{label} contains caller-local #{key}")
  end
end

def check_prefix_campaign_authority(authority)
  label = "prefix-campaign authority"
  check(authority.keys.sort == %w[
          campaign_release
          kandelo_commit
          kandelo_repository
          kind
          package_generations
          release_tag
          reusable_workflow_commit
          schema
          source_tap_commit
          source_tap_name
          source_tap_repository
          state
          target_source
        ], "#{label} field set changed")
  check(authority["schema"] == 2, "#{label} schema changed")
  check(
    authority["kind"] ==
      "kandelo-homebrew-prefix-campaign-caller-authority",
    "#{label} kind changed"
  )
  check(authority["kandelo_repository"] == "Automattic/kandelo",
        "#{label} Kandelo repository changed")
  check(
    authority["source_tap_repository"] ==
      "kandelo-dev/homebrew-tap-core" &&
      authority["source_tap_name"] == "kandelo-dev/tap-core",
    "#{label} source tap changed"
  )
  check(authority["release_tag"] == "bottles-abi-v42",
        "#{label} release tag changed")
  check(authority["state"].is_a?(String) &&
          authority["state"].match?(/\A(?:inert|active)\z/),
        "#{label} state changed")

  target_source = authority["target_source"]
  check(target_source.is_a?(Hash) &&
          target_source.keys.sort == %w[
            manifest_path
            manifest_sha256
            source_root
            source_tree_git_oid
            target_tree_git_oid
          ], "#{label} target source changed")
  check(
    target_source["manifest_path"] ==
      "Kandelo/campaigns/prefix-v1/manifest.json" &&
      target_source["source_root"] ==
        "Kandelo/campaigns/prefix-v1/source",
    "#{label} target source paths changed"
  )
  check(
    target_source["manifest_sha256"].is_a?(String) &&
      target_source["manifest_sha256"].match?(/\A[0-9a-f]{64}\z/) &&
      !target_source["manifest_sha256"].match?(/\A0+\z/),
    "#{label} target manifest is not content-addressed"
  )
  %w[source_tree_git_oid target_tree_git_oid].each do |name|
    check(
      target_source[name].is_a?(String) &&
        target_source[name].match?(/\A[0-9a-f]{40}\z/) &&
        !target_source[name].match?(/\A0+\z/),
      "#{label} #{name} is not an exact tree"
    )
  end

  campaign = authority["campaign_release"]
  check(campaign.is_a?(Hash) &&
          campaign.keys.sort == %w[repository tag],
        "#{label} campaign release changed")
  check(campaign["repository"] == "kandelo-dev/homebrew-tap-core",
        "#{label} campaign repository changed")
  check(
    campaign["tag"].is_a?(String) && campaign["tag"].match?(
      /\Ahomebrew-prefix-campaign-sha256-[0-9a-f]{64}\z/
    ),
    "#{label} campaign tag is not a full content identity"
  )

  generations = authority["package_generations"]
  check(generations.is_a?(Hash) &&
          generations.keys == %w[rootfs_wasm32],
        "#{label} package generation set changed")
  generation_patterns = {
    "rootfs_wasm32" =>
      /\Apackage-generation-rootfs-wasm32-abi-v42-sha256-[0-9a-f]{64}\z/,
  }
  generation_patterns.each do |name, pattern|
    check(generations[name].is_a?(String) &&
            generations[name].match?(pattern),
          "#{label} #{name} is not an exact ABI 42 content tag")
  end

  commits = %w[
    kandelo_commit
    reusable_workflow_commit
    source_tap_commit
  ]
  commits.each do |name|
    check(authority[name].is_a?(String) &&
            authority[name].match?(/\A[0-9a-f]{40}\z/),
          "#{label} #{name} is not an exact commit")
  end
  check(
    authority["reusable_workflow_commit"] ==
      authority["kandelo_commit"],
    "#{label} splits executor and reusable workflow commits"
  )

  identities = [
    authority["kandelo_commit"],
    authority["source_tap_commit"],
    campaign["tag"],
    *generations.values,
  ]
  zero_identities = identities.count do |identity|
    identity.scan(/[0-9a-f]+/).last.match?(/\A0+\z/)
  end
  if authority["state"] == "inert"
    check(zero_identities == identities.length,
          "#{label} inert placeholders are mixed with live authority")
  else
    check(zero_identities.zero?,
          "#{label} active state retains inert authority")
  end
end

def check_prefix_campaign_workflow(workflow, authority)
  label = "prefix-campaign workflow"
  check(normalized_keys(workflow, label).sort == %w[jobs name on],
        "#{label} has unexpected top-level configuration")
  check(workflow["name"] == "Publish prefix-campaign bottle",
        "#{label} name changed")
  check(workflow_events(workflow) == {
    "repository_dispatch" => {
      "types" => ["publish-prefix-campaign-bottle"],
    },
  }, "#{label} event changed")

  jobs = workflow["jobs"]
  check(jobs.is_a?(Hash) && jobs.keys == %w[
          admit
          publish-rootfs
          build-bootstrap-rootfs
          publish-first-child
          publish-bootstrap-rootfs
          seal-handoff
        ], "#{label} job set changed")
  check(exact_permissions?(
          jobs.dig("admit", "permissions"),
          { "contents" => "read" }
        ), "#{label} admission permissions changed")
  check(
    jobs.dig("admit", "outputs", "admission-kind") ==
      expression("steps.admit.outputs.admission-kind"),
    "#{label} does not expose the admitted destination kind"
  )
  check(
    jobs.dig("admit", "outputs", "arch") ==
      expression("steps.admit.outputs.arch"),
    "#{label} does not expose the admitted architecture"
  )
  # WHY: reusable-workflow validation checks the caller ceiling before job
  # conditions. The reusable declares skipped finalization/release writers,
  # so every call to it must permit their declared contents scope.
  publish_permissions = {
    "actions" => "read",
    "contents" => "write",
    "packages" => "write",
  }
  publisher_conditions = {
    "publish-rootfs" => expression([
      "needs.admit.outputs.disposition == 'build'",
      "&& needs.admit.outputs.generation-kind == 'rootfs-wasm32'",
      "&& needs.admit.outputs.admission-kind == 'anonymous-absence'",
    ].join(" ")),
    "build-bootstrap-rootfs" => expression([
      "needs.admit.outputs.disposition == 'build'",
      "&& needs.admit.outputs.generation-kind == 'rootfs-wasm32'",
      "&& needs.admit.outputs.admission-kind ==",
      "'first-package-namespace-bootstrap-required'",
    ].join(" ")),
    "publish-bootstrap-rootfs" => expression([
      "always() && !cancelled()",
      "&& needs.admit.result == 'success'",
      "&& needs.publish-first-child.result == 'success'",
      "&& needs.admit.outputs.disposition == 'build'",
      "&& needs.admit.outputs.generation-kind == 'rootfs-wasm32'",
      "&& needs.admit.outputs.admission-kind ==",
      "'first-package-namespace-bootstrap-required'",
    ].join(" ")),
  }
  %w[
    publish-rootfs
    build-bootstrap-rootfs
    publish-bootstrap-rootfs
  ].each do |name|
    job = jobs.fetch(name)
    reusable = [
      "Automattic/kandelo/.github/workflows/",
      "reusable-homebrew-bottle-publish.yml@",
      authority["reusable_workflow_commit"],
    ].join
    check(job["uses"] == reusable,
          "#{label} #{name} reusable target changed")
    check(
      compact_expression(job["if"]) == publisher_conditions[name],
      "#{label} #{name} route changed"
    )
    expected_needs = if name == "publish-bootstrap-rootfs"
      ["admit", "publish-first-child"]
    else
      ["admit"]
    end
    check(job["needs"] == expected_needs,
          "#{label} #{name} dependencies changed")
    expected_permissions = if name == "build-bootstrap-rootfs"
      {
        "actions" => "read",
        "contents" => "write",
        "packages" => "write",
      }
    else
      publish_permissions
    end
    check(exact_permissions?(job["permissions"], expected_permissions),
          "#{label} #{name} permissions changed")
    inputs = job["with"]
    check(inputs.is_a?(Hash), "#{label} #{name} inputs changed")
    check(inputs["defer-tap-finalization"] == true,
          "#{label} #{name} may finalize tap Git")
    check(inputs["require-vfs-acceptance"] == false,
          "#{label} #{name} may run per-Formula VFS acceptance")
    expected_dry_run = name == "build-bootstrap-rootfs"
    check(inputs["dry-run"] == expected_dry_run,
          "#{label} #{name} dry-run mode changed")
    check(inputs["force"] == true,
          "#{label} #{name} may accept a cached campaign build")
    check(
      inputs["prefix-campaign-tag"] ==
        expression("needs.admit.outputs.campaign-tag") &&
        inputs["prefix-campaign-dependencies"] ==
        expression("needs.admit.outputs.dependencies"),
      "#{label} #{name} campaign authority changed"
    )
    check(
      inputs["formulae"] ==
        expression("needs.admit.outputs.formula") &&
        inputs["arches"] ==
        expression("needs.admit.outputs.arches") &&
        inputs["tap-ref"] ==
        expression("needs.admit.outputs.source-tap-commit"),
      "#{label} #{name} task selection changed"
    )
  end
  first_child = jobs["publish-first-child"]
  first_child_reusable = [
    "Automattic/kandelo/.github/workflows/",
    "reusable-homebrew-prefix-first-child-publish.yml@",
    authority["reusable_workflow_commit"],
  ].join
  check(
    first_child["uses"] == first_child_reusable,
    "#{label} first-child reusable target changed"
  )
  check(
    compact_expression(first_child["if"]) == expression([
      "always() && !cancelled()",
      "&& needs.admit.result == 'success'",
      "&& needs.build-bootstrap-rootfs.result == 'success'",
      "&& needs.admit.outputs.disposition == 'build'",
      "&& needs.admit.outputs.generation-kind == 'rootfs-wasm32'",
      "&& needs.admit.outputs.admission-kind ==",
      "'first-package-namespace-bootstrap-required'",
    ].join(" ")) &&
      first_child["needs"] == [
        "admit", "build-bootstrap-rootfs",
      ],
    "#{label} first-child route is not bootstrap-only"
  )
  check(
    exact_permissions?(
      first_child["permissions"], {
        "actions" => "read",
        "contents" => "read",
        "packages" => "write",
      }
    ),
    "#{label} first-child permissions changed"
  )
  first_child_inputs = first_child["with"]
  check(
    first_child_inputs.is_a?(Hash) &&
      first_child_inputs.keys.sort == %w[
        arch
        formula
        kandelo-ref
        kandelo-repository
        prefix-campaign-dependencies
        prefix-campaign-tag
        release-tag
        tap-name
        tap-ref
        tap-repository
      ] &&
      first_child_inputs["formula"] ==
        expression("needs.admit.outputs.formula") &&
      first_child_inputs["arch"] ==
        expression("needs.admit.outputs.arch") &&
      first_child_inputs["kandelo-ref"] ==
        expression("needs.admit.outputs.kandelo-commit") &&
      first_child_inputs["tap-ref"] ==
        expression("needs.admit.outputs.source-tap-commit") &&
      first_child_inputs["prefix-campaign-tag"] ==
        expression("needs.admit.outputs.campaign-tag") &&
      first_child_inputs["prefix-campaign-dependencies"] ==
        expression("needs.admit.outputs.dependencies"),
    "#{label} first-child inputs changed"
  )
  check(exact_permissions?(
          jobs.dig("seal-handoff", "permissions"),
          { "actions" => "read", "contents" => "write" }
        ), "#{label} release permissions changed")
  check(
    jobs.dig("seal-handoff", "needs") == [
      "admit",
      "publish-rootfs",
      "build-bootstrap-rootfs",
      "publish-first-child",
      "publish-bootstrap-rootfs",
    ],
    "#{label} release dependencies changed"
  )
  check(
    compact_expression(jobs.dig("seal-handoff", "if")) == expression([
      "always() && !cancelled()",
      "&& needs.admit.result == 'success'",
      "&& ( ( needs.admit.outputs.disposition == 'build'",
      "&& needs.admit.outputs.generation-kind == 'rootfs-wasm32'",
      "&& ( ( needs.admit.outputs.admission-kind == 'anonymous-absence'",
      "&& needs.publish-rootfs.result == 'success'",
      "&& needs.build-bootstrap-rootfs.result == 'skipped'",
      "&& needs.publish-first-child.result == 'skipped'",
      "&& needs.publish-bootstrap-rootfs.result == 'skipped' )",
      "|| ( needs.admit.outputs.admission-kind ==",
      "'first-package-namespace-bootstrap-required'",
      "&& needs.publish-rootfs.result == 'skipped'",
      "&& needs.build-bootstrap-rootfs.result == 'success'",
      "&& needs.publish-first-child.result == 'success'",
      "&& needs.publish-bootstrap-rootfs.result == 'success' ) ) )",
      "|| ( needs.admit.outputs.disposition == 'reuse'",
      "&& needs.publish-rootfs.result == 'skipped'",
      "&& needs.build-bootstrap-rootfs.result == 'skipped'",
      "&& needs.publish-first-child.result == 'skipped'",
      "&& needs.publish-bootstrap-rootfs.result == 'skipped'",
      "&& needs.admit.outputs.generation-kind == 'none' ) )",
    ].join(" ")),
    "#{label} release route changed"
  )

  reusable = [
    "Automattic/kandelo/.github/workflows/",
    "reusable-homebrew-bottle-publish.yml@",
    authority["reusable_workflow_commit"],
  ].join
  expected_uses = [
    CHECKOUT_ACTION,
    CHECKOUT_ACTION,
    CHECKOUT_ACTION,
    reusable,
    reusable,
    first_child_reusable,
    reusable,
    CHECKOUT_ACTION,
    CHECKOUT_ACTION,
    CHECKOUT_ACTION,
    CHECKOUT_ACTION,
    DOWNLOAD_ACTION,
    NIX_INSTALLER_ACTION,
    UPLOAD_ACTION,
  ]
  check(values_for_key(workflow, "uses") == expected_uses,
        "#{label} executable dependency set changed")
  source_checkouts = jobs.values.flat_map do |job|
    job.fetch("steps", []).select do |step|
      step["name"] == "Checkout exact campaign source tap"
    end
  end
  check(
    source_checkouts.map { |step| step["with"] } == [
      {
        "repository" => "kandelo-dev/homebrew-tap-core",
        "ref" => expression("steps.authority.outputs.source-tap-commit"),
        "path" => "source-tap",
        "fetch-depth" => 0,
        "persist-credentials" => false,
      },
      {
        "repository" => "kandelo-dev/homebrew-tap-core",
        "ref" =>
          expression("needs.admit.outputs.source-tap-commit"),
        "path" => "source-tap",
        "fetch-depth" => 0,
        "persist-credentials" => false,
      },
    ],
    "#{label} source checkout is not exact full history"
  )
  check(values_for_key(workflow, "secrets").empty?,
        "#{label} passes repository secrets")
  controller_token = {
    "GH_TOKEN" => expression("github.token"),
  }
  check(values_for_key(workflow, "env") == [
          controller_token,
          controller_token,
          controller_token,
        ], "#{label} credential boundary changed")

  admit_step = jobs.dig("admit", "steps").find do |step|
    step["name"] == "Admit one exact campaign task"
  end
  check(
    admit_step&.fetch("env", nil) == controller_token,
    "#{label} internal campaign read lacks bounded GitHub authority"
  )

  historical_checkout = jobs.dig("seal-handoff", "steps").find do |step|
    step["name"] == "Checkout exact historical tap for reuse"
  end
  check(
    historical_checkout&.dig("if") ==
      expression("needs.admit.outputs.disposition == 'reuse'") &&
      historical_checkout&.dig("with") == {
        "repository" => "kandelo-dev/homebrew-tap-core",
        "ref" => expression("needs.admit.outputs.old-tap-commit"),
        "path" => "old-tap",
        "fetch-depth" => 0,
        "persist-credentials" => false,
      },
    "#{label} historical reuse checkout does not retain Formula history"
  )

  seal_steps = jobs.dig("seal-handoff", "steps")
  check(seal_steps.is_a?(Array), "#{label} release steps changed")
  prepare_step = seal_steps.find do |step|
    step["name"] == "Derive and prepare immutable Formula handoff"
  end
  nix_step = seal_steps.find do |step|
    step["name"] == "Install Nix for handoff derivation"
  end
  check(
    nix_step&.fetch("uses", nil) == NIX_INSTALLER_ACTION &&
      nix_step["with"] == { "github-token" => "" } &&
      seal_steps.index(nix_step) < seal_steps.index(prepare_step),
    "#{label} handoff derivation lacks its declared Nix environment"
  )
  check(
    prepare_step.is_a?(Hash) &&
      prepare_step["env"] == controller_token &&
      prepare_step["run"].is_a?(String) &&
      prepare_step["run"].include?("cd kandelo\n") &&
      prepare_step["run"].include?(
        'bash scripts/dev-shell.sh "${controller[@]}" '
      ) &&
      prepare_step["run"].include?(
        'controller+=(prepare-reuse)'
      ) &&
      prepare_step["run"].include?(
        '--old-tap-root "$GITHUB_WORKSPACE/old-tap"'
      ),
    "#{label} handoff derivation bypasses the Kandelo dev shell"
  )
  verify_step = seal_steps.find do |step|
    step["name"] == "Revalidate the release without credentials"
  end
  check(
    verify_step.is_a?(Hash) &&
      !verify_step.key?("env") &&
      verify_step["run"].is_a?(String) &&
      verify_step["run"].include?("verify-release"),
    "#{label} public release readback can inherit credentials"
  )
  downloads = seal_steps.select do |step|
    step["uses"] == DOWNLOAD_ACTION
  end
  check(downloads.map { |step| step.dig("with", "name") } == [
          "homebrew-publish-handoff-" \
          "#{expression('needs.admit.outputs.formula')}-" \
          "#{expression('needs.admit.outputs.arch')}-" \
          "attempt-#{expression('github.run_attempt')}",
        ], "#{label} publication artifact names changed")
  check(downloads.map { |step| step.dig("with", "path") } == [
          "#{expression('runner.temp')}/campaign-publications/" \
          "#{expression('needs.admit.outputs.arch')}",
        ], "#{label} publication artifact paths changed")
  evidence_step = seal_steps.find do |step|
    step["uses"] == UPLOAD_ACTION
  end
  check(
    evidence_step&.dig("with", "name") ==
      "prefix-campaign-controller-" \
      "#{expression('needs.admit.outputs.formula')}-" \
      "#{expression('needs.admit.outputs.arch')}-" \
      "attempt-#{expression('github.run_attempt')}",
    "#{label} controller evidence is not architecture-scoped"
  )
  publish_step = seal_steps.find do |step|
    step["name"] == "Publish immutable Formula handoff"
  end
  check(publish_step.is_a?(Hash),
        "#{label} immutable release step changed")
  publish_run = publish_step["run"]
  check(publish_run.is_a?(String),
        "#{label} immutable release command changed")
  expected_publish_run = [
    "set -euo pipefail",
    "# WHY: credentials enter only after all task data is sealed.",
    "# A campaign may outlive later Kandelo merges. Each release",
    "# therefore re-proves that its sealed source remains on",
    "# protected main.",
    "bash kandelo/scripts/publish-immutable-github-release.sh \\",
    "  --manifest \\",
    "    \"$RUNNER_TEMP/prepared-handoff-release/" \
      "release-manifest.json\" \\",
    "  --asset-root \\",
    "    \"$RUNNER_TEMP/prepared-handoff-release/assets\" \\",
    "  --lock-root source-tap \\",
    "  --receipt \"$RUNNER_TEMP/publish-receipt.json\" \\",
    "  --kandelo-main-contains-sha \\",
    "    \"#{expression(
      'needs.admit.outputs.kandelo-commit'
    )}\" \\",
    "  --target-main-contains-sha \\",
    "    \"#{expression(
      'needs.admit.outputs.source-tap-commit'
    )}\"",
  ].join("\n") + "\n"
  check(
    publish_run == expected_publish_run,
    "#{label} immutable release authority changed"
  )
end

def caller_profile_errors(callers, specs)
  errors = []
  specs.each do |key, spec|
    begin
      check_caller(callers.fetch(key), spec, "#{key} workflow")
    rescue KeyError, RuntimeError => e
      errors << "#{key}: #{e.message}"
    end
  end
  errors
end

def check_caller_profile(callers, profiles = CALLER_PROFILES)
  results = profiles.to_h do |name, specs|
    [name, caller_profile_errors(callers, specs)]
  end
  matches = results.select { |_name, errors| errors.empty? }.keys
  details = results.map { |name, errors| "#{name}=[#{errors.join('; ')}]" }.join(", ")
  check(matches.length == 1, "caller set does not match one exact profile: #{details}")
  matches.fetch(0)
end

def callers_for_specs(callers, specs)
  result = deep_copy(callers)
  specs.each do |key, spec|
    job = result.fetch(key).fetch("jobs").fetch(spec.fetch(:job))
    job["uses"] = spec.fetch(:reusable)
    job["with"] = spec.fetch(:inputs)
    expected_secrets = spec.fetch(:secrets, {})
    if expected_secrets.empty?
      job.delete("secrets")
    else
      job["secrets"] = expected_secrets
    end
  end
  result
end

def check_contract_workflow(workflow)
  label = "contract-check workflow"
  check(normalized_keys(workflow, label).sort == %w[jobs name on permissions],
        "#{label} has unexpected top-level configuration")
  check(workflow["name"] == "Tap contract checks", "#{label} name changed")

  watched_paths = [
    ".github/workflows/**",
    "Kandelo/campaigns/prefix-v1/**",
    "Kandelo/prefix-campaign-authority.json",
    "Kandelo/test-workflow-trust.sh",
    "Kandelo/test-workflow-trust.rb",
    "scripts/prefix-campaign-controller.py",
    "scripts/prefix-campaign-source.py",
    "scripts/test_prefix_campaign_controller.py",
    "scripts/test_prefix_campaign_source.py",
  ]
  check(workflow_events(workflow) == {
    "pull_request" => {},
    "push" => { "branches" => ["main"], "paths" => watched_paths },
  }, "#{label} triggers changed")
  check(exact_permissions?(workflow["permissions"], { "contents" => "read" }),
        "#{label} permissions are not exact")

  jobs = workflow["jobs"]
  check(jobs.is_a?(Hash) && jobs.keys == ["publisher-trust"],
        "#{label} has an unexpected job set")
  expected_steps = [
    {
      "uses" => CHECKOUT_ACTION,
      "with" => {
        "fetch-depth" => 0,
        "persist-credentials" => false,
      },
    },
    {
      "uses" => RUBY_ACTION,
      "with" => { "ruby-version" => "3.4" },
    },
    {
      "name" => "Validate publisher trust boundaries",
      "run" => "bash Kandelo/test-workflow-trust.sh",
    },
    {
      "name" => "Exercise prefix-campaign controller",
      "run" =>
        "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest " \
        "scripts/test_prefix_campaign_controller.py",
    },
    {
      "name" => "Verify inert prefix-campaign source",
      "run" =>
        "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest " \
        "scripts/test_prefix_campaign_source.py",
    },
  ]
  check(jobs.fetch("publisher-trust") == {
    "runs-on" => "ubuntu-latest",
    "steps" => expected_steps,
  }, "#{label} job execution contract changed")
  check(values_for_key(workflow, "uses") == [CHECKOUT_ACTION, RUBY_ACTION],
        "#{label} action set or pins changed")
  check(values_for_key(workflow, "secrets").empty?, "#{label} passes repository secrets")
end

def check_base_contract_workflow(workflow)
  label = "base-controlled contract-check workflow"
  check(normalized_keys(workflow, label).sort == %w[jobs name on permissions],
        "#{label} has unexpected top-level configuration")
  check(workflow["name"] == "Base-controlled tap contract checks", "#{label} name changed")
  check(workflow_events(workflow) == {
    "pull_request_target" => { "branches" => ["main"] },
  },
        "#{label} triggers changed")
  check(exact_permissions?(workflow["permissions"], { "contents" => "read" }),
        "#{label} permissions are not exact")

  jobs = workflow["jobs"]
  check(jobs.is_a?(Hash) && jobs.keys == ["publisher-trust-base"],
        "#{label} has an unexpected job set")
  expected_steps = [
    {
      "uses" => CHECKOUT_ACTION,
      "with" => {
        "ref" => expression("github.event.pull_request.base.sha"),
        "persist-credentials" => false,
      },
    },
    {
      "uses" => RUBY_ACTION,
      "with" => { "ruby-version" => "3.4" },
    },
    {
      "name" => "Materialize candidate contracts as inert data",
      "shell" => "bash",
      "env" => {
        "GH_TOKEN" => expression("github.token"),
        "HEAD_REPOSITORY" => expression("github.event.pull_request.head.repo.full_name"),
        "HEAD_SHA" => expression("github.event.pull_request.head.sha"),
      },
      "run" => BASE_MATERIALIZE_RUN,
    },
    {
      "name" => "Validate candidate with the base-owned parser",
      "shell" => "bash",
      "run" => 'ruby Kandelo/test-workflow-trust.rb "$KANDELO_TAP_CONTRACT_CANDIDATE"',
    },
  ]
  check(jobs.fetch("publisher-trust-base") == {
    "runs-on" => "ubuntu-latest",
    "steps" => expected_steps,
  }, "#{label} job execution contract changed")
  check(values_for_key(workflow, "uses") == [CHECKOUT_ACTION, RUBY_ACTION],
        "#{label} action set or pins changed")
  check(values_for_key(workflow, "secrets").empty?, "#{label} passes repository secrets")
end

def self_test(
  callers,
  closed_selection,
  contract,
  base_contract,
  prefix_campaign,
  prefix_authority
)
  retired_pat_specs = pat_caller_specs_for_sha(RETIRED_PAT_KANDELO_WORKFLOW_SHA)
  previous_specs = caller_specs_for_sha(PREVIOUS_KANDELO_WORKFLOW_SHA)
  retired_specs = caller_specs_for_sha(RETIRED_KANDELO_WORKFLOW_SHA)
  arbitrary_specs = caller_specs_for_sha(SELF_TEST_KANDELO_WORKFLOW_SHA)
  test_profiles = { "current" => CALLER_SPECS }
  current_callers = callers_for_specs(callers, CALLER_SPECS)
  write_tuple_specs = caller_specs_for_sha(CURRENT_KANDELO_WORKFLOW_SHA)
  write_tuple_callers = callers_for_specs(callers, write_tuple_specs)
  retired_pat_callers = callers_for_specs(callers, retired_pat_specs)
  previous_callers = callers_for_specs(callers, previous_specs)
  retired_callers = callers_for_specs(callers, retired_specs)
  arbitrary_callers = callers_for_specs(callers, arbitrary_specs)
  check(check_caller_profile(current_callers, test_profiles) == "current",
        "current caller profile was not selected")
  check_closed_selection_caller(closed_selection)

  expect_rejection("a mutable closed-selection publisher") do
    mutated = deep_copy(closed_selection)
    mutated.dig("jobs", "publish")["uses"] =
      "Automattic/kandelo/.github/workflows/" \
      "reusable-homebrew-closed-selection-publish.yml@main"
    check_closed_selection_caller(mutated)
  end
  expect_rejection("split closed-selection publisher authority") do
    mutated = deep_copy(closed_selection)
    mutated.dig("jobs", "publish", "with")["kandelo-ref"] =
      "1" * 40
    check_closed_selection_caller(mutated)
  end
  expect_rejection("caller-local closed-selection code") do
    mutated = deep_copy(closed_selection)
    mutated.dig("jobs", "publish")["steps"] = [{ "run" => "true" }]
    check_closed_selection_caller(mutated)
  end
  expect_rejection("package authority on closed-selection publication") do
    mutated = deep_copy(closed_selection)
    mutated.dig("jobs", "publish", "permissions")["packages"] = "write"
    check_closed_selection_caller(mutated)
  end
  expect_rejection("a closed-selection secret") do
    mutated = deep_copy(closed_selection)
    mutated.dig("jobs", "publish")["secrets"] = "inherit"
    check_closed_selection_caller(mutated)
  end
  expect_rejection("an unbound closed-selection plan digest") do
    mutated = deep_copy(closed_selection)
    mutated.dig("jobs", "publish", "with")[
      "selection-plan-sha256"
    ] = "0" * 64
    check_closed_selection_caller(mutated)
  end

  if DRY_RUN_KANDELO_WORKFLOW_SHA != CURRENT_KANDELO_WORKFLOW_SHA
    expect_rejection("the fail-closed write publisher as the dry-run proof") do
      mutated = deep_copy(current_callers)
      mutated["dry-run"] = deep_copy(write_tuple_callers.fetch("dry-run"))
      check_caller_profile(mutated, test_profiles)
    end
  end
  expect_rejection("mixed current and arbitrary caller generations") do
    mutated = deep_copy(current_callers)
    mutated["publish"] = deep_copy(arbitrary_callers.fetch("publish"))
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("the retired PAT-backed caller generation") do
    check_caller_profile(retired_pat_callers, test_profiles)
  end
  expect_rejection("the previous complete caller generation") do
    check_caller_profile(previous_callers, test_profiles)
  end
  expect_rejection("the retired complete caller generation") do
    check_caller_profile(retired_callers, test_profiles)
  end
  expect_rejection("an arbitrary immutable Kandelo workflow pin") do
    check_caller_profile(arbitrary_callers, test_profiles)
  end
  expect_rejection("the publisher without its dispatch run identity") do
    mutated = deep_copy(current_callers.fetch("publish"))
    mutated.delete("run-name")
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("a changed dispatch run identity") do
    mutated = deep_copy(current_callers.fetch("publish"))
    mutated["run-name"] = [
      "Publish Kandelo bottles /",
      expression("github.event.client_payload.formulae"),
    ].join(" ")
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("the current publisher without VFS acceptance mapping") do
    mutated = deep_copy(current_callers)
    mutated.dig("publish", "jobs", "publish", "with").delete("require-vfs-acceptance")
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("a changed VFS acceptance mapping") do
    mutated = deep_copy(current_callers)
    mutated.dig("publish", "jobs", "publish", "with")["require-vfs-acceptance"] =
      expression("github.event.client_payload.require_vfs_acceptance")
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("the current publisher without its admitted rootfs generation") do
    mutated = deep_copy(current_callers)
    mutated.dig("publish", "jobs", "publish", "with").delete(
      "package-generation-wasm32"
    )
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("an event-selected write package generation") do
    mutated = deep_copy(current_callers)
    mutated.dig("publish", "jobs", "publish", "with")["package-generation-wasm32"] =
      expression("github.event.client_payload.package_generation_wasm32")
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("a fixed Kandelo source on the staging dry-run caller") do
    mutated = deep_copy(current_callers)
    mutated.dig("dry-run", "jobs", "dry-run", "with")["kandelo-ref"] =
      CURRENT_KANDELO_CONSUMER_SHA
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("an admitted production generation on the staging dry-run caller") do
    mutated = deep_copy(current_callers)
    mutated.dig("dry-run", "jobs", "dry-run", "with")["package-generation-wasm32"] =
      PACKAGE_GENERATION_WASM32_TAG
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("maintenance without its admitted rootfs generation") do
    mutated = deep_copy(current_callers)
    mutated.dig("maintenance", "jobs", "maintain", "with").delete(
      "package-generation-wasm32"
    )
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("VFS acceptance mapping on the dry-run caller") do
    mutated = deep_copy(current_callers)
    mutated.dig("dry-run", "jobs", "dry-run", "with")["require-vfs-acceptance"] =
      expression("github.event.client_payload.require_vfs_acceptance || false")
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("caller-local environment configuration") do
    mutated = deep_copy(current_callers.fetch("dry-run"))
    mutated["env"] = { "BASH_ENV" => "/tmp/untrusted" }
    check_caller(mutated, CALLER_SPECS.fetch("dry-run"), "dry-run workflow")
  end
  expect_rejection("caller-local executable steps") do
    mutated = deep_copy(current_callers.fetch("dry-run"))
    mutated.dig("jobs", "dry-run")["steps"] = [{ "run" => "true" }]
    check_caller(mutated, CALLER_SPECS.fetch("dry-run"), "dry-run workflow")
  end
  expect_rejection("secret inheritance") do
    mutated = deep_copy(current_callers.fetch("publish"))
    mutated.dig("jobs", "publish")["secrets"] = "inherit"
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("the retired package PAT mapping") do
    mutated = deep_copy(current_callers)
    publish = mutated.dig("publish", "jobs", "publish")
    publish["with"] = PAT_PUBLISH_INPUTS
    publish["secrets"] = PAT_PUBLISH_SECRETS
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("unexpected package secret") do
    mutated = deep_copy(current_callers.fetch("publish"))
    mutated.dig("jobs", "publish")["secrets"] = {
      "UNREVIEWED_TOKEN" => expression("secrets.UNREVIEWED_TOKEN"),
    }
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("package PAT fallback") do
    mutated = deep_copy(current_callers)
    mutated.dig("publish", "jobs", "publish", "with")["require-github-packages-token"] = false
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("package PAT owner drift") do
    mutated = deep_copy(current_callers)
    mutated.dig("publish", "jobs", "publish", "with")["github-packages-user"] =
      expression("github.actor")
    check_caller_profile(mutated, test_profiles)
  end
  expect_rejection("a package secret on first publication") do
    mutated = deep_copy(current_callers.fetch("first-publication"))
    mutated.dig("jobs", "first-publication")["secrets"] = {
      "HOMEBREW_GITHUB_PACKAGES_TOKEN" =>
        expression("secrets.HOMEBREW_GITHUB_PACKAGES_TOKEN"),
    }
    check_caller(
      mutated,
      CALLER_SPECS.fetch("first-publication"),
      "first-publication workflow"
    )
  end
  expect_rejection("an event-selected Kandelo ref on first publication") do
    mutated = deep_copy(current_callers.fetch("first-publication"))
    mutated.dig("jobs", "first-publication", "with")["kandelo-ref"] =
      expression("github.event.client_payload.kandelo_ref")
    check_caller(
      mutated,
      CALLER_SPECS.fetch("first-publication"),
      "first-publication workflow"
    )
  end
  expect_rejection("an event-selected Formula on first publication") do
    mutated = deep_copy(current_callers.fetch("first-publication"))
    mutated.dig("jobs", "first-publication", "with")["formula"] =
      expression("github.event.client_payload.formula")
    check_caller(
      mutated,
      CALLER_SPECS.fetch("first-publication"),
      "first-publication workflow"
    )
  end
  expect_rejection("an event-selected architecture on first publication") do
    mutated = deep_copy(current_callers.fetch("first-publication"))
    mutated.dig("jobs", "first-publication", "with")["arch"] =
      expression("github.event.client_payload.arch")
    check_caller(
      mutated,
      CALLER_SPECS.fetch("first-publication"),
      "first-publication workflow"
    )
  end
  expect_rejection("an event-selected tap ref on first publication") do
    mutated = deep_copy(current_callers.fetch("first-publication"))
    mutated.dig("jobs", "first-publication", "with")["tap-ref"] =
      expression("github.event.client_payload.tap_ref")
    check_caller(
      mutated,
      CALLER_SPECS.fetch("first-publication"),
      "first-publication workflow"
    )
  end
  expect_rejection("missing artifact evidence on first publication") do
    mutated = deep_copy(current_callers.fetch("first-publication"))
    mutated.dig("jobs", "first-publication", "with").delete(
      "dry-run-child-artifact-digest"
    )
    check_caller(
      mutated,
      CALLER_SPECS.fetch("first-publication"),
      "first-publication workflow"
    )
  end
  expect_rejection("write-capable contents on first publication") do
    mutated = deep_copy(current_callers.fetch("first-publication"))
    mutated.dig("jobs", "first-publication", "permissions")["contents"] = "write"
    check_caller(
      mutated,
      CALLER_SPECS.fetch("first-publication"),
      "first-publication workflow"
    )
  end
  expect_rejection("a mutable first-publication target") do
    mutated = deep_copy(current_callers.fetch("first-publication"))
    mutated.dig("jobs", "first-publication")["uses"] =
      "Automattic/kandelo/.github/workflows/reusable-homebrew-repository-namespace-canary.yml@main"
    check_caller(
      mutated,
      CALLER_SPECS.fetch("first-publication"),
      "first-publication workflow"
    )
  end
  expect_rejection("a mismatched main-shell mirror Kandelo input") do
    mutated = deep_copy(current_callers.fetch("main-shell-mirror"))
    mutated.dig("jobs", "publish", "with")["kandelo-ref"] =
      SELF_TEST_KANDELO_WORKFLOW_SHA
    check_caller(
      mutated,
      CALLER_SPECS.fetch("main-shell-mirror"),
      "main-shell-mirror workflow"
    )
  end
  expect_rejection("an event-selected main-shell mirror catalog") do
    mutated = deep_copy(current_callers.fetch("main-shell-mirror"))
    mutated.dig("jobs", "publish", "with")["tap-catalog-ref"] =
      expression("github.event.client_payload.tap_sha")
    check_caller(
      mutated,
      CALLER_SPECS.fetch("main-shell-mirror"),
      "main-shell-mirror workflow"
    )
  end
  expect_rejection("an event-selected main-shell mirror authority") do
    mutated = deep_copy(current_callers.fetch("main-shell-mirror"))
    mutated.dig("jobs", "publish", "with")["mirror-authority-ref"] =
      expression("github.event.client_payload.mirror_sha")
    check_caller(
      mutated,
      CALLER_SPECS.fetch("main-shell-mirror"),
      "main-shell-mirror workflow"
    )
  end
  expect_rejection("a mutable main-shell mirror publisher") do
    mutated = deep_copy(current_callers.fetch("main-shell-mirror"))
    mutated.dig("jobs", "publish")["uses"] =
      "Automattic/kandelo/.github/workflows/reusable-homebrew-main-shell-mirror-publish.yml@main"
    check_caller(
      mutated,
      CALLER_SPECS.fetch("main-shell-mirror"),
      "main-shell-mirror workflow"
    )
  end
  expect_rejection("a package permission on the main-shell mirror caller") do
    mutated = deep_copy(current_callers.fetch("main-shell-mirror"))
    mutated.dig("jobs", "publish", "permissions")["packages"] = "write"
    check_caller(
      mutated,
      CALLER_SPECS.fetch("main-shell-mirror"),
      "main-shell-mirror workflow"
    )
  end
  expect_rejection("a secret on the main-shell mirror caller") do
    mutated = deep_copy(current_callers.fetch("main-shell-mirror"))
    mutated.dig("jobs", "publish")["secrets"] = "inherit"
    check_caller(
      mutated,
      CALLER_SPECS.fetch("main-shell-mirror"),
      "main-shell-mirror workflow"
    )
  end
  expect_rejection("caller-local main-shell mirror steps") do
    mutated = deep_copy(current_callers.fetch("main-shell-mirror"))
    mutated.dig("jobs", "publish")["steps"] = [{ "run" => "true" }]
    check_caller(
      mutated,
      CALLER_SPECS.fetch("main-shell-mirror"),
      "main-shell-mirror workflow"
    )
  end
  expect_rejection("a manual main-shell mirror event") do
    mutated = deep_copy(current_callers.fetch("main-shell-mirror"))
    events = workflow_events(mutated)
    events.delete("repository_dispatch")
    events["workflow_dispatch"] = {}
    check_caller(
      mutated,
      CALLER_SPECS.fetch("main-shell-mirror"),
      "main-shell-mirror workflow"
    )
  end
  expect_rejection("an extra privileged job") do
    mutated = deep_copy(current_callers.fetch("publish"))
    mutated.fetch("jobs")["backdoor"] = {
      "permissions" => { "contents" => "write" },
      "uses" => "owner/repo/.github/workflows/publish.yml@main",
    }
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("a mutable publisher target") do
    mutated = deep_copy(current_callers.fetch("publish"))
    mutated.dig("jobs", "publish")["uses"] =
      "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@feature"
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("an executable publish ref from event data") do
    mutated = deep_copy(current_callers.fetch("publish"))
    mutated.dig("jobs", "publish", "with")["tap-ref"] =
      expression("github.event.client_payload.tap_ref")
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("a mutable write publication tap ref") do
    mutated = deep_copy(current_callers.fetch("publish"))
    mutated.dig("jobs", "publish", "with")["tap-ref"] = "main"
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("the caller commit substituted for reserved Formula source") do
    mutated = deep_copy(current_callers.fetch("publish"))
    mutated.dig("jobs", "publish", "with")["tap-ref"] = expression("github.sha")
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("dry-run publication") do
    mutated = deep_copy(current_callers.fetch("dry-run"))
    mutated.dig("jobs", "dry-run", "with")["dry-run"] = false
    check_caller(mutated, CALLER_SPECS.fetch("dry-run"), "dry-run workflow")
  end
  expect_rejection("maintenance through the publisher") do
    mutated = deep_copy(current_callers.fetch("maintenance"))
    mutated.dig("jobs", "maintain")["uses"] =
      "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@#{CURRENT_KANDELO_WORKFLOW_SHA}"
    check_caller(mutated, CALLER_SPECS.fetch("maintenance"), "maintenance workflow")
  end
  expect_rejection("campaign publication with tap finalization") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig(
      "jobs",
      "publish-rootfs",
      "with",
    )["defer-tap-finalization"] = false
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("campaign publication with VFS acceptance") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig(
      "jobs",
      "publish-rootfs",
      "with",
    )["require-vfs-acceptance"] = true
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("reuse task routed through the build publisher") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig("jobs", "publish-rootfs")["if"] =
      expression(
        "needs.admit.outputs.generation-kind == 'rootfs-wasm32'"
      )
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("bootstrap task routed through the ordinary writer") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig("jobs", "publish-rootfs")["if"] = expression(
      "needs.admit.outputs.disposition == 'build'"
    )
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("bootstrap caller missing package permission ceiling") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig(
      "jobs", "build-bootstrap-rootfs", "permissions"
    ).delete("packages")
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("bootstrap build changed into a normal write") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig(
      "jobs", "build-bootstrap-rootfs", "with"
    )["dry-run"] = false
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("first-child publisher enabled for ordinary absence") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig("jobs", "publish-first-child")["if"] = expression(
      "needs.admit.outputs.disposition == 'build'"
    )
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("first-child publisher replaced by normal publisher") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig("jobs", "publish-first-child")["uses"] = [
      "Automattic/kandelo/.github/workflows/",
      "reusable-homebrew-bottle-publish.yml@",
      prefix_authority["reusable_workflow_commit"],
    ].join
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("bootstrap completion changed into another dry run") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig(
      "jobs", "publish-bootstrap-rootfs", "with"
    )["dry-run"] = true
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("bootstrap completion no longer waits for first child") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig("jobs", "publish-bootstrap-rootfs")["needs"] = [
      "admit",
    ]
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("an event-selected campaign publisher") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig("jobs", "publish-rootfs")["uses"] =
      expression("github.event.client_payload.reusable")
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("a secret inherited by the campaign publisher") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig("jobs", "publish-rootfs")["secrets"] = "inherit"
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("write permission during campaign admission") do
    mutated = deep_copy(prefix_campaign)
    mutated.dig("jobs", "admit", "permissions")["contents"] = "write"
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("campaign release without source ancestry") do
    mutated = deep_copy(prefix_campaign)
    step = mutated.dig("jobs", "seal-handoff", "steps").find do |item|
      item["name"] == "Publish immutable Formula handoff"
    end
    step["run"] = step["run"].sub(
      /[ \t]*--target-main-contains-sha \\\n[^\n]*\n?/,
      ""
    )
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("campaign release without Kandelo ancestry") do
    mutated = deep_copy(prefix_campaign)
    step = mutated.dig("jobs", "seal-handoff", "steps").find do |item|
      item["name"] == "Publish immutable Formula handoff"
    end
    step["run"] = step["run"].sub(
      /[ \t]*--kandelo-main-contains-sha \\\n[^\n]*\n?/,
      ""
    )
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("campaign release with exact-main authority") do
    mutated = deep_copy(prefix_campaign)
    step = mutated.dig("jobs", "seal-handoff", "steps").find do |item|
      item["name"] == "Publish immutable Formula handoff"
    end
    step["run"] = step["run"].sub(
      "--kandelo-main-contains-sha",
      "--exact-kandelo-main-sha"
    )
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("campaign release with swapped ancestry") do
    mutated = deep_copy(prefix_campaign)
    step = mutated.dig("jobs", "seal-handoff", "steps").find do |item|
      item["name"] == "Publish immutable Formula handoff"
    end
    kandelo = expression("needs.admit.outputs.kandelo-commit")
    source_tap = expression(
      "needs.admit.outputs.source-tap-commit"
    )
    sentinel = "__SWAPPED_CAMPAIGN_AUTHORITY__"
    step["run"] = step["run"]
      .sub(kandelo, sentinel)
      .sub(source_tap, kandelo)
      .sub(sentinel, source_tap)
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("handoff derivation outside the dev shell") do
    mutated = deep_copy(prefix_campaign)
    step = mutated.dig("jobs", "seal-handoff", "steps").find do |item|
      item["name"] ==
        "Derive and prepare immutable Formula handoff"
    end
    step["run"] = step["run"].sub(
      'bash scripts/dev-shell.sh "${controller[@]}" ',
      ""
    )
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("workflow SHA as campaign source ancestry") do
    mutated = deep_copy(prefix_campaign)
    step = mutated.dig("jobs", "seal-handoff", "steps").find do |item|
      item["name"] == "Publish immutable Formula handoff"
    end
    step["run"] = step["run"].sub(
      expression("needs.admit.outputs.source-tap-commit"),
      expression("github.sha")
    )
    check_prefix_campaign_workflow(mutated, prefix_authority)
  end
  expect_rejection("path-filtered pull-request checks") do
    mutated = deep_copy(contract)
    workflow_events(mutated)["pull_request"] = { "paths" => [".github/workflows/**"] }
    check_contract_workflow(mutated)
  end
  expect_rejection("a write-capable contract check") do
    mutated = deep_copy(contract)
    mutated.dig("jobs", "publisher-trust")["permissions"] = { "contents" => "write" }
    check_contract_workflow(mutated)
  end
  expect_rejection("an unpinned setup action") do
    mutated = deep_copy(contract)
    mutated.dig("jobs", "publisher-trust", "steps", 1)["uses"] = "ruby/setup-ruby@v1"
    check_contract_workflow(mutated)
  end
  expect_rejection("a disabled contract command") do
    mutated = deep_copy(contract)
    mutated.dig("jobs", "publisher-trust", "steps", 2)["run"] = "true"
    check_contract_workflow(mutated)
  end
  expect_rejection("a write-capable base-controlled check") do
    mutated = deep_copy(base_contract)
    mutated["permissions"] = { "contents" => "write" }
    check_base_contract_workflow(mutated)
  end
  expect_rejection("checking out pull-request code in the base-controlled check") do
    mutated = deep_copy(base_contract)
    mutated.dig("jobs", "publisher-trust-base", "steps", 0, "with")["ref"] =
      expression("github.event.pull_request.head.sha")
    check_base_contract_workflow(mutated)
  end
  expect_rejection("executing the candidate trust parser") do
    mutated = deep_copy(base_contract)
    mutated.dig("jobs", "publisher-trust-base", "steps", 3)["run"] =
      'ruby "$KANDELO_TAP_CONTRACT_CANDIDATE/Kandelo/test-workflow-trust.rb"'
    check_base_contract_workflow(mutated)
  end
  expect_rejection("a path-filtered base-controlled check") do
    mutated = deep_copy(base_contract)
    workflow_events(mutated)["pull_request_target"] = {
      "paths" => [".github/workflows/**"],
    }
    check_base_contract_workflow(mutated)
  end
  expect_rejection("a base-controlled check for an unprotected target branch") do
    mutated = deep_copy(base_contract)
    workflow_events(mutated).fetch("pull_request_target")["branches"] = ["release"]
    check_base_contract_workflow(mutated)
  end
end

begin
  check_workflow_file_set
  check(CURRENT_KANDELO_WORKFLOW_SHA.match?(/\A[0-9a-f]{40}\z/),
        "current Kandelo workflow pin is not an exact SHA")
  check(CURRENT_KANDELO_CONSUMER_SHA.match?(/\A[0-9a-f]{40}\z/),
        "current Kandelo package-consumer pin is not an exact SHA")
  check(CURRENT_KANDELO_CONSUMER_SHA == CURRENT_KANDELO_WORKFLOW_SHA,
        "write publisher and package consumer must select the same Kandelo main SHA")
  check(DRY_RUN_KANDELO_WORKFLOW_SHA.match?(/\A[0-9a-f]{40}\z/),
        "dry-run Kandelo workflow pin is not an exact SHA")
  check(PACKAGE_GENERATION_WASM32_TAG.match?(
          /\Apackage-generation-rootfs-wasm32-abi-v42-sha256-[0-9a-f]{64}\z/
        ),
        "rootfs package generation is not an exact ABI 42 content tag")
  check(CLOSED_SELECTION_KANDELO_SHA.match?(/\A[0-9a-f]{40}\z/),
        "closed-selection Kandelo pin is not an exact SHA")
  {
    "main-shell lifecycle Kandelo M" => MAIN_SHELL_MIRROR_KANDELO_SHA,
    "main-shell mirror tap catalog TF" => MAIN_SHELL_MIRROR_TAP_CATALOG_SHA,
    "main-shell mirror authority TA0" => MAIN_SHELL_MIRROR_AUTHORITY_SHA,
    "main-shell mirror canary C" => MAIN_SHELL_MIRROR_CANARY_SHA,
  }.each do |label, sha|
    check(sha.match?(/\A[0-9a-f]{40}\z/),
          "#{label} is not finalized to an exact SHA")
  end
  {
    "first publication" => FIRST_PUBLICATION_KANDELO_SHA,
    "retired PAT" => RETIRED_PAT_KANDELO_WORKFLOW_SHA,
    "previous" => PREVIOUS_KANDELO_WORKFLOW_SHA,
    "retired" => RETIRED_KANDELO_WORKFLOW_SHA,
    "self-test" => SELF_TEST_KANDELO_WORKFLOW_SHA,
  }.each do |label, sha|
    check(sha.match?(/\A[0-9a-f]{40}\z/),
          "#{label} Kandelo workflow pin is not an exact SHA")
  end
  historical_profile_shas = [
    RETIRED_PAT_KANDELO_WORKFLOW_SHA,
    PREVIOUS_KANDELO_WORKFLOW_SHA,
    RETIRED_KANDELO_WORKFLOW_SHA,
    SELF_TEST_KANDELO_WORKFLOW_SHA,
  ]
  current_and_historical_profile_shas = [
    CURRENT_KANDELO_WORKFLOW_SHA,
    *historical_profile_shas,
  ]
  check(
    current_and_historical_profile_shas.uniq.length ==
      current_and_historical_profile_shas.length,
    "current and historical workflow trust fixtures must use " \
      "distinct SHAs"
  )
  # WHY: first-publication is a live bootstrap caller, not a historical
  # fixture. A trust rotation intentionally folds its older pin into
  # current main. It may remain separately pinned while split, but it
  # must never alias a historical fixture because that would weaken the
  # fixture mutation tests.
  check(
    FIRST_PUBLICATION_KANDELO_SHA == CURRENT_KANDELO_WORKFLOW_SHA ||
      !historical_profile_shas.include?(FIRST_PUBLICATION_KANDELO_SHA),
    "first-publication pin collides with a historical workflow " \
      "trust fixture"
  )
  # WHY: the split bootstrap deliberately moves dry-run and
  # first-publication to one new commit before production moves. Those two
  # live callers may therefore converge with each other or current main,
  # while neither may impersonate a historical mutation-test fixture.
  check(
    DRY_RUN_KANDELO_WORKFLOW_SHA == CURRENT_KANDELO_WORKFLOW_SHA ||
      DRY_RUN_KANDELO_WORKFLOW_SHA == FIRST_PUBLICATION_KANDELO_SHA ||
      !historical_profile_shas.include?(DRY_RUN_KANDELO_WORKFLOW_SHA),
    "split dry-run pin collides with a historical workflow trust fixture"
  )
  callers = CALLER_SPECS.to_h do |key, spec|
    [key, load_workflow(spec.fetch(:path))]
  end
  closed_selection = load_workflow(CLOSED_SELECTION_PATH)
  contract = load_workflow(CONTRACT_PATH)
  base_contract = load_workflow(BASE_CONTRACT_PATH)
  prefix_authority = load_json(PREFIX_CAMPAIGN_AUTHORITY_PATH)
  prefix_campaign = load_workflow(PREFIX_CAMPAIGN_PATH)
  check(
    File.read(PREFIX_CAMPAIGN_AUTHORITY_PATH) ==
      JSON.pretty_generate(prefix_authority) + "\n",
    "prefix-campaign authority is not canonical pretty JSON"
  )

  self_test(
    callers,
    closed_selection,
    contract,
    base_contract,
    prefix_campaign,
    prefix_authority
  )
  check_caller_profile(callers)
  check_closed_selection_caller(closed_selection)
  check_prefix_campaign_authority(prefix_authority)
  check_prefix_campaign_workflow(prefix_campaign, prefix_authority)
  check_contract_workflow(contract)
  check_base_contract_workflow(base_contract)
  puts "test-workflow-trust.rb: ok"
rescue KeyError, Psych::Exception, RuntimeError => e
  warn "test-workflow-trust.rb: #{e.message}"
  exit 1
end
