require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Erlang < Formula
  include KandeloFormulaSupport

  KANDELO_REGISTRY_BRIDGE = true

  ERTS_VERSION = "16.1.2".freeze
  GUEST_PREFIX = "/home/linuxbrew/.linuxbrew".freeze
  GUEST_OPT_PREFIX = "#{GUEST_PREFIX}/opt/erlang".freeze
  GUEST_OTP_ROOT = "#{GUEST_OPT_PREFIX}/lib/erlang".freeze
  GUEST_ERTS_BIN = "#{GUEST_OTP_ROOT}/erts-#{ERTS_VERSION}/bin".freeze
  desc "Embedded Erlang/OTP runtime for Kandelo"
  homepage "https://www.erlang.org/"
  url "https://github.com/erlang/otp/archive/refs/tags/OTP-28.2.tar.gz"
  version "28.2"
  sha256 "b984f9e02bb61637997a35daa9070ae8f41cea1667676416438c467fda3d141f"
  license "Apache-2.0"
  revision 1

  depends_on "binaryen" => :build
  depends_on "erlang@28" => :build
  depends_on "gnu-tar" => :build
  depends_on "python@3.13" => :build
  depends_on "wabt" => :build
  depends_on "zstd" => :build
  depends_on "kandelo-dev/tap-core/dash" => :test

  skip_clean "bin"
  skip_clean "lib/erlang"
  skip_clean "libexec"

  def install
    kandelo_require_arch!("wasm32")

    # OTP's package bridge runs native bootstrap Erlang and Python, and GNU tar
    # invokes zstd when it seals the deterministic runtime closure. Put only
    # those declared native tools on PATH; target Wasm dependencies remain
    # excluded by the shared Formula support.
    kandelo_prepend_path! formula_opt_bin("erlang@28")
    kandelo_prepend_path! formula_opt_libexec("python@3.13")/"bin"
    kandelo_prepend_path! formula_opt_bin("gnu-tar")
    kandelo_prepend_path! formula_opt_bin("zstd")

    out_dir = kandelo_build_package(script_env: {})
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
      case "$0" in
        */*) erl_script=$0 ;;
        *) erl_script=$(command -v "$0") || exit 127 ;;
      esac
      erl_bindir=${erl_script%/*}
      erl_parent=${erl_bindir%/*}
      case "$erl_bindir" in
        "#{GUEST_OPT_PREFIX}/bin"|"#{GUEST_PREFIX}/Cellar/erlang/"*/bin)
          erl_root="${erl_bindir%/bin}/lib/erlang"
          ;;
        "#{GUEST_PREFIX}/bin"|/bin|/usr/bin)
          erl_root="#{GUEST_OTP_ROOT}"
          ;;
        *)
          if [ -d "$erl_parent/opt/erlang/lib/erlang" ]; then
            erl_root="$erl_parent/opt/erlang/lib/erlang"
          elif [ -d "$erl_parent/lib/erlang" ]; then
            erl_root="$erl_parent/lib/erlang"
          else
            erl_root="#{GUEST_OTP_ROOT}"
          fi
          ;;
      esac
      if [ ! -d "$erl_root" ]; then
        echo "erl: cannot locate the installed Erlang root for $erl_script" >&2
        exit 1
      fi
      ROOTDIR="$erl_root"
      BINDIR="$ROOTDIR/erts-#{ERTS_VERSION}/bin"
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
    wrapper = (bin/"erl").read
    refute_includes wrapper, "ROOTDIR=$("
    assert_equal <<~SH, wrapper
      #!/bin/sh
      case "$0" in
        */*) erl_script=$0 ;;
        *) erl_script=$(command -v "$0") || exit 127 ;;
      esac
      erl_bindir=${erl_script%/*}
      erl_parent=${erl_bindir%/*}
      case "$erl_bindir" in
        "#{GUEST_OPT_PREFIX}/bin"|"#{GUEST_PREFIX}/Cellar/erlang/"*/bin)
          erl_root="${erl_bindir%/bin}/lib/erlang"
          ;;
        "#{GUEST_PREFIX}/bin"|/bin|/usr/bin)
          erl_root="#{GUEST_OTP_ROOT}"
          ;;
        *)
          if [ -d "$erl_parent/opt/erlang/lib/erlang" ]; then
            erl_root="$erl_parent/opt/erlang/lib/erlang"
          elif [ -d "$erl_parent/lib/erlang" ]; then
            erl_root="$erl_parent/lib/erlang"
          else
            erl_root="#{GUEST_OTP_ROOT}"
          fi
          ;;
      esac
      if [ ! -d "$erl_root" ]; then
        echo "erl: cannot locate the installed Erlang root for $erl_script" >&2
        exit 1
      fi
      ROOTDIR="$erl_root"
      BINDIR="$ROOTDIR/erts-#{ERTS_VERSION}/bin"
      EMU=beam
      PROGNAME=erl
      export ROOTDIR BINDIR EMU PROGNAME
      exec "$BINDIR/erlexec" "$@"
    SH

    runtime_maps = lambda do |guest_otp_root|
      runtime_files = {}
      runtime_programs = {}
      otp_root.glob("**/*").select(&:file?).each do |file|
        relative = file.relative_path_from(otp_root)
        guest_path = "#{guest_otp_root}/#{relative}"
        if file.stat.mode.anybits?(0111)
          runtime_programs[guest_path] = file
        else
          runtime_files[guest_path] = file
        end
      end
      [runtime_files, runtime_programs]
    end
    runtime_files, runtime_programs = runtime_maps.call(GUEST_OTP_ROOT)
    assert_operator runtime_files.length, :>, 100
    assert_equal beam, runtime_programs["#{GUEST_ERTS_BIN}/beam.smp"]
    assert_equal child_setup, runtime_programs["#{GUEST_ERTS_BIN}/erl_child_setup"]
    assert_equal erlexec, runtime_programs["#{GUEST_ERTS_BIN}/erlexec"]
    refute_includes (bin/"erl").read, "@@HOMEBREW_PREFIX@@"

    env = {
      "HOME" => "/tmp",
      "PATH" => "#{GUEST_PREFIX}/bin:/usr/bin:/bin",
    }
    base_args = [
      "+S", "1:1", "+A", "0", "+SDio", "1", "+SDcpu", "1:1",
      "-mode", "embedded", "-noshell", "-noinput"
    ]
    dash = formula_opt_bin("kandelo-dev/tap-core/dash")/"dash"
    guest_keg_prefix = "#{GUEST_PREFIX}/Cellar/erlang/#{pkg_version}"
    [
      ["global", "erl", "#{GUEST_PREFIX}/bin/erl", GUEST_OTP_ROOT],
      ["bin-alias", "/bin/erl", "/bin/erl", GUEST_OTP_ROOT],
      ["usr-bin-alias", "/usr/bin/erl", "/usr/bin/erl", GUEST_OTP_ROOT],
      ["opt", "#{GUEST_OPT_PREFIX}/bin/erl", "#{GUEST_OPT_PREFIX}/bin/erl", GUEST_OTP_ROOT],
      ["keg", "#{guest_keg_prefix}/bin/erl", "#{guest_keg_prefix}/bin/erl",
       "#{guest_keg_prefix}/lib/erlang"],
    ].each do |label, command, guest_wrapper, guest_otp_root|
      case_files, case_programs = runtime_maps.call(guest_otp_root)
      case_programs[guest_wrapper] = bin/"erl"
      case_files["/usr/lib/erlang/DECOY"] = otp_root/"releases/28/OTP_VERSION" if label == "usr-bin-alias"
      eval_arg = "io:format(\"erlang-#{label}-ok:~p~n\", [lists:sum([1,2,3])]), halt()."
      node_command = Shellwords.join(["exec", command, *base_args, "-eval", eval_arg])
      assert_equal "erlang-#{label}-ok:6\n", kandelo_run_wasm(
        dash,
        ["-c", node_command],
        argv0:                     "/bin/sh",
        env:                       env,
        exec_programs:             case_programs,
        expected_fork_descendants: 1,
        guest_files:               case_files,
      )
    end

    runtime_programs["#{GUEST_PREFIX}/bin/erl"] = bin/"erl"
    browser_args = [
      *base_args,
      "-eval", 'io:format("erlang-browser-ok:~p~n", [[3,2,1]]), halt().'
    ]
    browser_command = Shellwords.join(["exec", "erl", *browser_args])
    assert_equal "erlang-browser-ok:[3,2,1]\n", kandelo_run_browser_wasm(
      dash,
      ["-c", browser_command],
      argv0:              "sh",
      guest_program_path: "/bin/sh",
      env:                env,
      exec_programs:      runtime_programs,
      guest_files:        runtime_files,
      timeout_ms:         180_000,
    )
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "b0e83c048bce601d2b4552b44339c4f486f1a05d6277333e4ea45180673270e2"
  end

end
