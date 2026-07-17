#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

candidate_root = ARGV.shift
abort "usage: test-workflow-trust.rb [candidate-root]" unless ARGV.empty?

ROOT = candidate_root ? File.expand_path(candidate_root) : File.expand_path("..", __dir__)
WORKFLOW_ROOT = File.join(ROOT, ".github/workflows")
CONTRACT_PATH = File.join(WORKFLOW_ROOT, "contract-checks.yml")
BASE_CONTRACT_PATH = File.join(WORKFLOW_ROOT, "base-contract-checks.yml")
EXPECTED_WORKFLOW_FILES = %w[
  base-contract-checks.yml
  contract-checks.yml
  dry-run-bottles.yml
  maintain-bottles.yml
  publish-bottles.yml
].freeze
CALLER_PERMISSIONS = {
  "actions" => "read",
  "contents" => "write",
  "packages" => "write",
}.freeze
CHECKOUT_ACTION = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
RUBY_ACTION = "ruby/setup-ruby@d45b1a4e94b71acab930e56e79c6aa188764e7f9"

def check(condition, message)
  raise message unless condition
end

def load_workflow(path)
  workflow = YAML.safe_load(File.read(path), aliases: false)
  check(workflow.is_a?(Hash), "#{File.basename(path)} is not a workflow mapping")
  workflow
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

def deep_copy(value)
  Marshal.load(Marshal.dump(value))
end

def expect_rejection(label)
  rejected = false
  begin
    yield
  rescue KeyError, RuntimeError
    rejected = true
  end
  check(rejected, "self-test accepted #{label}")
end

PUBLISH_INPUTS = {
  "kandelo-repository" => "Automattic/kandelo",
  "kandelo-ref" => "main",
  "tap-repository" => "Automattic/kandelo-homebrew",
  "tap-ref" => "main",
  "formulae" => expression("github.event.client_payload.formulae"),
  "arches" => expression("github.event.client_payload.arches || 'wasm32'"),
  "release-tag" => expression("github.event.client_payload.release_tag || ''"),
  "expected-cache-keys" => expression("github.event.client_payload.expected_cache_keys || ''"),
  "force" => expression("github.event.client_payload.force || false"),
  "dry-run" => false,
}.freeze

VFS_PUBLISH_INPUTS = PUBLISH_INPUTS.merge({
  "require-vfs-acceptance" => expression(
    "github.event.client_payload.require_vfs_acceptance || false"
  ),
}).freeze

CURRENT_KANDELO_WORKFLOW_SHA = "c3f91d622c3c878e15783c67e99e483e54ab25c1"
PREVIOUS_KANDELO_WORKFLOW_SHA = "d26c2f69da766830eaf5125c7b4fcf43ed620313"
RETIRED_KANDELO_WORKFLOW_SHA = "b8bdecce9c450f840a64ad072fb8ddb31d8cfcb5"
SELF_TEST_KANDELO_WORKFLOW_SHA = "1111111111111111111111111111111111111111"

