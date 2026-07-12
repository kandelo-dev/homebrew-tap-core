require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Sharutils < Formula
  include KandeloFormulaSupport

  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/sharutils".freeze

  desc "GNU shell archive and uuencoding tools for Kandelo"
  homepage "https://savannah.gnu.org/projects/sharutils/"
  url "https://ftpmirror.gnu.org/gnu/sharutils/sharutils-4.15.2.tar.xz"
  sha256 "2b05cff7de5d7b646dc1669bc36c35fdac02ac6ae4b6c19cb3340d87ec553a9a"
  license "GPL-3.0-or-later"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/bzip2"
  depends_on "automattic/kandelo-homebrew/coreutils"
  depends_on "automattic/kandelo-homebrew/dash"
  depends_on "automattic/kandelo-homebrew/grep"
  depends_on "automattic/kandelo-homebrew/gzip"
  depends_on "automattic/kandelo-homebrew/sed"
  depends_on "automattic/kandelo-homebrew/xz"

  skip_clean "bin/shar", "bin/unshar", "bin/uuencode", "bin/uudecode"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/sharutils-#{version}"
      prefix_maps = {
        buildpath.to_s => stable_source,
        root.to_s      => "/usr/src/kandelo",
        "/nix/store"   => "/usr/src/toolchain",
      }.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end
      ENV["CFLAGS"] = ["-O2", "-gline-tables-only", *prefix_maps].join(" ")

      # Upstream runs a discovered host `compress` and treats that result as a
      # target feature probe. Suppress it; the deprecated compress mode is not
      # part of this target, while bzip2, gzip, and xz are declared below.
      ENV["ac_cv_path_COMPRESS"] = "no"

      # The 2015 release's generated headers rely on pre-GCC-10 common-symbol
      # behavior. Keep the single initialized definition in each opts source.
      %w[shar unshar uuencode uudecode].each do |program|
        inreplace "src/#{program}-opts.h",
          /^char const \* const program_name;$/,
          "extern char const * const program_name;"
      end

      # `egrep` is obsolescent and is not a separate Kandelo package surface.
      # Keep this replacement byte-sized: scripts.x has fixed string offsets.
      %w[src/scripts.def src/scripts.x].each do |script|
        inreplace script,
          " | egrep ",
          "|grep -E "
      end

      # Kandelo reports 128 MiB of guest memory. XZ preset 9 refuses that
      # limit, while preset 6 is XZ's own default and fits the machine.
      inreplace "src/shar-opts.def", "arg-default = 9;", "arg-default = 6;"
      inreplace "src/shar-opts.def", "default is @code{9}", "default is @code{6}"
      inreplace "src/shar-opts.c",
        "#define LEVEL_OF_COMPRESSION_DFT_ARG   ((char const*)9)",
        "#define LEVEL_OF_COMPRESSION_DFT_ARG   ((char const*)6)"
      inreplace "src/shar-opts.c", "default is @code{9}", "default is @code{6}"
      inreplace "doc/invoke-shar.texi", "default is @code{9}", "default is @code{6}"
      inreplace "doc/sharutils.info", "The default is `9'", "The default is `6'"
      inreplace "doc/shar.1", 'default is \fB9\fP', 'default is \fB6\fP'
      inreplace "doc/shar.1",
        "for this option is:\n.ti +4\n 9\n.sp\nSome compression programs",
        "for this option is:\n.ti +4\n 6\n.sp\nSome compression programs"

      system kandelo_configure(root),
        "--prefix=#{GUEST_OPT_PREFIX}",
        "--disable-compress-link",
        "--disable-dependency-tracking"
      system "make", "-j#{ENV.make_jobs}"

      stage = buildpath/"kandelo-stage"
      system "make", "install", "DESTDIR=#{stage}"
      staged_prefix = stage/GUEST_OPT_PREFIX.delete_prefix("/")
      odie "sharutils did not install into the guest opt prefix" unless staged_prefix.directory?

      shar = staged_prefix/"bin/shar"
      kandelo_fork_instrument(shar)
      chmod 0755, shar
      kandelo_validate_wasm_artifact(shar, fork: :required)
      %w[unshar uuencode uudecode].each do |program|
        kandelo_validate_wasm_artifact(staged_prefix/"bin"/program, fork: :forbidden)
      end
      prefix.install staged_prefix.children
    end
  end

  test do
    %w[shar unshar uuencode uudecode].each do |program|
      assert_match(/#{program} \(GNU sharutils\) 4\.15\.2/,
        kandelo_run_wasm(bin/program, ["--version"]))
      assert_path_exists man1/"#{program}.1"
    end
    assert_path_exists info/"sharutils.info"
    %w[compress compress-dummy].each do |program|
      refute_path_exists bin/program
    end
    manpage = (man1/"shar.1").read
    assert_match(/for this option is:\n\.ti \+4\n 6\n/, manpage)
    refute_match(/for this option is:\n\.ti \+4\n 9\n/, manpage)
    refute_match(/--compress\b/, kandelo_run_wasm(bin/"shar", ["--help"]))

    payload = testpath/"payload.bin"
    payload.binwrite("Kandelo\0uuencode\n\xFF".b)
    mounts = { "/work" => testpath }
    env = { "KERNEL_CWD" => "/work" }

    classic = kandelo_run_wasm(
      bin/"uuencode", ["payload.bin", "classic.out"],
      env: env, writable_host_directories: mounts
    )
    assert_match(/^begin 644 classic\.out$/, classic)
    (testpath/"classic.uue").write(classic)
    assert_empty kandelo_run_wasm(
      bin/"uudecode", ["classic.uue"],
      env: env, writable_host_directories: mounts
    )
    assert_equal payload.binread, (testpath/"classic.out").binread

    base64 = kandelo_run_wasm(
      bin/"uuencode", ["--base64", "payload.bin", "base64.out"],
      env: env, writable_host_directories: mounts
    )
    assert_match(/^begin-base64 644 base64\.out$/, base64)
    (testpath/"base64.uue").write(base64)
    assert_empty kandelo_run_wasm(
      bin/"uudecode", ["base64.uue"],
      env: env, writable_host_directories: mounts
    )
    assert_equal payload.binread, (testpath/"base64.out").binread

    coreutils = formula_opt_bin("automattic/kandelo-homebrew/coreutils")
    shell = formula_opt_bin("automattic/kandelo-homebrew/dash")/"dash"
    programs = {
      "/bin/bzip2"    => formula_opt_bin("automattic/kandelo-homebrew/bzip2")/"bzip2",
      "/bin/chmod"    => coreutils/"chmod",
      "/bin/grep"     => formula_opt_bin("automattic/kandelo-homebrew/grep")/"grep",
      "/bin/gzip"     => formula_opt_bin("automattic/kandelo-homebrew/gzip")/"gzip",
      "/bin/md5sum"   => coreutils/"md5sum",
      "/bin/mkdir"    => coreutils/"mkdir",
      "/bin/rm"       => coreutils/"rm",
      "/bin/sed"      => formula_opt_bin("automattic/kandelo-homebrew/sed")/"sed",
      "/bin/sh"       => shell,
      "/bin/touch"    => coreutils/"touch",
      "/bin/uuencode" => bin/"uuencode",
      "/bin/uudecode" => bin/"uudecode",
      "/bin/wc"       => coreutils/"wc",
      "/bin/xz"       => formula_opt_bin("automattic/kandelo-homebrew/xz")/"xz",
    }
    shell_env = env.merge("KERNEL_PATH" => "/bin")
    {
      "plain" => "--uuencode",
      "bzip2" => "--bzip2",
      "gzip"  => "--gzip",
      "xz"    => "--compactor=xz",
    }.each do |label, mode|
      archive_name = "payload-#{label}.shar"
      archive = kandelo_run_wasm(
        bin/"shar", [mode, "--quiet", "payload.bin"],
        env:                       shell_env,
        exec_programs:             programs,
        writable_host_directories: mounts,
        expected_fork_descendants: 1
      )
      assert_match(/md5sum/, archive)
      assert_match(/wc -c/, archive)
      assert_match(/\|grep -E /, archive)
      refute_match(/\begrep\b/, archive)
      (testpath/archive_name).write(archive)

      unpacked = testpath/"unpacked-#{label}"
      unpacked.mkpath
      unshar_output = kandelo_run_wasm(
        bin/"unshar", ["--directory=/work/#{unpacked.basename}", archive_name],
        env: shell_env, merge_stderr: true, exec_programs: programs,
        writable_host_directories: mounts
      )
      assert_match(/#{Regexp.escape(archive_name)}:/, unshar_output)
      refute_match(/not verifying md5sums/i, unshar_output)
      assert_equal payload.binread, (unpacked/"payload.bin").binread
    end
  end
end
