# typed: strict
# frozen_string_literal: true

require "minitest/autorun"
# Standalone Ruby does not preload Homebrew's Pathname helper.
require "pathname" # rubocop:disable Lint/RedundantRequireStatement
require_relative "../kandelo_formula_support"

# Regression coverage for Formula runtime execution evidence.
class KandeloFormulaSupportTest < Minitest::Test
  # Minimal Formula double for command-construction tests.
  class Harness
    include KandeloFormulaSupport

    attr_reader :command

    def kandelo_require_root!
      "/tmp/kandelo root"
    end

    def testpath
      Pathname("/tmp/formula test")
    end

    def shell_output(command)
      @command = command
      "runtime-ok\n"
    end

    def kandelo_record_node_execution!(_wasm_path, _argv); end
  end

  def test_node_execution_receipt_is_optional
    previous = ENV.delete("HOMEBREW_KANDELO_NODE_RECEIPT_PATH")

    assert_nil Harness.new.kandelo_record_node_execution!("program.wasm", [])
  ensure
    ENV["HOMEBREW_KANDELO_NODE_RECEIPT_PATH"] = previous if previous
  end

  def test_network_execution_uses_tap_owned_runner
    harness = Harness.new
    output = harness.kandelo_run_wasm(
      "program.wasm", ["a b"], env: { "TOKEN" => "x y" }, network: true
    )

    assert_equal "runtime-ok\n", output
    assert_includes harness.command, "run-network-wasm.ts"
    assert_includes harness.command, "/tmp/kandelo\\ root"
    assert_includes harness.command, "KANDELO_FORMULA_GUEST_ENV_JSON="
    assert_includes harness.command, "TOKEN"
    refute_includes harness.command, "TOKEN=x\\ y"
    assert_includes harness.command, "program.wasm a\\ b"
    refute_includes harness.command, "examples/run-example.ts"
  end

  def test_default_execution_keeps_standard_runner
    harness = Harness.new

    harness.kandelo_run_wasm("program.wasm", [])

    assert_includes harness.command, "examples/run-example.ts"
    refute_includes harness.command, "run-network-wasm.ts"
  end
end
