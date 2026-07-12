require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Pax < Formula
  include KandeloFormulaSupport

  desc "Portable archive interchange utility for Kandelo"
  homepage "https://www.mirbsd.org/pax.htm"
  url "https://mbsd.evolvis.org/MirOS/dist/mir/cpio/paxmirabilis-20240817.tgz"
  sha256 "e955d5d3af97aede0a3f463a9a59b83e8d1083aaf142eb6f388c549a7d182e6b"
  license all_of: ["BSD-3-Clause", "MirOS", "ISC", "CC0-1.0"]

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/musl-fts"

  skip_clean "bin/pax"

  def install
    kandelo_require_arch!("wasm32")
    musl_fts = formula_opt_prefix("automattic/kandelo-homebrew/musl-fts")
    artifact = buildpath/"paxpax"

    kandelo_wasm_build do |root|
      stable_source = "/usr/src/paxmirabilis-#{version}"
      prefix_maps = {
        buildpath.to_s => stable_source,
        root.to_s      => "/usr/src/kandelo",
        musl_fts.to_s  => "/usr/src/musl-fts",
        "/nix/store"   => "/usr/src/toolchain",
      }.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end

      ENV["CC"] = "#{kandelo_arch}posix-cc"
      ENV["CFLAGS"] = [
        "-O2",
        "-gline-tables-only",
        "-fdebug-compilation-dir=#{stable_source}",
        *prefix_maps,
      ].join(" ")
      ENV["CPPFLAGS"] = [
        "-D_GNU_SOURCE",
        "-I#{musl_fts}/include",
        '-DPAX_SAFE_PATH=\"/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin\"',
      ].join(" ")
      ENV["LIBS"] = (musl_fts/"lib/libfts.a").to_s
      ENV["TARGET_OS"] = "Kandelo"
      ENV["TARGET_OSREV"] = "1.0.0"

      # Kandelo executables intentionally leave kernel imports unresolved, so
      # upstream's undefined-symbol linker-failure probe cannot be meaningful.
      # The remaining declaration and API probes still determine target facts.
      ENV["HAVE_COMPILER_FAILS"] = "0"

      system "sh", "Build.sh", "-r", "-tpax"
      odie "paxmirabilis did not produce #{artifact}" unless artifact.file?

      kandelo_fork_instrument(artifact)
      kandelo_validate_wasm_artifact(
        artifact,
        fork:            :required,
        forbidden_paths: [musl_fts.to_s],
      )
    end

    kandelo_install_bin(buildpath, artifact.basename, "pax")
    bin.install_symlink "pax" => "paxcpio"
    bin.install_symlink "pax" => "paxtar"
    man1.install "mans/pax.1", "mans/paxcpio.1", "mans/paxtar.1"
  end

  test do
    assert_equal "pax", (bin/"paxcpio").readlink.to_s
    assert_equal "pax", (bin/"paxtar").readlink.to_s
    assert_path_exists man1/"pax.1"
    assert_path_exists man1/"paxcpio.1"
    assert_path_exists man1/"paxtar.1"

    workspace = testpath/"workspace"
    source = workspace/"source"
    (source/"nested").mkpath
    (source/"empty").mkpath
    (source/"root.txt").write "root payload\n"
    (source/"nested/child.txt").write "nested payload\n"
    ln_s "../root.txt", source/"nested/root-link"

    mount = { "/work" => workspace }
    work_env = { "KERNEL_CWD" => "/work" }
    kandelo_run_wasm(
      bin/"pax", ["-w", "-x", "ustar", "-f", "archive.tar", "source"],
      env: work_env, writable_host_directories: mount
    )

    archive = (workspace/"archive.tar").binread
    assert_operator archive.bytesize, :>=, 1_024
    assert_equal "ustar", archive.byteslice(257, 5)

    tar_listing = kandelo_run_wasm(
      bin/"paxtar", ["-tf", "archive.tar"],
      env: work_env, preserve_argv0: true, writable_host_directories: mount
    ).lines.map(&:chomp)
    assert_includes tar_listing, "source/root.txt"
    assert_includes tar_listing, "source/nested/child.txt"

    cpio_archive = kandelo_run_wasm(
      bin/"paxcpio", ["-o", "-H", "ustar"],
      env: work_env, stdin: "source/root.txt\n", preserve_argv0: true,
      writable_host_directories: mount
    ).b
    assert_equal "ustar", cpio_archive.byteslice(257, 5)
    assert_includes kandelo_run_wasm(
      bin/"paxcpio", ["-it"], stdin: cpio_archive, preserve_argv0: true
    ).lines.map(&:chomp), "source/root.txt"

    extracted = workspace/"extracted"
    extracted.mkpath
    kandelo_run_wasm(
      bin/"pax", ["-r", "-f", "/work/archive.tar"],
      env:                       { "KERNEL_CWD" => "/work/extracted" },
      writable_host_directories: mount
    )
    assert_equal "root payload\n", (extracted/"source/root.txt").read
    assert_equal "nested payload\n", (extracted/"source/nested/child.txt").read
    assert_predicate extracted/"source/empty", :directory?
    assert_predicate extracted/"source/nested/root-link", :symlink?
    assert_equal "../root.txt", (extracted/"source/nested/root-link").readlink.to_s
  end
end
