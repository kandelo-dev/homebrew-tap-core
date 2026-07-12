#!/usr/bin/env ruby
# frozen_string_literal: true

require "digest"
require "yaml"

ROOT = File.expand_path("..", __dir__)
DRY_RUN_PATH = File.join(ROOT, ".github/workflows/dry-run-bottles.yml")
PUBLISH_PATH = File.join(ROOT, ".github/workflows/publish-bottles.yml")
CONTRACT_PATH = File.join(ROOT, ".github/workflows/contract-checks.yml")

def check(condition, message)
  raise message unless condition
end

def load_workflow(path)
  workflow = YAML.safe_load_file(path, aliases: false)
  check(workflow.is_a?(Hash), "#{File.basename(path)} is not a workflow mapping")
  workflow
end

def workflow_events(workflow)
  events = workflow.key?("on") ? workflow["on"] : workflow[true]
  check(events.is_a?(Hash), "workflow on: value is not a mapping")
  events
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

def keys_named(node, wanted, matches = [])
  case node
  when Hash
    node.each do |key, value|
      matches << value if key.to_s == wanted
      keys_named(value, wanted, matches)
    end
  when Array
    node.each { |value| keys_named(value, wanted, matches) }
  end
  matches
end

def exact_permissions?(actual, expected)
  actual.is_a?(Hash) && actual.transform_keys(&:to_s) == expected
end

def workflow_jobs(workflow)
  jobs = workflow["jobs"]
  check(jobs.is_a?(Hash), "workflow jobs: value is not a mapping")
  jobs
end

def job_steps(job, name)
  steps = job["steps"]
  check(steps.is_a?(Array), "#{name} steps: value is not an array")
  check(steps.all? { |step| step.is_a?(Hash) }, "#{name} contains a non-mapping step")
  steps
end

def check_common(workflow, label)
  cache_uses = values_for_key(workflow, "uses").select do |value|
    value.is_a?(String) && value.downcase.match?(%r{\Aactions/cache(?:/restore)?@})
  end
  check(cache_uses.empty?, "#{label} consumes Actions cache state: #{cache_uses.join(', ')}")
  check(keys_named(workflow, "secrets").empty?, "#{label} passes repository secrets")

  unsafe_runs = values_for_key(workflow, "run").select do |value|
    value.is_a?(String) && value.include?("${{")
  end
  check(unsafe_runs.empty?, "#{label} interpolates a GitHub expression into shell syntax")
end

def check_dispatch(workflow, event_type, label)
  events = workflow_events(workflow)
  check(events.keys == ["repository_dispatch"], "#{label} must only expose repository_dispatch")
  types = events.dig("repository_dispatch", "types")
  check(types == [event_type], "#{label} has an unexpected repository_dispatch type")
end

def check_default_branch(job, label, expected_name, expected_hash)
  steps = job_steps(job, label)
  check(steps.length == 1, "#{label} must contain exactly one validation step")
  validation = steps.first
  check(validation.keys.sort == %w[env id name run shell] &&
        validation["name"] == expected_name &&
        validation["id"] == "request" &&
        validation["shell"] == "bash",
        "#{label} validation step mapping changed")
  check(validation["run"].to_s.include?('[ "$GITHUB_REF" = "refs/heads/main" ]'),
        "#{label} does not enforce the default-branch event invariant")
  check(Digest::SHA256.hexdigest(validation["run"].to_s) == expected_hash,
        "#{label} validation script changed")
end

READ_PERMISSIONS = { "contents" => "read", "packages" => "read", "actions" => "read" }.freeze
WRITE_PERMISSIONS = { "contents" => "write", "packages" => "write", "actions" => "read" }.freeze

