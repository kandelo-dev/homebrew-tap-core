require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Erlang < Formula
  include KandeloFormulaSupport

  ERTS_VERSION = "16.1.2".freeze
  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/erlang".freeze
  GUEST_OTP_ROOT = "#{GUEST_OPT_PREFIX}/lib/erlang".freeze
  GUEST_ERTS_BIN = "#{GUEST_OTP_ROOT}/erts-#{ERTS_VERSION}/bin".freeze
  SOURCE_URL = "https://github.com/erlang/otp/archive/refs/tags/OTP-28.2.tar.gz".freeze
  SOURCE_SHA256 = "b984f9e02bb61637997a35daa9070ae8f41cea1667676416438c467fda3d141f".freeze

  desc "Embedded Erlang/OTP runtime for Kandelo"
  homepage "https://www.erlang.org/"
  url SOURCE_URL
  sha256 SOURCE_SHA256
  license "Apache-2.0"

  depends_on "binaryen" => :build
  depends_on "erlang@28" => :build
  depends_on "gnu-tar" => :build
  depends_on "homebrew/core/zstd" => :build
  depends_on "python@3.13" => :build
  depends_on "wabt" => :build

  skip_clean "bin"
  skip_clean "lib/erlang"
  skip_clean "libexec"

  def install
    kandelo_require_arch!("wasm32")

    # OTP's package bridge runs native bootstrap Erlang and Python, and GNU tar
    # invokes zstd when it seals the deterministic runtime closure. Put only
    # those declared native tools on PATH; target Wasm dependencies remain
    # excluded by KandeloFormulaSupport.
    kandelo_prepend_path! formula_opt_bin("erlang@28")
    kandelo_prepend_path! formula_opt_libexec("python@3.13")/"bin"
    kandelo_prepend_path! formula_opt_bin("gnu-tar")
    kandelo_prepend_path! formula_opt_bin("homebrew/core/zstd")

    out_dir = kandelo_build_package("erlang", "build-erlang.sh", SOURCE_URL, SOURCE_SHA256)
    kandelo_validate_wasm_artifact(out_dir/"erlang.wasm", fork: :required)
    libexec.install out_dir/"erlang.wasm"

    runtime_stage = buildpath/"erlang-runtime-stage"
    runtime_stage.mkpath
    host_tar = OS.mac? ? formula_opt_bin("gnu-tar")/"gtar" : formula_opt_bin("gnu-tar")/"tar"
    system host_tar, "--zstd", "-xf", out_dir/"erlang-otp.tar.zst", "-C", runtime_stage

    beam = runtime_stage/"erts-#{ERTS_VERSION}/bin/beam.smp"
    erlexec = runtime_stage/"erts-#{ERTS_VERSION}/bin/erlexec"
    child_setup = runtime_stage/"erts-#{ERTS_VERSION}/bin/erl_child_setup"
    [beam, erlexec, child_setup, runtime_stage/"bin/start.boot"].each do |required|
      odie "Erlang runtime archive is incomplete: #{required}" unless required.file?
    end
    kandelo_validate_wasm_artifact(beam, fork: :required)
    kandelo_validate_wasm_artifact(erlexec, fork: :auto)
    kandelo_validate_wasm_artifact(child_setup, fork: :auto)

    otp_root = lib/"erlang"
    otp_root.mkpath
    cp_r runtime_stage.children, otp_root

    (bin/"erl").write <<~SH
      #!/bin/sh
      ROOTDIR=#{GUEST_OTP_ROOT.shellescape}
      BINDIR=#{GUEST_ERTS_BIN.shellescape}
      EMU=beam
      PROGNAME=erl
      export ROOTDIR BINDIR EMU PROGNAME
      exec "$BINDIR/erlexec" "$@"
    SH
    chmod 0755, bin/"erl"
  end

  test do
    otp_root = lib/"erlang"
    erts_bin = otp_root/"erts-#{ERTS_VERSION}/bin"
    erlexec = erts_bin/"erlexec"
    beam = erts_bin/"beam.smp"
    child_setup = erts_bin/"erl_child_setup"
    [erlexec, beam, child_setup, otp_root/"bin/start.boot"].each { |path| assert_path_exists path }
    assert_equal <<~SH, (bin/"erl").read
      #!/bin/sh
      ROOTDIR=#{GUEST_OTP_ROOT.shellescape}
      BINDIR=#{GUEST_ERTS_BIN.shellescape}
      EMU=beam
      PROGNAME=erl
      export ROOTDIR BINDIR EMU PROGNAME
      exec "$BINDIR/erlexec" "$@"
    SH

    runtime_files = {}
    runtime_programs = {}
    otp_root.glob("**/*").select(&:file?).each do |file|
      relative = file.relative_path_from(otp_root)
      guest_path = "#{GUEST_OTP_ROOT}/#{relative}"
      if file.stat.executable?
        runtime_programs[guest_path] = file
      else
        runtime_files[guest_path] = file
      end
    end
    assert_operator runtime_files.length, :>, 100
    assert_equal beam, runtime_programs["#{GUEST_ERTS_BIN}/beam.smp"]
    assert_equal child_setup, runtime_programs["#{GUEST_ERTS_BIN}/erl_child_setup"]
    assert_equal erlexec, runtime_programs["#{GUEST_ERTS_BIN}/erlexec"]

    env = {
      "BINDIR"   => GUEST_ERTS_BIN,
      "EMU"      => "beam",
      "HOME"     => "/tmp",
      "PROGNAME" => "erl",
      "ROOTDIR"  => GUEST_OTP_ROOT,
    }
    base_args = [
      "+S", "1:1", "+A", "0", "+SDio", "1", "+SDcpu", "1:1",
      "-mode", "embedded", "-noshell", "-noinput",
      "-boot", "#{GUEST_OTP_ROOT}/releases/28/start_clean"
    ]
    node_args = [
      *base_args,
      "-eval", 'io:format("erlang-node-ok:~p~n", [lists:sum([1,2,3])]), halt().'
    ]
    assert_equal "erlang-node-ok:6\n", kandelo_run_wasm(
      erlexec,
      node_args,
      env:                       env,
      exec_programs:             runtime_programs,
      expected_fork_descendants: 1,
      guest_files:               runtime_files,
    )

    browser_args = [
      *base_args,
      "-eval", 'io:format("erlang-browser-ok:~p~n", [[3,2,1]]), halt().'
    ]
    assert_equal "erlang-browser-ok:[3,2,1]\n", kandelo_run_browser_wasm(
      erlexec,
      browser_args,
      argv0:         "erlexec",
      env:           env,
      exec_programs: runtime_programs,
      guest_files:   runtime_files,
      timeout_ms:    180_000,
    )
  end
end
