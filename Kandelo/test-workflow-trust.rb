#!/usr/bin/env ruby
# frozen_string_literal: true

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

def check_default_branch(steps, label)
  validation = steps.find { |step| step["name"].to_s.start_with?("Validate default-branch") }
  check(!validation.nil?, "#{label} lacks default-branch validation")
  check(validation["run"].to_s.include?('[ "$GITHUB_REF" = "refs/heads/main" ]'),
        "#{label} does not enforce the default-branch event invariant")
end

READ_PERMISSIONS = { "contents" => "read", "packages" => "read", "actions" => "read" }.freeze
WRITE_PERMISSIONS = { "contents" => "write", "packages" => "write", "actions" => "read" }.freeze

def check_dry_run(workflow)
  check_dispatch(workflow, "dry-run-kandelo-bottles", "dry-run workflow")
  check_common(workflow, "dry-run workflow")
  check(exact_permissions?(workflow["permissions"], READ_PERMISSIONS),
        "dry-run workflow permissions are not exact")

  jobs = workflow_jobs(workflow)
  check_default_branch(job_steps(jobs.fetch("validate-request"), "dry-run validation"), "dry-run workflow")
  caller = jobs.fetch("dry-run")
  check(exact_permissions?(caller["permissions"], READ_PERMISSIONS),
        "dry-run caller permissions are not exact")
  check(caller["uses"] ==
        "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main",
        "dry-run caller does not use the reviewed publisher")
  inputs = caller.fetch("with")
  check(inputs["kandelo-repository"] == "Automattic/kandelo" &&
        inputs["tap-repository"] == "Automattic/kandelo-homebrew",
        "dry-run caller does not use first-party repositories")
  check(inputs["dry-run"] == true, "dry-run caller does not pass literal dry-run mode")
end

def check_publish(workflow)
  check_dispatch(workflow, "publish-kandelo-bottles", "publish workflow")
  check_common(workflow, "publish workflow")
  check(exact_permissions?(workflow["permissions"], READ_PERMISSIONS),
        "publish workflow defaults are not read-only")

  jobs = workflow_jobs(workflow)
  validation = jobs.fetch("validate-request")
  check(exact_permissions?(validation["permissions"], READ_PERMISSIONS),
        "publication validation permissions are not read-only")
  check_default_branch(job_steps(validation, "publication validation"), "publish workflow")

  caller = jobs.fetch("publish")
  check(exact_permissions?(caller["permissions"], WRITE_PERMISSIONS),
        "publisher call permissions are not exact")
  check(caller["uses"] ==
        "Automattic/kandelo/.github/workflows/reusable-homebrew-bottle-publish.yml@main",
        "publisher does not use the reviewed reusable workflow")
  inputs = caller.fetch("with")
  check(inputs["kandelo-repository"] == "Automattic/kandelo" &&
        inputs["kandelo-ref"] == "main" &&
        inputs["tap-repository"] == "Automattic/kandelo-homebrew" &&
        inputs["tap-ref"] == "main", "publisher does not use first-party main refs")
  check(inputs["dry-run"] == false, "publisher does not pass literal publication mode")

  serialized = workflow.to_s
  check(!serialized.include?("client_payload.kandelo_ref") &&
        !serialized.include?("client_payload.tap_ref"), "publisher accepts executable refs")
end

def check_contract_workflow(workflow)
  expected = { "contents" => "read" }
  check(exact_permissions?(workflow["permissions"], expected),
        "contract-check workflow permissions are not exact")
  uses = values_for_key(workflow, "uses")
  check(uses.include?("actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"),
        "contract-check checkout action is not pinned")
  check(uses.include?("ruby/setup-ruby@d45b1a4e94b71acab930e56e79c6aa188764e7f9"),
        "contract-check Ruby action is not pinned")
  check_common(workflow, "contract-check workflow")
end

def self_test
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
end

begin
  self_test
  check_dry_run(load_workflow(DRY_RUN_PATH))
  check_publish(load_workflow(PUBLISH_PATH))
  check_contract_workflow(load_workflow(CONTRACT_PATH))
  puts "test-workflow-trust.rb: ok"
rescue KeyError, Psych::Exception, RuntimeError => e
  warn "test-workflow-trust.rb: #{e.message}"
  exit 1
end
