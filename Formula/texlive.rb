require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Texlive < Formula
  include KandeloFormulaSupport

  GUEST_PREFIX = "/home/linuxbrew/.linuxbrew/opt/texlive".freeze
  GUEST_TEXMF = "#{GUEST_PREFIX}/share/texmf-dist".freeze
  TEXLIVE_SNAPSHOT = "20250308".freeze
  RUNTIME_COLLECTIONS = {
    "collection-basic"            => "72890",
    "collection-fontsrecommended" => "54074",
    "collection-latex"            => "73720",
    "collection-latexrecommended" => "73720",
    "collection-mathscience"      => "74351",
    "collection-pictures"         => "74418",
  }.freeze
  RUNTIME_EXTENSIONS = %w[
    .sty .cls .clo .def .cfg .fd .ldf .tfm .vf .pfb .pfa .map .enc .tex
    .ltx .ini .cnf .tcx
  ].freeze
  RUNTIME_SKIP_COMPONENTS = %w[
    doc source man info context luatex xetex lualatex xelatex platex uplatex
    ptex uptex eptex
  ].freeze

  desc "Typesetting engine and curated TeX Live runtime for Kandelo"
  homepage "https://www.tug.org/texlive/"
  url "https://ftp.math.utah.edu/pub/tex/historic/systems/texlive/2025/texlive-20250308-source.tar.xz"
  mirror "https://ftp.tu-chemnitz.de/pub/tug/historic/systems/texlive/2025/texlive-20250308-source.tar.xz"
  sha256 "fffdb1a3d143c177a4398a2229a40d6a88f18098e5f6dcfd57648c9f2417490f"
  license :cannot_represent

  depends_on "binaryen" => :build
  depends_on "pkgconf" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/libpng"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "bin"

  # The engine, package ledger, and texmf tree are all the immutable TeX Live
  # 20250308 snapshot. The mutable tlnet repository and install-tl transaction
  # used by the registry recipe are intentionally not part of this formula.
  resource "texlive-extra" do
    url "https://ftp.math.utah.edu/pub/tex/historic/systems/texlive/2025/texlive-20250308-extra.tar.xz"
    mirror "https://ftp.tu-chemnitz.de/pub/tug/historic/systems/texlive/2025/texlive-20250308-extra.tar.xz"
    version TEXLIVE_SNAPSHOT
    sha256 "ea69cfecbc9b138acbc45476e8cb4d9357f5e4e45fd12b3bf9ceabbebd7669d2"
  end

  resource "texlive-texmf" do
    url "https://ftp.math.utah.edu/pub/tex/historic/systems/texlive/2025/texlive-20250308-texmf.tar.xz"
    mirror "https://ftp.tu-chemnitz.de/pub/tug/historic/systems/texlive/2025/texlive-20250308-texmf.tar.xz"
    version TEXLIVE_SNAPSHOT
    sha256 "08dcda7430bf0d2f6ebb326f1e197e1473d3f7cc0984a2adb7236df45316c7cf"
  end

  def install
    kandelo_require_arch!("wasm32")
    root = Pathname(kandelo_require_root!)
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    libpng = formula_opt_prefix("automattic/kandelo-homebrew/libpng")
    build_script = Pathname(__dir__).parent/"Kandelo/formula_support/build-texlive-pdftex.sh"

    tlpdb = buildpath/"texlive.tlpdb"
    resource("texlive-extra").stage do
      cp "tlpkg/texlive.tlpdb", tlpdb
      (pkgshare/"licenses").install "LICENSE.CTAN", "LICENSE.TL"
    end

    runtime_files, runtime_packages = texlive_runtime_contract(tlpdb)
    write_runtime_provenance(runtime_packages)

    # Extract only the files owned by the six registry collection roots. The
    # 4.3 GB upstream texmf snapshot stays one hashed Homebrew resource, while
    # the bottle contains the actual macro/font tree rather than a demo JSON
    # wrapper or an install-tl side effect.
    archive_manifest = buildpath/"texlive-runtime-archive-files.txt"
    archive_manifest.write runtime_files.map { |path| "texlive-#{TEXLIVE_SNAPSHOT}-texmf/#{path}\n" }.join
    share.mkpath
    system kandelo_host_tool("tar"),
      "-xJf", resource("texlive-texmf").cached_download,
      "-C", share,
      "--strip-components=1",
      "-T", archive_manifest
    verify_runtime_files!(runtime_files)

    texmf_dist = share/"texmf-dist"
    write_runtime_config(texmf_dist)
    write_language_config(texmf_dist)

    # Kpathsea's redistributor layout resolves share/texmf-dist from the real
    # guest opt symlink without embedding the build machine's Cellar path.
    inreplace buildpath/"texk/kpathsea/texmf.cnf",
      /^TEXMFROOT = .*$/,
      "TEXMFROOT = $SELFAUTODIR/share"

    host_build = buildpath/"host-build"
    cross_build = buildpath/"cross-build"
    linked_pdftex = buildpath/"pdftex.wasm"
    jobs = [ENV.make_jobs, 2].min
    host_bash = kandelo_host_tool("bash")
    system host_bash, build_script, "engine",
      buildpath, host_build, cross_build, root, zlib, libpng,
      linked_pdftex, GUEST_PREFIX, jobs

    fixture = buildpath/"kandelo-texlive-smoke.tex"
    fixture.write texlive_test_document
    format_work = buildpath/"format-work"
    host_smoke = buildpath/"host-smoke"
    system host_bash, build_script, "formats",
      host_build/"texk/web2c/pdftex", texmf_dist, format_work, fixture, host_smoke

    test_files = recorded_texmf_inputs(host_smoke/"input.fls", texmf_dist)
    (pkgshare/"test-files.txt").write "#{test_files.join("\n")}\n"

    # pdfTeX has real system()/popen() paths for shell escape and font
    # generation, so it is a fork-using program even though the smoke keeps
    # shell escape disabled. Apply and verify the current Kandelo continuation
    # ABI rather than shipping an uninstrumented executable.
    kandelo_fork_instrument(linked_pdftex)
    verify_wasm_contract!(linked_pdftex, root)
    verify_builder_paths!(linked_pdftex, texmf_dist, zlib, libpng, root)

    kandelo_install_bin(buildpath, "pdftex.wasm", "pdftex")
    bin.install_symlink "pdftex" => "pdflatex"
    bin.install_symlink "pdftex" => "latex"
  end

  test do
    texmf_dist = share/"texmf-dist"
    assert_path_exists bin/"pdftex"
    assert_path_exists bin/"pdflatex"
    assert_path_exists bin/"latex"
    assert_path_exists texmf_dist/"web2c/pdftex/pdftex.fmt"
    assert_path_exists texmf_dist/"web2c/pdftex/pdflatex.fmt"
    assert_path_exists texmf_dist/"web2c/pdftex/latex.fmt"
    assert_path_exists texmf_dist/"tex/latex/base/article.cls"
    assert_path_exists texmf_dist/"tex/latex/amsmath/amsmath.sty"
    assert_path_exists texmf_dist/"tex/latex/pgf/frontendlayer/tikz.sty"
    assert_path_exists texmf_dist/"fonts/tfm/public/cm/cmr10.tfm"

    input = testpath/"input.tex"
    input.write texlive_test_document
    (testpath/"home").mkpath
    guest_files = {}
    (pkgshare/"test-files.txt").each_line do |line|
      relative = line.strip
      next if relative.empty?

      host_file = texmf_dist/relative
      assert_path_exists host_file
      guest_files["#{GUEST_TEXMF}/#{relative}"] = host_file
    end
    assert_operator guest_files.length, :>, 20

    guest_pdftex = "#{GUEST_PREFIX}/bin/pdflatex"
    output = kandelo_run_wasm(
      bin/"pdflatex",
      [
        "-progname=pdflatex",
        "-fmt=pdflatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-output-format=pdf",
        "-output-directory=/work",
        "/work/input.tex",
      ],
      argv0:                     guest_pdftex,
      env:                       {
        "HOME"       => "/work/home",
        "KERNEL_CWD" => "/work",
        "TEXMF"      => GUEST_TEXMF,
        "TEXMFCNF"   => "#{GUEST_TEXMF}/web2c",
        "TEXMFDIST"  => GUEST_TEXMF,
        "TEXMFVAR"   => "/work/texmf-var",
        "TIMEOUT"    => "120000",
      },
      exec_programs:             { guest_pdftex => bin/"pdflatex" },
      guest_files:               guest_files,
      merge_stderr:              true,
      writable_host_directories: { "/work" => testpath.realpath },
    )
    assert_match(/This is pdfTeX .*TeX Live 2025/, output)
    assert_match(/Output written on .*input\.pdf \(1 page, [0-9]+ bytes\)/, output)

    pdf = testpath/"input.pdf"
    assert_path_exists pdf
    pdf_bytes = pdf.binread
    assert_operator pdf_bytes.bytesize, :>, 1_000
    assert pdf_bytes.start_with?("%PDF-"), "pdfTeX output has no PDF header"
    pdf_tail = pdf_bytes.byteslice([pdf_bytes.bytesize - 1_024, 0].max, 1_024)
    assert_includes pdf_tail, "%%EOF"

    guest_latex = "#{GUEST_PREFIX}/bin/latex"
    latex_guest_files = guest_files.merge(
      "#{GUEST_TEXMF}/web2c/pdftex/latex.fmt" => texmf_dist/"web2c/pdftex/latex.fmt",
    )
    latex_output = kandelo_run_wasm(
      bin/"latex",
      [
        "-progname=latex",
        "-fmt=latex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-jobname=latex-input",
        "-output-directory=/work",
        "/work/input.tex",
      ],
      argv0:                     guest_latex,
      env:                       {
        "HOME"       => "/work/home",
        "KERNEL_CWD" => "/work",
        "TEXMF"      => GUEST_TEXMF,
        "TEXMFCNF"   => "#{GUEST_TEXMF}/web2c",
        "TEXMFDIST"  => GUEST_TEXMF,
        "TEXMFVAR"   => "/work/texmf-var",
        "TIMEOUT"    => "120000",
      },
      exec_programs:             { guest_latex => bin/"latex" },
      guest_files:               latex_guest_files,
      merge_stderr:              true,
      writable_host_directories: { "/work" => testpath.realpath },
    )
    assert_match(/Output written on .*latex-input\.dvi \(1 page, [0-9]+ bytes\)/, latex_output)
    dvi = (testpath/"latex-input.dvi").binread
    assert_operator dvi.bytesize, :>, 100
    assert_equal [247, 2], dvi.bytes.first(2), "LaTeX output has no DVI preamble"
    assert_includes dvi.bytes.last(16), 249, "LaTeX output has no DVI post_post opcode"

    browser_root = "/home/texlive-test"
    browser_guest_files = guest_files.merge("#{browser_root}/input.tex" => input)
    browser_output = kandelo_run_browser_wasm(
      bin/"pdflatex",
      [
        "-progname=pdflatex",
        "-fmt=pdflatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-output-format=pdf",
        "-output-directory=#{browser_root}",
        "#{browser_root}/input.tex",
      ],
      argv0:       "pdflatex",
      env:         {
        "HOME"      => browser_root,
        "TEXMF"     => GUEST_TEXMF,
        "TEXMFCNF"  => "#{GUEST_TEXMF}/web2c",
        "TEXMFDIST" => GUEST_TEXMF,
        "TEXMFVAR"  => "#{browser_root}/texmf-var",
      },
      guest_files: browser_guest_files,
      timeout_ms:  120_000,
    )
    assert_match(/Output written on .*input\.pdf \(1 page, [0-9]+ bytes\)/, browser_output)
  end

  private

  def texlive_runtime_contract(tlpdb)
    packages = {}
    tlpdb.read.split(/\n\n+/).each do |paragraph|
      lines = paragraph.lines(chomp: true)
      name_line = lines.find { |line| line.start_with?("name ") }
      next if name_line.nil?

      name = name_line.delete_prefix("name ")
      revision = lines.find { |line| line.start_with?("revision ") }&.split&.last
      dependencies = lines.grep(/^depend /).map { |line| line.delete_prefix("depend ") }
      runfiles = []
      if (runfiles_index = lines.index { |line| line.start_with?("runfiles ") })
        lines[(runfiles_index + 1)..].each do |line|
          break unless line.start_with?(" ")

          runfiles << line.strip.split(" details=", 2).first
        end
      end
      packages[name] = { revision: revision, dependencies: dependencies, runfiles: runfiles }
    end

    RUNTIME_COLLECTIONS.each do |name, expected_revision|
      actual_revision = packages.fetch(name).fetch(:revision)
      if actual_revision != expected_revision
        odie "#{name} revision #{actual_revision} does not match #{expected_revision}"
      end
    end

    selected = {}
    queue = RUNTIME_COLLECTIONS.keys.dup
    until queue.empty?
      name = queue.shift
      next if selected.key?(name)

      package = packages[name]
      odie "TeX Live dependency #{name} is absent from the pinned tlpdb" if package.nil?

      selected[name] = package
      package.fetch(:dependencies).each do |dependency|
        next if dependency.end_with?(".ARCH") || dependency.include?("/")

        queue << dependency
      end
    end
    odie "expected 784 TeX Live runtime packages, found #{selected.length}" if selected.length != 784

    runtime_files = selected.values.flat_map { |package| package.fetch(:runfiles) }.uniq.select do |path|
      components = path.split("/")
      path.start_with?("texmf-dist/") &&
        !components.intersect?(RUNTIME_SKIP_COMPONENTS) &&
        RUNTIME_EXTENSIONS.any? { |extension| path.end_with?(extension) }
    end
    runtime_files << "texmf-dist/fonts/map/pdftex/updmap/pdftex.map"
    runtime_files = runtime_files.uniq.sort
    runtime_files.each do |path|
      odie "unsafe TeX Live runtime path: #{path}" if Pathname(path).cleanpath.to_s != path || path.include?("\0")
    end
    odie "expected 10240 TeX Live runtime files, found #{runtime_files.length}" if runtime_files.length != 10_240

    [runtime_files, selected]
  end

  def write_runtime_provenance(packages)
    lines = [
      "TeX Live snapshot: #{TEXLIVE_SNAPSHOT}",
      "source sha256: fffdb1a3d143c177a4398a2229a40d6a88f18098e5f6dcfd57648c9f2417490f",
      "texmf sha256: 08dcda7430bf0d2f6ebb326f1e197e1473d3f7cc0984a2adb7236df45316c7cf",
      "extra sha256: ea69cfecbc9b138acbc45476e8cb4d9357f5e4e45fd12b3bf9ceabbebd7669d2",
      "collection roots:",
      *RUNTIME_COLLECTIONS.map { |name, revision| "  #{name}@#{revision}" },
      "resolved packages:",
      *packages.sort.map { |name, package| "  #{name}@#{package.fetch(:revision)}" },
    ]
    (pkgshare/"runtime-packages.txt").write "#{lines.join("\n")}\n"
  end

  def verify_runtime_files!(runtime_files)
    runtime_files.each do |relative|
      installed = share/relative
      odie "TeX Live resource omitted #{relative}" unless installed.file?
      odie "TeX Live runtime path is a symlink: #{relative}" if installed.symlink?
    end
  end

  def write_runtime_config(texmf_dist)
    (texmf_dist/"web2c").mkpath
    (texmf_dist/"web2c/texmf.cnf").write <<~CNF
      % Kandelo pdfTeX runtime paths
      TEXMFDIST = #{GUEST_TEXMF}
      TEXMF = {$TEXMFDIST}
      TEXMFCNF = #{GUEST_TEXMF}/web2c
      TEXINPUTS = .;$TEXMF/tex/{latex,generic,}//
      TFMFONTS = .;$TEXMF/fonts/tfm//
      T1FONTS = .;$TEXMF/fonts/type1//
      AFMFONTS = .;$TEXMF/fonts/afm//
      VFFONTS = .;$TEXMF/fonts/vf//
      ENCFONTS = .;$TEXMF/fonts/enc//
      TEXFONTMAPS = .;$TEXMF/fonts/map/{pdftex,}//
      TEXPSHEADERS = .;$TEXMF/fonts/type1//;$TEXMF/fonts/enc//
      TEXFORMATS = .;$TEXMF/web2c/{pdftex,}
      MFINPUTS = .;$TEXMF/metafont//;$TEXMF/fonts/source//
      TEX_HUSH = all
    CNF
  end

  def write_language_config(texmf_dist)
    config_dir = texmf_dist/"tex/generic/config"
    config_dir.mkpath
    (config_dir/"language.dat").write <<~LANGUAGE
      english hyphen.tex
      =usenglish
      =USenglish
      =american
      dumylang dumyhyph.tex
      nohyphenation zerohyph.tex
      ukenglish loadhyph-en-gb.tex
      =british
      =UKenglish
      usenglishmax loadhyph-en-us.tex
    LANGUAGE
  end

  def recorded_texmf_inputs(fls, texmf_dist)
    prefix = "#{texmf_dist}/"
    inputs = fls.readlines.filter_map do |line|
      next unless line.start_with?("INPUT ")

      path = line.delete_prefix("INPUT ").strip
      next unless path.start_with?(prefix)

      path.delete_prefix(prefix)
    end
    inputs |= %w[web2c/texmf.cnf web2c/pdftex/pdflatex.fmt]
    inputs.sort!
    odie "host pdfTeX smoke recorded too few runtime inputs" if inputs.length <= 20
    inputs
  end

  def verify_wasm_contract!(wasm, root)
    guards = root/"scripts/wasm-artifact-guards.sh"
    system "bash", "-c", <<~SH
      set -euo pipefail
      . #{guards.to_s.shellescape}
      expected_abi=$(wasm_current_abi_version #{root.to_s.shellescape})
      artifact_abi=$(wasm_extract_abi_version #{wasm.to_s.shellescape})
      if [ -z "$expected_abi" ] || [ "$artifact_abi" != "$expected_abi" ]; then
        echo "ERROR: pdfTeX ABI $artifact_abi does not match Kandelo ABI $expected_abi" >&2
        exit 1
      fi
      wasm_require_no_legacy_asyncify #{wasm.to_s.shellescape}
      if ! wasm_has_complete_fork_instrumentation #{wasm.to_s.shellescape}; then
        echo "ERROR: pdfTeX has incomplete fork instrumentation" >&2
        exit 1
      fi
    SH
  end

  def verify_builder_paths!(wasm, texmf_dist, zlib, libpng, root)
    markers = [
      prefix.to_s,
      buildpath.to_s,
      root.to_s,
      zlib.to_s,
      libpng.to_s,
      "/private/tmp/",
      "/nix/store/",
      "/home/runner/work/",
    ]
    artifacts = [
      wasm,
      texmf_dist/"web2c/pdftex/pdftex.fmt",
      texmf_dist/"web2c/pdftex/pdflatex.fmt",
      texmf_dist/"web2c/pdftex/latex.fmt",
    ]
    artifacts.each do |artifact|
      contents = artifact.binread
      markers.each do |marker|
        odie "#{artifact.basename} contains builder path #{marker}" if contents.include?(marker)
      end
      odie "#{artifact.basename} contains a builder home path" if contents.match?(%r{/Users/[^/]+/})
      odie "#{artifact.basename} contains a Homebrew Cellar path" if contents.include?("/Cellar/")
    end
  end

  def texlive_test_document
    <<~'TEX'
      \documentclass{article}
      \usepackage{amsmath}
      \usepackage{tikz}
      \begin{document}
      Kandelo pdfTeX generated this document.
      \[
        e^{i\pi} + 1 = 0
      \]
      \begin{tikzpicture}
        \draw (0,0) rectangle (1,1);
      \end{tikzpicture}
      \end{document}
    TEX
  end
end