def check_dry_run(workflow)
  check_dispatch(workflow, "dry-run-kandelo-bottles", "dry-run workflow")
  check_common(workflow, "dry-run workflow")
  check(exact_permissions?(workflow["permissions"], READ_PERMISSIONS),
        "dry-run workflow permissions are not exact")

  jobs = workflow_jobs(workflow)
  check(jobs.keys.sort == %w[dry-run validate-request], "dry-run workflow has an unexpected job set")
  validation = jobs.fetch("validate-request")
  check(validation.keys.sort == %w[outputs runs-on steps],
        "dry-run validation job execution contract changed")
  check(validation["runs-on"] == "ubuntu-latest",
        "dry-run validation runner trust boundary changed")
  check(!validation.key?("permissions"), "dry-run validation overrides read-only permissions")
  expected_outputs = {
    "arches" => "${{ steps.request.outputs.arches }}",
    "formulae" => "${{ steps.request.outputs.formulae }}",
    "kandelo-ref" => "${{ steps.request.outputs.kandelo-ref }}",
    "tap-ref" => "${{ steps.request.outputs.tap-ref }}",
  }
  check(validation["outputs"] == expected_outputs, "dry-run validation outputs changed")
  expected_env = {
    "REQUEST_ARCHES" => "${{ github.event.client_payload.arches || 'wasm32' }}",
    "REQUEST_FORMULAE" => "${{ github.event.client_payload.formulae }}",
    "REQUEST_KANDELO_REF" => "${{ github.event.client_payload.kandelo_ref || 'main' }}",
    "REQUEST_TAP_REF" => "${{ github.event.client_payload.tap_ref }}",
  }
  check(job_steps(validation, "dry-run validation").first["env"] == expected_env,
        "dry-run validation input wiring changed")
  check_default_branch(
    validation,
    "dry-run validation",
    "Validate default-branch dry-run request",
    "dd0dcc5d4565ab1d83342c43bb439ccd350397321254cf3a15b45a72cde270ba"
  )
  caller = jobs.fetch("dry-run")
  check(caller.keys.sort == %w[needs permissions uses with],
        "dry-run caller execution contract changed")
  check(caller["needs"] == ["validate-request"],
        "dry-run caller bypasses request validation")
  check(exact_permissions?(caller["permissions"], READ_PERMISSIONS),
        "dry-run caller permissions are not exact")
  check(caller["uses"] ==
        "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main",
        "dry-run caller does not use the reviewed publisher")
  expected_inputs = {
    "kandelo-repository" => "Automattic/kandelo",
    "kandelo-ref" => "${{ needs.validate-request.outputs.kandelo-ref }}",
    "tap-repository" => "Automattic/kandelo-homebrew",
    "tap-ref" => "${{ needs.validate-request.outputs.tap-ref }}",
    "formulae" => "${{ needs.validate-request.outputs.formulae }}",
    "arches" => "${{ needs.validate-request.outputs.arches }}",
    "dry-run" => true,
  }
  check(caller["with"] == expected_inputs, "dry-run caller bypasses validated inputs")
  expected_uses = ["Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main"]
  check(values_for_key(workflow, "uses") == expected_uses, "dry-run action set changed")
end

def check_publish(workflow)
  check_dispatch(workflow, "publish-kandelo-bottles", "publish workflow")
  check_common(workflow, "publish workflow")
  check(exact_permissions?(workflow["permissions"], READ_PERMISSIONS),
        "publish workflow defaults are not read-only")

  jobs = workflow_jobs(workflow)
  check(jobs.keys.sort == %w[publish validate-request], "publish workflow has an unexpected job set")
  validation = jobs.fetch("validate-request")
  check(validation.keys.sort == %w[outputs permissions runs-on steps],
        "publication validation job execution contract changed")
  check(validation["runs-on"] == "ubuntu-latest",
        "publication validation runner trust boundary changed")
  check(exact_permissions?(validation["permissions"], READ_PERMISSIONS),
        "publication validation permissions are not read-only")
  expected_outputs = {
    "arches" => "${{ steps.request.outputs.arches }}",
    "formulae" => "${{ steps.request.outputs.formulae }}",
    "release-tag" => "${{ steps.request.outputs.release-tag }}",
  }
  check(validation["outputs"] == expected_outputs, "publication validation outputs changed")
  expected_env = {
    "REQUEST_ARCHES" => "${{ github.event.client_payload.arches || 'wasm32' }}",
    "REQUEST_FORMULAE" => "${{ github.event.client_payload.formulae }}",
    "REQUEST_RELEASE_TAG" => "${{ github.event.client_payload.release_tag || '' }}",
  }
  check(job_steps(validation, "publication validation").first["env"] == expected_env,
        "publication validation input wiring changed")
  check_default_branch(
    validation,
    "publication validation",
    "Validate default-branch publication request",
    "f62d1a67ad9100fe33d406e9923139f50c5c2e252c81cc71e63dbd1fde74b7a2"
  )

  caller = jobs.fetch("publish")
  check(caller.keys.sort == %w[needs permissions uses with],
        "publisher call execution contract changed")
  check(caller["needs"] == ["validate-request"],
        "publisher bypasses request validation")
  check(exact_permissions?(caller["permissions"], WRITE_PERMISSIONS),
        "publisher call permissions are not exact")
  check(caller["uses"] ==
        "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main",
        "publisher does not use the reviewed reusable workflow")
  expected_inputs = {
    "kandelo-repository" => "Automattic/kandelo",
    "kandelo-ref" => "main",
    "tap-repository" => "Automattic/kandelo-homebrew",
    "tap-ref" => "main",
    "formulae" => "${{ needs.validate-request.outputs.formulae }}",
    "arches" => "${{ needs.validate-request.outputs.arches }}",
    "release-tag" => "${{ needs.validate-request.outputs.release-tag }}",
    "dry-run" => false,
  }
  check(caller["with"] == expected_inputs, "publisher bypasses validated inputs or main refs")

  serialized = workflow.to_s
  check(!serialized.include?("client_payload.kandelo_ref") &&
        !serialized.include?("client_payload.tap_ref"), "publisher accepts executable refs")
  expected_uses = ["Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main"]
  check(values_for_key(workflow, "uses") == expected_uses, "publish action set changed")
