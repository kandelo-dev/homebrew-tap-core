#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

ROOT = File.expand_path("..", __dir__)
WORKFLOW_ROOT = File.join(ROOT, ".github/workflows")
CONTRACT_PATH = File.join(WORKFLOW_ROOT, "contract-checks.yml")
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

CALLER_SPECS = {
  "publish" => {
    path: File.join(WORKFLOW_ROOT, "publish-bottles.yml"),
    name: "Publish Kandelo bottles",
    event: "publish-kandelo-bottles",
    job: "publish",
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main",
    inputs: PUBLISH_INPUTS,
  },
  "dry-run" => {
    path: File.join(WORKFLOW_ROOT, "dry-run-bottles.yml"),
    name: "Dry run Kandelo bottles",
    event: "dry-run-kandelo-bottles",
    job: "dry-run",
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main",
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
    reusable: "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-maintenance.yml@main",
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

def self_test(callers, contract)
  expect_rejection("caller-local environment configuration") do
    mutated = deep_copy(callers.fetch("dry-run"))
    mutated["env"] = { "BASH_ENV" => "/tmp/untrusted" }
    check_caller(mutated, CALLER_SPECS.fetch("dry-run"), "dry-run workflow")
  end
  expect_rejection("caller-local executable steps") do
    mutated = deep_copy(callers.fetch("dry-run"))
    mutated.dig("jobs", "dry-run")["steps"] = [{ "run" => "true" }]
    check_caller(mutated, CALLER_SPECS.fetch("dry-run"), "dry-run workflow")
  end
  expect_rejection("secret inheritance") do
    mutated = deep_copy(callers.fetch("publish"))
    mutated.dig("jobs", "publish")["secrets"] = "inherit"
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("an extra privileged job") do
    mutated = deep_copy(callers.fetch("publish"))
    mutated.fetch("jobs")["backdoor"] = {
      "permissions" => { "contents" => "write" },
      "uses" => "owner/repo/.github/workflows/publish.yml@main",
    }
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("a mutable publisher target") do
    mutated = deep_copy(callers.fetch("publish"))
    mutated.dig("jobs", "publish")["uses"] =
      "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@feature"
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("an executable publish ref from event data") do
    mutated = deep_copy(callers.fetch("publish"))
    mutated.dig("jobs", "publish", "with")["tap-ref"] =
      expression("github.event.client_payload.tap_ref")
    check_caller(mutated, CALLER_SPECS.fetch("publish"), "publish workflow")
  end
  expect_rejection("dry-run publication") do
    mutated = deep_copy(callers.fetch("dry-run"))
    mutated.dig("jobs", "dry-run", "with")["dry-run"] = false
    check_caller(mutated, CALLER_SPECS.fetch("dry-run"), "dry-run workflow")
  end
  expect_rejection("maintenance through the publisher") do
    mutated = deep_copy(callers.fetch("maintenance"))
    mutated.dig("jobs", "maintain")["uses"] =
      "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main"
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
end

begin
  callers = CALLER_SPECS.to_h do |key, spec|
    [key, load_workflow(spec.fetch(:path))]
  end
  contract = load_workflow(CONTRACT_PATH)

  self_test(callers, contract)
  callers.each do |key, workflow|
    check_caller(workflow, CALLER_SPECS.fetch(key), "#{key} workflow")
  end
  check_contract_workflow(contract)
  puts "test-workflow-trust.rb: ok"
rescue KeyError, Psych::Exception, RuntimeError => e
  warn "test-workflow-trust.rb: #{e.message}"
  exit 1
end