CALLER_SPECS = {
  "publish" => {
    path: File.join(WORKFLOW_ROOT, "publish-bottles.yml"),
    name: "Publish Kandelo bottles",
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
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@#{CURRENT_KANDELO_WORKFLOW_SHA}",
    inputs: PUBLISH_INPUTS.merge({
      "kandelo-repository" => expression(
        "github.event.client_payload.kandelo_repository || 'Automattic/kandelo'"
      ),
      "kandelo-ref" => expression("github.event.client_payload.kandelo_ref || 'main'"),
      "tap-repository" => expression(
        "github.event.client_payload.tap_repository || 'Automattic/kandelo-homebrew'"
      ),
      "tap-ref" => expression("github.event.client_payload.tap_ref || 'main'"),
      "dry-run" => true,
    }).freeze,
  },
  "maintenance" => {
    path: File.join(WORKFLOW_ROOT, "maintain-bottles.yml"),
    name: "Maintain Kandelo bottles",
    event: "maintain-kandelo-bottles",
    job: "maintain",
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-maintenance.yml@#{CURRENT_KANDELO_WORKFLOW_SHA}",
    inputs: {
      "mode" => expression("github.event.client_payload.mode || 'rebuild'"),
      "formulae" => expression("github.event.client_payload.formulae"),
      "arches" => expression("github.event.client_payload.arches || 'wasm32'"),
      "release-tag" => expression("github.event.client_payload.release_tag || ''"),
      "expected-cache-keys" => expression(
        "github.event.client_payload.expected_cache_keys || ''"
      ),
      "force" => expression("github.event.client_payload.force || false"),
      "rollback-reason" => expression("github.event.client_payload.rollback_reason || ''"),
      "rollback-ref" => expression("github.event.client_payload.rollback_ref || ''"),
      "deleted-package-url" => expression(
        "github.event.client_payload.deleted_package_url || ''"
      ),
      "deletion-reason" => expression("github.event.client_payload.deletion_reason || ''"),
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
        {path: ".github/workflows/publish-bottles.yml", mode: "100644", type: "blob"}
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
      ]
  ' "$tree_json" >/dev/null || {
    echo "::error::candidate workflow or trust-root file set changed"; exit 2;
  }
  paths=(
    .github/workflows/base-contract-checks.yml
    .github/workflows/contract-checks.yml
    .github/workflows/dry-run-bottles.yml
    .github/workflows/maintain-bottles.yml
    .github/workflows/publish-bottles.yml
    Kandelo/test-workflow-trust.rb
    Kandelo/test-workflow-trust.sh
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
  check(normalized_keys(workflow, label).sort == %w[jobs name on],
        "#{label} has unexpected top-level configuration")
  check(workflow["name"] == spec.fetch(:name), "#{label} name changed")
  check(workflow_events(workflow) == {
    "repository_dispatch" => { "types" => [spec.fetch(:event)] },
  }, "#{label} must expose only its reviewed repository_dispatch event")

  jobs = workflow["jobs"]
  check(jobs.is_a?(Hash) && jobs.keys == [spec.fetch(:job)],
        "#{label} has an unexpected job set")
  job = jobs.fetch(spec.fetch(:job))
  check(normalized_keys(job, "#{label} job").sort == %w[permissions uses with],
        "#{label} caller job is not data-only")
  check(exact_permissions?(job["permissions"], CALLER_PERMISSIONS),
        "#{label} permission ceiling changed")
  check(job["uses"] == spec.fetch(:reusable), "#{label} reusable workflow target changed")
  check(job["with"] == spec.fetch(:inputs), "#{label} caller inputs changed")

  check(values_for_key(workflow, "uses") == [spec.fetch(:reusable)],
        "#{label} executable workflow set changed")
  %w[run steps secrets env defaults].each do |key|
    check(values_for_key(workflow, key).empty?, "#{label} contains caller-local #{key}")
  end
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
    "Kandelo/test-workflow-trust.sh",
    "Kandelo/test-workflow-trust.rb",
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
    { "uses" => CHECKOUT_ACTION },
    {
      "uses" => RUBY_ACTION,
      "with" => { "ruby-version" => "3.4" },
    },
    {
      "name" => "Validate publisher trust boundaries",
      "run" => "bash Kandelo/test-workflow-trust.sh",
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

def self_test(callers, contract, base_contract)
  previous_specs = caller_specs_for_sha(PREVIOUS_KANDELO_WORKFLOW_SHA)
  retired_specs = deep_copy(caller_specs_for_sha(RETIRED_KANDELO_WORKFLOW_SHA))
  retired_specs.fetch("publish")[:inputs] = PUBLISH_INPUTS
  retired_specs.freeze
  arbitrary_specs = caller_specs_for_sha(SELF_TEST_KANDELO_WORKFLOW_SHA)
  test_profiles = { "current" => CALLER_SPECS }
  current_callers = callers_for_specs(callers, CALLER_SPECS)
  previous_callers = callers_for_specs(callers, previous_specs)
  retired_callers = callers_for_specs(callers, retired_specs)
  arbitrary_callers = callers_for_specs(callers, arbitrary_specs)
  check(check_caller_profile(current_callers, test_profiles) == "current",
        "current caller profile was not selected")

  expect_rejection("mixed current and arbitrary caller generations") do
    mutated = deep_copy(current_callers)
    mutated["publish"] = deep_copy(arbitrary_callers.fetch("publish"))
    check_caller_profile(mutated, test_profiles)
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
  {
    "previous" => PREVIOUS_KANDELO_WORKFLOW_SHA,
    "retired" => RETIRED_KANDELO_WORKFLOW_SHA,
    "self-test" => SELF_TEST_KANDELO_WORKFLOW_SHA,
  }.each do |label, sha|
    check(sha.match?(/\A[0-9a-f]{40}\z/),
          "#{label} Kandelo workflow pin is not an exact SHA")
  end
  workflow_shas = [
    CURRENT_KANDELO_WORKFLOW_SHA,
    PREVIOUS_KANDELO_WORKFLOW_SHA,
    RETIRED_KANDELO_WORKFLOW_SHA,
    SELF_TEST_KANDELO_WORKFLOW_SHA,
  ]
  check(workflow_shas.uniq.length == workflow_shas.length,
        "Kandelo workflow trust fixtures must use distinct SHAs")
  callers = CALLER_SPECS.to_h do |key, spec|
    [key, load_workflow(spec.fetch(:path))]
  end
  contract = load_workflow(CONTRACT_PATH)
  base_contract = load_workflow(BASE_CONTRACT_PATH)

  self_test(callers, contract, base_contract)
  check_caller_profile(callers)
  check_contract_workflow(contract)
  check_base_contract_workflow(base_contract)
  puts "test-workflow-trust.rb: ok"
rescue KeyError, Psych::Exception, RuntimeError => e
  warn "test-workflow-trust.rb: #{e.message}"
  exit 1
end