end

def check_contract_workflow(workflow)
  events = workflow_events(workflow)
  watched_paths = [
    ".github/workflows/**",
    "Kandelo/test-workflow-trust.sh",
    "Kandelo/test-workflow-trust.rb",
  ]
  expected_events = {
    "pull_request" => { "paths" => watched_paths },
    "push" => { "branches" => ["main"], "paths" => watched_paths },
  }
  check(events == expected_events, "contract-check workflow triggers changed")
  expected = { "contents" => "read" }
  check(exact_permissions?(workflow["permissions"], expected),
        "contract-check workflow permissions are not exact")
  jobs = workflow_jobs(workflow)
  check(jobs.keys == ["publisher-trust"], "contract-check workflow has an unexpected job set")
  job = jobs.fetch("publisher-trust")
  check(!job.key?("permissions"), "contract-check job overrides read-only permissions")
  steps = job_steps(job, "contract-check")
  expected_steps = [
    { "uses" => "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" },
    {
      "uses" => "ruby/setup-ruby@d45b1a4e94b71acab930e56e79c6aa188764e7f9",
      "with" => { "ruby-version" => "3.4" },
    },
    {
      "name" => "Validate publisher trust boundaries",
      "run" => "bash Kandelo/test-workflow-trust.sh",
    },
  ]
  expected_job = { "runs-on" => "ubuntu-latest", "steps" => expected_steps }
  check(job == expected_job, "contract-check job execution contract changed")
  uses = values_for_key(workflow, "uses")
  expected_uses = [
    "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
    "ruby/setup-ruby@d45b1a4e94b71acab930e56e79c6aa188764e7f9",
  ]
  check(uses == expected_uses, "contract-check action set or pins changed")
  ruby_step = steps.find do |step|
    step["uses"] == "ruby/setup-ruby@d45b1a4e94b71acab930e56e79c6aa188764e7f9"
  end
  check(ruby_step&.dig("with", "ruby-version") == "3.4",
        "contract-check Ruby version changed")
  check_common(workflow, "contract-check workflow")
end

def self_test(dry_run, publish, contract)
  fixture = YAML.safe_load(<<~YAML, aliases: false)
    on:
      workflow_dispatch: {}
    permissions: "write-all"
    jobs:
      unsafe:
        permissions:
          contents: "write"
        steps:
          - uses: >-
              actions/cache/restore@v4
          - run: >-
              echo "${{ inputs.formulae }}"
  YAML
  check(workflow_events(fixture).key?("workflow_dispatch"), "self-test missed workflow_dispatch")
  check(fixture["permissions"] == "write-all", "self-test missed quoted write-all")
  check(fixture.dig("jobs", "unsafe", "permissions", "contents") == "write",
        "self-test missed quoted write permission")
  check(values_for_key(fixture, "uses").include?("actions/cache/restore@v4"),
        "self-test missed folded cache action")
  check(values_for_key(fixture, "run").first.include?("${{"),
        "self-test missed folded shell expression")

  expect_rejection("a write-capable dry-run validation job") do
    mutated = deep_copy(dry_run)
    mutated.dig("jobs", "validate-request")["permissions"] = { "contents" => "write" }
    check_dry_run(mutated)
  end
  expect_rejection("short-circuited dry-run validation") do
    mutated = deep_copy(dry_run)
    step = mutated.dig("jobs", "validate-request", "steps").first
    step["run"] = "exit 0\n#{step['run']}"
    check_dry_run(mutated)
  end
  expect_rejection("continued dry-run validation failure") do
    mutated = deep_copy(dry_run)
    mutated.dig("jobs", "validate-request", "steps").first["continue-on-error"] = true
    check_dry_run(mutated)
  end
  expect_rejection("a self-hosted dry-run validation") do
    mutated = deep_copy(dry_run)
    mutated.dig("jobs", "validate-request")["runs-on"] = "self-hosted"
    check_dry_run(mutated)
  end
  expect_rejection("an extra dry-run backdoor job") do
    mutated = deep_copy(dry_run)
    mutated.fetch("jobs")["backdoor"] = {
      "permissions" => { "contents" => "write", "packages" => "write" },
      "uses" => "owner/repo/.github/workflows/write.yml@main",
    }
    check_dry_run(mutated)
  end
  expect_rejection("a dry-run cache restore") do
    mutated = deep_copy(dry_run)
    mutated.dig("jobs", "validate-request", "steps") << {
      "uses" => "actions/cache/restore@v4",
    }
    check_dry_run(mutated)
  end
  expect_rejection("a dry-run shell expression") do
    mutated = deep_copy(dry_run)
    mutated.dig("jobs", "validate-request", "steps") << {
      "run" => 'echo "${{ github.token }}"',
    }
    check_dry_run(mutated)
  end
  expect_rejection("an extra publish backdoor job") do
    mutated = deep_copy(publish)
    mutated.fetch("jobs")["backdoor"] = {
      "permissions" => { "contents" => "write", "packages" => "write" },
      "uses" => "owner/repo/.github/workflows/write.yml@main",
    }
    check_publish(mutated)
  end
  expect_rejection("publication detached from validation") do
    mutated = deep_copy(publish)
    caller = mutated.dig("jobs", "publish")
    caller.delete("needs")
    caller.fetch("with")["formulae"] = "${{ github.event.client_payload.formulae }}"
    check_publish(mutated)
  end
  expect_rejection("a write-capable contract-check job") do
    mutated = deep_copy(contract)
    mutated.dig("jobs", "publisher-trust")["permissions"] = { "contents" => "write" }
    check_contract_workflow(mutated)
  end
  expect_rejection("an extra contract-check job") do
    mutated = deep_copy(contract)
    mutated.fetch("jobs")["backdoor"] = {
      "permissions" => { "contents" => "write" },
      "runs-on" => "ubuntu-latest",
      "steps" => [{ "run" => "true" }],
    }
    check_contract_workflow(mutated)
  end
  expect_rejection("an unpinned Ruby setup action") do
    mutated = deep_copy(contract)
    mutated.dig("jobs", "publisher-trust", "steps") << { "uses" => "ruby/setup-ruby@v1" }
    check_contract_workflow(mutated)
  end
  expect_rejection("a disabled hosted contract command") do
    mutated = deep_copy(contract)
    mutated.dig("jobs", "publisher-trust", "steps", 2)["run"] = "true"
    check_contract_workflow(mutated)
  end
  expect_rejection("a skipped hosted contract job") do
    mutated = deep_copy(contract)
    mutated.dig("jobs", "publisher-trust")["if"] = false
    check_contract_workflow(mutated)
  end
  expect_rejection("a self-hosted contract job") do
    mutated = deep_copy(contract)
    mutated.dig("jobs", "publisher-trust")["runs-on"] = "self-hosted"
    check_contract_workflow(mutated)
  end
  expect_rejection("contract checks that ignore workflow changes") do
    mutated = deep_copy(contract)
    workflow_events(mutated).fetch("pull_request")["paths"] = ["README.md"]
    check_contract_workflow(mutated)
  end
end

begin
  dry_run = load_workflow(DRY_RUN_PATH)
  publish = load_workflow(PUBLISH_PATH)
  contract = load_workflow(CONTRACT_PATH)
  self_test(dry_run, publish, contract)
  check_dry_run(dry_run)
  check_publish(publish)
  check_contract_workflow(contract)
  puts "test-workflow-trust.rb: ok"
rescue KeyError, Psych::Exception, RuntimeError => e
  warn "test-workflow-trust.rb: #{e.message}"
  exit 1
end
