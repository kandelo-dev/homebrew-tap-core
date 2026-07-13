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
  PAYLOAD_GROUPS = [:runfiles, :docfiles, :srcfiles].freeze
  INSTALLER_PAYLOAD_FILES = %w[
    tlpkg/gpg/pubring.gpg
    tlpkg/gpg/random_seed
    tlpkg/gpg/secring.gpg
    tlpkg/gpg/tl-key-extension.txt
    tlpkg/gpg/trustdb.gpg
    tlpkg/installer/COPYING.MinGW-runtime.txt
    tlpkg/installer/config.guess
    tlpkg/installer/ctan-mirrors.pl
    tlpkg/installer/curl/curl-ca-bundle.crt
    tlpkg/installer/install-menu-extl.pl
    tlpkg/installer/install-menu-text.pl
    tlpkg/installer/install-tl-gui.tcl
    tlpkg/tltcl/tlmgr.gif
    tlpkg/tltcl/tltcl.tcl
  ].freeze
  DECLARED_ABSENT_PAYLOAD_FILES = %w[
    tlpkg/README
    tlpkg/tltcl/README.TEXLIVE
  ].freeze
  MODIFIED_PAYLOAD_FILES = %w[
    texmf-dist/tex/generic/config/language.dat
    texmf-dist/tex/generic/config/language.dat.lua
    texmf-dist/tex/generic/config/language.def
    texmf-dist/web2c/texmf.cnf
    texmf-dist/web2c/updmap.cfg
  ].freeze
  LANGUAGE_CONFIG_FILES = {
    "language.dat"     => {
      relative_path:    "tex/generic/config/language.dat",
      upstream_sha256:  "97d3da1047e75066623ccdaca8d3b05e0c67986830accf4195a27be463bdff8f",
      upstream_size:    6836,
      generated_sha256: "721cebcac15765e39d371aaedb290631adbdfefc079ef4f0501e4cf9518be6b0",
      generated_size:   2936,
    },
    "language.dat.lua" => {
      relative_path:    "tex/generic/config/language.dat.lua",
      upstream_sha256:  "17e85abeaa35303ec50e4e40f52e60b2d992896b97035a9e275cce557a4e8325",
      upstream_size:    18_186,
      generated_sha256: "ad37e4b207b1d675f12de90102480f62dffaf4c4fd21214c13d0844658020d3d",
      generated_size:   1983,
    },
    "language.def"     => {
      relative_path:    "tex/generic/config/language.def",
      upstream_sha256:  "caaf9bac4fcb8a95a68b58c6a4b863dbebd81f50bdf6b88602d99d14a0a9f1d8",
      upstream_size:    8061,
      generated_sha256: "c9cb1f3162c5b09a0914e797bbf344a0484c24e599bca7288ed4d513e7c47ccd",
      generated_size:   1712,
    },
  }.freeze
  UPDMAP_CONFIG = {
    relative_path:    "web2c/updmap.cfg",
    upstream_sha256:  "ec163069b8dd1009bfdf308db9b75364f94e2ab2f8258c638618e08184fd8354",
    upstream_size:    10_354,
    generated_sha256: "be79a77db89fc44a33578f83f04b0ccb14be7d246fa8308789f147c383ae62e9",
    generated_size:   4462,
  }.freeze
  GENERATED_FONT_MAP_FILES = {
    "pdftex.map"       => {
      relative_path: "fonts/map/pdftex/updmap/pdftex.map",
      sha256:        "e56abed67fcc8c57f1f978b9cdb107e7f79ca69365795cf4f0e3ed8feb097e5d",
      size:          325_360,
    },
    "pdftex_dl14.map"  => {
      relative_path: "fonts/map/pdftex/updmap/pdftex_dl14.map",
      sha256:        "27c3b4f7e071f4527428300d8a2a1e35ebdef5b104a6a87fb8c1a4a3d59fd48d",
      size:          325_365,
    },
    "pdftex_ndl14.map" => {
      relative_path: "fonts/map/pdftex/updmap/pdftex_ndl14.map",
      sha256:        "ba31424bef6c9725046174e13451769986e7970d86a740cea39d603c4c209ab5",
      size:          323_700,
    },
  }.freeze
  GENERATED_FORMAT_FILES = %w[
    web2c/pdftex/latex.fmt
    web2c/pdftex/pdflatex.fmt
    web2c/pdftex/pdftex.fmt
  ].freeze
  desc "Typesetting engine and selected TeX Live runtime for Kandelo"
  homepage "https://www.tug.org/texlive/"
  url "https://ftp.math.utah.edu/pub/tex/historic/systems/texlive/2025/texlive-20250308-source.tar.xz"
  mirror "https://ftp.tu-chemnitz.de/pub/tug/historic/systems/texlive/2025/texlive-20250308-source.tar.xz"
  sha256 "fffdb1a3d143c177a4398a2229a40d6a88f18098e5f6dcfd57648c9f2417490f"
  license :cannot_represent

  depends_on "binaryen" => :build
  depends_on "pkgconf" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/libcxx"
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

  resource "texlive-installer" do
    url "https://ftp.math.utah.edu/pub/tex/historic/systems/texlive/2025/install-tl-unx.tar.gz"
    mirror "https://ftp.tu-chemnitz.de/pub/tug/historic/systems/texlive/2025/install-tl-unx.tar.gz"
    version TEXLIVE_SNAPSHOT
    sha256 "9938f192af75f792e84282580cce6eedac32969e0e07b33cb39ca1b699e948b6"
  end

  def install
    kandelo_require_arch!("wasm32")
    root = Pathname(kandelo_require_root!)
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")
    libpng = formula_opt_prefix("automattic/kandelo-homebrew/libpng")
    libcxx = formula_opt_prefix("automattic/kandelo-homebrew/libcxx")
    pkg_config = formula_opt_bin("pkgconf")/"pkg-config"
    build_script = Pathname(__dir__).parent/"Kandelo/formula_support/build-texlive-pdftex.sh"
    config_generator = Pathname(__dir__).parent/"Kandelo/formula_support/generate-texlive-runtime-config.pl"

    tlpdb = buildpath/"texlive.tlpdb"
    resource("texlive-extra").stage do
      cp "tlpkg/texlive.tlpdb", tlpdb
      (pkgshare/"licenses").install "LICENSE.CTAN", "LICENSE.TL"
    end
    install_engine_licenses

    payload_files, runtime_packages = texlive_runtime_contract(tlpdb)
    write_runtime_provenance(runtime_packages)
    write_payload_manifest(runtime_packages, DECLARED_ABSENT_PAYLOAD_FILES, MODIFIED_PAYLOAD_FILES)
    write_modification_notice

    # Retain every available run/doc/src file declared by the selected closure.
    # Non-texmf root files live in a formula-specific namespace so Homebrew does
    # not link generic upstream names such as LICENSE.TL into prefix/share.
    texmf_files, root_files = payload_files.partition { |path| path.start_with?("texmf-dist/") }
    odie "expected 24467 TeX Live texmf paths" if texmf_files.length != 24_467
    odie "expected 89 TeX Live root paths" if root_files.length != 89
    installer_files = root_files & INSTALLER_PAYLOAD_FILES
    absent_files = root_files & DECLARED_ABSENT_PAYLOAD_FILES
    extra_files = root_files - installer_files - absent_files
    odie "expected 73 TeX Live extra paths" if extra_files.length != 73
    odie "expected 14 TeX Live installer paths" if installer_files.length != 14
    odie "unexpected declared-absent TeX Live paths" if absent_files != DECLARED_ABSENT_PAYLOAD_FILES

    texmf_manifest = buildpath/"texlive-texmf-archive-files.txt"
    texmf_manifest.write texmf_files.map { |path| "texlive-#{TEXLIVE_SNAPSHOT}-texmf/#{path}\n" }.join
    extra_manifest = buildpath/"texlive-extra-archive-files.txt"
    extra_manifest.write extra_files.map { |path| "texlive-#{TEXLIVE_SNAPSHOT}-extra/#{path}\n" }.join
    installer_manifest = buildpath/"texlive-installer-archive-files.txt"
    installer_manifest.write installer_files.map { |path| "install-tl-#{TEXLIVE_SNAPSHOT}/#{path}\n" }.join

    share.mkpath
    system kandelo_host_tool("tar"),
      "-xJf", resource("texlive-texmf").cached_download,
      "-C", share,
      "--strip-components=1",
      "--verbatim-files-from",
      "-T", texmf_manifest
    upstream_root = pkgshare/"upstream-root"
    upstream_root.mkpath
    system kandelo_host_tool("tar"),
      "-xJf", resource("texlive-extra").cached_download,
      "-C", upstream_root,
      "--strip-components=1",
      "--verbatim-files-from",
      "-T", extra_manifest
    system kandelo_host_tool("tar"),
      "-xzf", resource("texlive-installer").cached_download,
      "-C", upstream_root,
      "--strip-components=1",
      "--verbatim-files-from",
      "-T", installer_manifest
    verify_payload_files!(payload_files - absent_files)

    texmf_dist = share/"texmf-dist"
    configure_texmf_cnf!(texmf_dist/"web2c/texmf.cnf")

    # Kpathsea's redistributor layout resolves share/texmf-dist from the real
    # guest opt symlink without embedding the build machine's Cellar path.
    configure_texmf_cnf!(buildpath/"texk/kpathsea/texmf.cnf")

    host_build = buildpath/"host-build"
    cross_build = buildpath/"cross-build"
    linked_pdftex = buildpath/"pdftex.wasm"
    jobs = [ENV.make_jobs, 2].min
    host_bash = kandelo_host_tool("bash")
    system host_bash, build_script, "engine",
      buildpath, host_build, cross_build, root, zlib, libpng, libcxx,
      pkg_config, linked_pdftex, GUEST_PREFIX, jobs
    generate_runtime_config!(
      texmf_dist, tlpdb, runtime_packages, upstream_root,
      config_generator, host_build/"texk/kpathsea/kpsewhich"
    )

    fixture = buildpath/"kandelo-texlive-smoke.tex"
    fixture.write texlive_test_document
    format_work = buildpath/"format-work"
    host_smoke = buildpath/"host-smoke"
    system host_bash, build_script, "formats",
      host_build/"texk/web2c/pdftex", texmf_dist, format_work, fixture, host_smoke

    test_files = recorded_texmf_inputs(
      [host_smoke/"input.fls", host_smoke/"latex-input.fls"],
      texmf_dist,
    )
    (pkgshare/"test-files.txt").write "#{test_files.join("\n")}\n"
    write_generated_payload_manifest(texmf_dist)

    # pdfTeX has real system()/popen() paths for shell escape and font
    # generation, so it is a fork-using program even though the smoke keeps
    # shell escape disabled. Apply and verify the current Kandelo continuation
    # ABI rather than shipping an uninstrumented executable.
    kandelo_fork_instrument(linked_pdftex)
    kandelo_validate_wasm_artifact(
      linked_pdftex,
      fork:            :required,
      forbidden_paths: [
        root.realpath.to_s,
        zlib.to_s,
        zlib.realpath.to_s,
        libpng.to_s,
        libpng.realpath.to_s,
        libcxx.to_s,
        libcxx.realpath.to_s,
        "/.cache/kandelo/",
        "/Cellar/",
      ],
    )
    verify_no_unresolved_cxx_imports!(linked_pdftex)
    verify_generated_format_paths!(texmf_dist, zlib, libpng, libcxx, root)

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
    assert_path_exists texmf_dist/"tex/plain/etex/etex.src"
    assert_path_exists texmf_dist/"tex/generic/config/language.dat"
    assert_path_exists texmf_dist/"tex/generic/config/language.dat.lua"
    assert_path_exists texmf_dist/"tex/generic/config/language.def"
    assert_path_exists texmf_dist/"fonts/map/pdftex/updmap/pdftex.map"
    assert_path_exists texmf_dist/"fonts/map/pdftex/updmap/pdftex_dl14.map"
    assert_path_exists texmf_dist/"fonts/map/pdftex/updmap/pdftex_ndl14.map"
    assert_path_exists texmf_dist/"doc/fonts/amsfonts/OFL.txt"
    assert_path_exists texmf_dist/"doc/fonts/amsfonts/OFL-FAQ.txt"
    assert_path_exists pkgshare/"upstream-root/LICENSE.TL"
    assert_path_exists pkgshare/"selected-files.txt"
    assert_path_exists pkgshare/"generated-files.txt"
    selected_files = (pkgshare/"selected-files.txt").read
    assert_includes selected_files, "retained\ttlpkg/installer/COPYING.MinGW-runtime.txt\t"
    assert_includes selected_files, "retained-modified\ttexmf-dist/tex/generic/config/language.dat\t"
    assert_includes selected_files, "retained-modified\ttexmf-dist/tex/generic/config/language.dat.lua\t"
    assert_includes selected_files, "retained-modified\ttexmf-dist/tex/generic/config/language.def\t"
    assert_includes selected_files, "retained-modified\ttexmf-dist/web2c/texmf.cnf\t"
    assert_includes selected_files, "retained-modified\ttexmf-dist/web2c/updmap.cfg\t"
    assert_includes selected_files, "upstream-declared-absent\ttlpkg/README\t"
    assert_includes selected_files, "upstream-declared-absent\ttlpkg/tltcl/README.TEXLIVE\t"
    generated_files = (pkgshare/"generated-files.txt").read
    assert_includes generated_files, "generated\ttexmf-dist/fonts/map/pdftex/updmap/pdftex.map\tselected-updmap"
    assert_includes generated_files, "generated\ttexmf-dist/web2c/pdftex/pdflatex.fmt\tformat"

    input = testpath/"input.tex"
    input.write texlive_test_document(user_package: true)
    (testpath/"home").mkpath
    user_package = testpath/"home/texmf/tex/latex/kandelo-user/kandelo-user.sty"
    user_package.dirname.mkpath
    user_package.write <<~'TEX'
      \NeedsTeXFormat{LaTeX2e}
      \ProvidesPackage{kandelo-user}[2025/03/08 Kandelo user tree test]
      \typeout{KANDELO_USER_TREE_OK}
      \endinput
    TEX
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
        "TIMEOUT"    => "120000",
      },
      exec_programs:             { guest_pdftex => bin/"pdflatex" },
      guest_files:               guest_files,
      merge_stderr:              true,
      writable_host_directories: { "/work" => testpath.realpath },
    )
    assert_match(/This is pdfTeX, Version .* \(TeX Live 2025\)/, output)
    refute_match(/pdfTeX warning:/, output)
    assert_match(/KANDELO_USER_TREE_OK/, output)
    assert_match(/Output written on .*input\.pdf \(1 page, [0-9]+ bytes\)/, output)

    pdf = testpath/"input.pdf"
    assert_path_exists pdf
    pdf_bytes = pdf.binread
    assert_operator pdf_bytes.bytesize, :>, 1_000
    assert pdf_bytes.start_with?("%PDF-"), "pdfTeX output has no PDF header"
    pdf_tail = pdf_bytes.byteslice([pdf_bytes.bytesize - 1_024, 0].max, 1_024)
    assert_includes pdf_tail, "%%EOF"

    plain_input = testpath/"plain.tex"
    plain_input.write "Kandelo plain pdfTeX output.\\par\n\\bye\n"
    guest_plain_pdftex = "#{GUEST_PREFIX}/bin/pdftex"
    plain_output = kandelo_run_wasm(
      bin/"pdftex",
      [
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-jobname=plain-input",
        "-output-format=pdf",
        "-output-directory=/work",
        "/work/plain.tex",
      ],
      argv0:                     guest_plain_pdftex,
      env:                       {
        "HOME"       => "/work/home",
        "KERNEL_CWD" => "/work",
        "TIMEOUT"    => "120000",
      },
      exec_programs:             { guest_plain_pdftex => bin/"pdftex" },
      guest_files:               guest_files,
      merge_stderr:              true,
      writable_host_directories: { "/work" => testpath.realpath },
    )
    refute_match(/pdfTeX warning:/, plain_output)
    assert_match(/Output written on .*plain-input\.pdf \(1 page, [0-9]+ bytes\)/, plain_output)
    plain_pdf = (testpath/"plain-input.pdf").binread
    assert_operator plain_pdf.bytesize, :>, 500
    assert plain_pdf.start_with?("%PDF-"), "plain pdfTeX output has no PDF header"

    guest_latex = "#{GUEST_PREFIX}/bin/latex"
    latex_output = kandelo_run_wasm(
      bin/"latex",
      [
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
        "TIMEOUT"    => "120000",
      },
      exec_programs:             { guest_latex => bin/"latex" },
      guest_files:               guest_files,
      merge_stderr:              true,
      writable_host_directories: { "/work" => testpath.realpath },
    )
    refute_match(/pdfTeX warning:/, latex_output)
    assert_match(/Output written on .*latex-input\.dvi \(1 page, [0-9]+ bytes\)/, latex_output)
    dvi = (testpath/"latex-input.dvi").binread
    assert_operator dvi.bytesize, :>, 100
    assert_equal [247, 2], dvi.bytes.first(2), "LaTeX output has no DVI preamble"
    assert_includes dvi.bytes.last(16), 249, "LaTeX output has no DVI post_post opcode"

    browser_input = "/usr/local/share/texlive-test/input.tex"
    browser_input_host = testpath/"browser-input.tex"
    browser_input_host.write texlive_test_document
    browser_guest_files = guest_files.merge(browser_input => browser_input_host)
    browser_output = kandelo_run_browser_wasm(
      bin/"pdflatex",
      [
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-output-format=pdf",
        "-output-directory=/tmp",
        browser_input,
      ],
      argv0:       "pdflatex",
      env:         {
        "HOME"      => "/tmp",
        "TEXMF"     => GUEST_TEXMF,
        "TEXMFCNF"  => "#{GUEST_TEXMF}/web2c",
        "TEXMFDIST" => GUEST_TEXMF,
        "TEXMFVAR"  => "/tmp",
      },
      guest_files: browser_guest_files,
      timeout_ms:  120_000,
    )
    refute_match(/pdfTeX warning:/, browser_output)
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
      catalogue_license_line = lines.find { |line| line.start_with?("catalogue-license ") }
      catalogue_license = catalogue_license_line&.delete_prefix("catalogue-license ")
      dependencies = lines.grep(/^depend /).map { |line| line.delete_prefix("depend ") }
      file_groups = PAYLOAD_GROUPS.to_h do |group|
        files = []
        if (group_index = lines.index { |line| line.start_with?("#{group} ") })
          lines[(group_index + 1)..].each do |line|
            break unless line.start_with?(" ")

            files << line.strip.split(" details=", 2).first
          end
        end
        [group, files]
      end
      packages[name] = {
        revision:          revision,
        catalogue_license: catalogue_license || "<missing>",
        dependencies:      dependencies,
        **file_groups,
      }
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

    expected_counts = {
      runfiles: 16_395,
      docfiles: 6_705,
      srcfiles: 1_456,
    }
    expected_counts.each do |group, expected|
      actual = selected.values.flat_map { |package| package.fetch(group) }.uniq.length
      odie "expected #{expected} TeX Live #{group}, found #{actual}" if actual != expected
    end
    payload_files = selected.values.flat_map do |package|
      PAYLOAD_GROUPS.flat_map { |group| package.fetch(group) }
    end.uniq.sort
    payload_files.each do |path|
      unsafe = path.start_with?("/", "-") ||
               Pathname(path).cleanpath.to_s != path ||
               path.match?(/[\0\t\r\n]/)
      odie "unsafe TeX Live payload path: #{path}" if unsafe
    end
    odie "expected 24556 TeX Live payload files, found #{payload_files.length}" if payload_files.length != 24_556

    [payload_files, selected]
  end

  def write_runtime_provenance(packages)
    package_lines = packages.sort.map do |name, package|
      counts = PAYLOAD_GROUPS.map { |group| "#{group}=#{package.fetch(group).length}" }
      license = package.fetch(:catalogue_license)
      "  #{name}@#{package.fetch(:revision)} catalogue-license=#{license} #{counts.join(" ")}"
    end
    lines = [
      "TeX Live snapshot: #{TEXLIVE_SNAPSHOT}",
      "source sha256: fffdb1a3d143c177a4398a2229a40d6a88f18098e5f6dcfd57648c9f2417490f",
      "texmf sha256: 08dcda7430bf0d2f6ebb326f1e197e1473d3f7cc0984a2adb7236df45316c7cf",
      "extra sha256: ea69cfecbc9b138acbc45476e8cb4d9357f5e4e45fd12b3bf9ceabbebd7669d2",
      "installer sha256: 9938f192af75f792e84282580cce6eedac32969e0e07b33cb39ca1b699e948b6",
      "upstream language.dat sha256: 97d3da1047e75066623ccdaca8d3b05e0c67986830accf4195a27be463bdff8f",
      "generated language.dat sha256: 721cebcac15765e39d371aaedb290631adbdfefc079ef4f0501e4cf9518be6b0",
      "upstream language.dat.lua sha256: 17e85abeaa35303ec50e4e40f52e60b2d992896b97035a9e275cce557a4e8325",
      "generated language.dat.lua sha256: ad37e4b207b1d675f12de90102480f62dffaf4c4fd21214c13d0844658020d3d",
      "upstream language.def sha256: caaf9bac4fcb8a95a68b58c6a4b863dbebd81f50bdf6b88602d99d14a0a9f1d8",
      "generated language.def sha256: c9cb1f3162c5b09a0914e797bbf344a0484c24e599bca7288ed4d513e7c47ccd",
      "upstream updmap.cfg sha256: ec163069b8dd1009bfdf308db9b75364f94e2ab2f8258c638618e08184fd8354",
      "generated updmap.cfg sha256: be79a77db89fc44a33578f83f04b0ccb14be7d246fa8308789f147c383ae62e9",
      "selected font-map entries: 52",
      "generated pdftex.map sha256: e56abed67fcc8c57f1f978b9cdb107e7f79ca69365795cf4f0e3ed8feb097e5d",
      "generated pdftex_dl14.map sha256: 27c3b4f7e071f4527428300d8a2a1e35ebdef5b104a6a87fb8c1a4a3d59fd48d",
      "generated pdftex_ndl14.map sha256: ba31424bef6c9725046174e13451769986e7970d86a740cea39d603c4c209ab5",
      "generated texmf runtime files: 6",
      "selected package count: 784",
      "selected unique runfiles: 16395",
      "selected unique docfiles: 6705",
      "selected unique srcfiles: 1456",
      "selected unique declared files: 24556",
      "retained unique payload files: 24554",
      "retained unmodified payload files: 24549",
      "retained modified payload files: 5",
      *MODIFIED_PAYLOAD_FILES.map { |path| "  #{path}" },
      "upstream-declared absent files: 2",
      *DECLARED_ABSENT_PAYLOAD_FILES.map { |path| "  #{path}" },
      "collection roots:",
      *RUNTIME_COLLECTIONS.map { |name, revision| "  #{name}@#{revision}" },
      "resolved packages:",
      *package_lines,
    ]
    (pkgshare/"runtime-packages.txt").write "#{lines.join("\n")}\n"
  end

  def write_payload_manifest(packages, absent_files, modified_files)
    owners = Hash.new { |hash, path| hash[path] = [] }
    packages.each do |name, package|
      revision = package.fetch(:revision)
      PAYLOAD_GROUPS.each do |group|
        package.fetch(group).each do |path|
          owners[path] << "#{group}:#{name}@#{revision}"
        end
      end
    end
    odie "payload manifest does not contain 24556 paths" if owners.length != 24_556
    odie "modified payload set is not declared" unless (modified_files - owners.keys).empty?
    odie "modified and absent payload sets overlap" if modified_files.intersect?(absent_files)

    lines = [
      "TeX Live snapshot: #{TEXLIVE_SNAPSHOT}",
      "unique declared files: #{owners.length}",
      "retained files: #{owners.length - absent_files.length}",
      "retained unmodified files: #{owners.length - absent_files.length - modified_files.length}",
      "retained modified files: #{modified_files.length}",
      "upstream-declared absent files: #{absent_files.length}",
      "format: status<TAB>upstream-path<TAB>group:package@revision[,group:package@revision...]",
      *owners.sort.map do |path, claims|
        status = if absent_files.include?(path)
          "upstream-declared-absent"
        elsif modified_files.include?(path)
          "retained-modified"
        else
          "retained"
        end
        "#{status}\t#{path}\t#{claims.uniq.sort.join(",")}"
      end,
    ]
    (pkgshare/"selected-files.txt").write "#{lines.join("\n")}\n"
  end

  def write_generated_payload_manifest(texmf_dist)
    entries = GENERATED_FONT_MAP_FILES.values.map do |contract|
      [contract.fetch(:relative_path), "selected-updmap"]
    end
    entries.concat(GENERATED_FORMAT_FILES.map { |path| [path, "format"] })
    odie "expected six generated TeX Live runtime files" if entries.length != 6

    lines = [
      "TeX Live snapshot: #{TEXLIVE_SNAPSHOT}",
      "generated texmf runtime files: #{entries.length}",
      "format: status<TAB>runtime-path<TAB>purpose<TAB>size<TAB>sha256",
    ]
    entries.sort.each do |relative, purpose|
      generated = texmf_dist/relative
      odie "generated TeX Live runtime file is missing: #{relative}" unless generated.file?
      odie "generated TeX Live runtime file is a symlink: #{relative}" if generated.symlink?
      lines << "generated\ttexmf-dist/#{relative}\t#{purpose}\t#{generated.size}\t#{generated.sha256}"
    end
    (pkgshare/"generated-files.txt").write "#{lines.join("\n")}\n"
  end

  def write_modification_notice
    (pkgshare/"README.Kandelo").write <<~NOTICE
      Kandelo TeX Live redistribution

      This is a modified redistribution of the TeX Live 20250308 snapshot.
      The Kandelo formula:

      * resolves the 784-package dependency closure rooted at the six
        collections recorded in runtime-packages.txt;
      * resolves 16,395 runfiles, 6,705 docfiles, and 1,456 srcfiles declared by
        that closure, for 24,556 unique upstream paths, without a filename or
        engine-specific allowlist;
      * retains all 24,554 paths available across the pinned texmf, extra, and
        install-tl-unx snapshot artifacts; the tlpdb-declared tlpkg/README and
        tlpkg/tltcl/README.TEXLIVE paths are absent from those upstream artifacts
        and remain explicitly marked upstream-declared-absent;
      * preserves texmf-dist paths in share/texmf-dist and preserves the 87
        retained non-texmf root paths under upstream-root/ to avoid linking
        generic upstream filenames into Homebrew's shared prefix;
      * records every declared path, retention status, and package owner in
        selected-files.txt, including package documentation, source, license,
        and copyright notices;
      * marks texmf.cnf, updmap.cfg, language.dat, language.dat.lua, and
        language.def as retained-modified in the ledger, adjusts texmf.cnf for
        Kandelo guest self-location and non-indexed selected-tree search, and
        regenerates mutually consistent language and font-map configuration
        with TeX Live's pinned tools from the exact selected package closure;
      * generates pdftex.map, pdftex_dl14.map, and pdftex_ndl14.map with the
        retained updmap.pl, and generates pdftex, pdflatex, and latex format
        files without permitting the host smoke to mutate texmf-dist;
      * records those six generated runtime files, sizes, and hashes in
        generated-files.txt;
      * cross-compiles pdfTeX for Kandelo's wasm32 POSIX target; and
      * applies Kandelo fork-continuation instrumentation to the engine.

      LICENSE.TL and LICENSE.CTAN are installed under licenses/. Engine component
      notices and texts are installed there as pdftex-README.txt,
      pdftex-GPL-2.0.txt, pdftex-regex-LGPL-2.1.txt, kpathsea-LGPL-2.1.txt,
      xpdf-README.txt, xpdf-GPL-2.0.txt, and xpdf-GPL-3.0.txt. The pinned Xpdf
      README permits derivatives under GPLv2-only, GPLv3-only, or GPLv2-or-v3,
      so both GPL texts are retained.

      Individual package and file terms remain controlling. runtime-packages.txt
      preserves pinned package revisions, catalogue-license metadata, and file
      counts; selected-files.txt maps every declared path, including explicit
      upstream absences, to its package owner; and generated-files.txt records
      formula-generated runtime state separately from upstream-declared files.
      These ledgers provide traceability and do not replace review of the
      controlling terms for a particular redistributed file.
    NOTICE
  end

  def install_engine_licenses
    license_dir = pkgshare/"licenses"
    license_dir.install buildpath/"texk/web2c/pdftexdir/README" => "pdftex-README.txt"
    license_dir.install buildpath/"texk/web2c/pdftexdir/COPYINGv2" => "pdftex-GPL-2.0.txt"
    license_dir.install buildpath/"texk/web2c/pdftexdir/regex/COPYING.LIB" => "pdftex-regex-LGPL-2.1.txt"
    license_dir.install buildpath/"texk/kpathsea/COPYING.LESSERv2" => "kpathsea-LGPL-2.1.txt"
    license_dir.install buildpath/"libs/xpdf/xpdf-src/README" => "xpdf-README.txt"
    license_dir.install buildpath/"libs/xpdf/xpdf-src/COPYING" => "xpdf-GPL-2.0.txt"
    license_dir.install buildpath/"libs/xpdf/xpdf-src/COPYING3" => "xpdf-GPL-3.0.txt"
  end

  def verify_payload_files!(payload_files)
    payload_files.each do |relative|
      installed = if relative.start_with?("texmf-dist/")
        share/relative
      else
        pkgshare/"upstream-root"/relative
      end
      odie "TeX Live resource omitted #{relative}" unless installed.file?
      odie "TeX Live payload path is a symlink: #{relative}" if installed.symlink?
    end
    amsfonts_doc = share/"texmf-dist/doc/fonts/amsfonts"
    odie "AMS Fonts OFL.txt is missing" unless (amsfonts_doc/"OFL.txt").file?
    odie "AMS Fonts OFL-FAQ.txt is missing" unless (amsfonts_doc/"OFL-FAQ.txt").file?
  end

  def configure_texmf_cnf!(texmf_cnf)
    contents = texmf_cnf.read
    odie "unexpected TeX Live TEXMFROOT contract" unless contents.match?(/^TEXMFROOT = \$SELFAUTOPARENT$/)
    odie "unexpected TeX Live TEXMFDIST contract" unless contents.include?("!!$TEXMFDIST")

    inreplace texmf_cnf, /^TEXMFROOT = .*$/, "TEXMFROOT = $SELFAUTODIR/share"
    inreplace texmf_cnf, "!!$TEXMFDIST", "$TEXMFDIST"
  end

  def generate_runtime_config!(texmf_dist, tlpdb, runtime_packages, upstream_root, generator_script, kpsewhich)
    config_contracts = [*LANGUAGE_CONFIG_FILES.values, UPDMAP_CONFIG]
    config_contracts.each do |contract|
      upstream = texmf_dist/contract.fetch(:relative_path)
      odie "upstream #{upstream.basename} is missing" unless upstream.file?
      odie "upstream #{upstream.basename} is a symlink" if upstream.symlink?
      odie "unexpected upstream #{upstream.basename} size" if upstream.size != contract.fetch(:upstream_size)
      odie "unexpected upstream #{upstream.basename}" if upstream.sha256 != contract.fetch(:upstream_sha256)
    end

    generator_root = buildpath/"runtime-config-generator-root"
    (generator_root/"tlpkg").mkpath
    cp tlpdb, generator_root/"tlpkg/texlive.tlpdb"
    ln_s texmf_dist, generator_root/"texmf-dist"
    selected_packages = buildpath/"texlive-selected-packages.txt"
    selected_packages.write "#{runtime_packages.keys.sort.join("\n")}\n"
    output_dir = buildpath/"generated-runtime-config"
    module_root = upstream_root/"tlpkg"
    odie "pinned TeX Live runtime-config generator is missing" unless generator_script.file?
    odie "pinned TeX Live Perl modules are missing" unless (module_root/"TeXLive/TLPDB.pm").file?
    ln_s module_root/"TeXLive", generator_root/"tlpkg/TeXLive"
    system kandelo_host_tool("perl"), "-I#{module_root}", generator_script,
      generator_root, selected_packages, output_dir, TEXLIVE_SNAPSHOT, kpsewhich

    LANGUAGE_CONFIG_FILES.each do |filename, contract|
      generated = output_dir/filename
      odie "generated #{filename} is missing" unless generated.file?
      odie "generated #{filename} is a symlink" if generated.symlink?
      odie "unexpected generated #{filename} size" if generated.size != contract.fetch(:generated_size)
      odie "unexpected generated #{filename}" if generated.sha256 != contract.fetch(:generated_sha256)

      destination = texmf_dist/contract.fetch(:relative_path)
      destination.atomic_write generated.binread
    end

    generated_updmap = output_dir/"updmap.cfg"
    odie "generated updmap.cfg is missing" unless generated_updmap.file?
    odie "generated updmap.cfg is a symlink" if generated_updmap.symlink?
    odie "unexpected generated updmap.cfg size" if generated_updmap.size != UPDMAP_CONFIG.fetch(:generated_size)
    odie "unexpected generated updmap.cfg" if generated_updmap.sha256 != UPDMAP_CONFIG.fetch(:generated_sha256)
    (texmf_dist/UPDMAP_CONFIG.fetch(:relative_path)).atomic_write generated_updmap.binread

    generated_map_dir = output_dir/"runtime/texmf-var/fonts/map/pdftex/updmap"
    GENERATED_FONT_MAP_FILES.each do |filename, contract|
      generated = generated_map_dir/filename
      odie "generated #{filename} is missing" unless generated.file?
      odie "generated #{filename} is a symlink" if generated.symlink?
      odie "unexpected generated #{filename} size" if generated.size != contract.fetch(:size)
      odie "unexpected generated #{filename}" if generated.sha256 != contract.fetch(:sha256)

      destination = texmf_dist/contract.fetch(:relative_path)
      odie "generated map destination already exists: #{destination}" if destination.exist?
      destination.dirname.mkpath
      destination.atomic_write generated.binread
    end
  end

  def recorded_texmf_inputs(fls_files, texmf_dist)
    prefix = "#{texmf_dist}/"
    inputs = fls_files.flat_map do |fls|
      fls.readlines.filter_map do |line|
        next unless line.start_with?("INPUT ")

        path = line.delete_prefix("INPUT ").strip
        next unless path.start_with?(prefix)

        path.delete_prefix(prefix)
      end
    end
    inputs |= %w[
      web2c/texmf.cnf
      web2c/pdftex/pdftex.fmt
      web2c/pdftex/pdflatex.fmt
      web2c/pdftex/latex.fmt
    ]
    inputs.sort!
    odie "host pdfTeX smoke recorded too few runtime inputs" if inputs.length <= 20
    odie "host pdfTeX smoke did not load pdftex.map" unless inputs.include?("fonts/map/pdftex/updmap/pdftex.map")
    odie "host pdfTeX smoke did not load an outline font" unless inputs.any? { |path| path.end_with?(".pfb") }
    odie "host pdfTeX smoke fell back to a bitmap font" if inputs.any? { |path| path.start_with?("fonts/pk/") }
    inputs
  end

  def verify_no_unresolved_cxx_imports!(wasm)
    imports = Utils.safe_popen_read("wasm-objdump", "-x", wasm.to_s)
    unresolved_cxx = imports.each_line.select do |line|
      line.include?(" <- ") && line.match?(/(?:<|\.)(?:_Z|__cxa_|__gxx_|_Unwind_)/)
    end
    return if unresolved_cxx.empty?

    odie "pdfTeX retains unresolved C++ runtime imports:\n#{unresolved_cxx.join}"
  end

  def verify_generated_format_paths!(texmf_dist, zlib, libpng, libcxx, root)
    markers = [
      prefix.to_s,
      buildpath.to_s,
      root.to_s,
      root.realpath.to_s,
      zlib.to_s,
      zlib.realpath.to_s,
      libpng.to_s,
      libpng.realpath.to_s,
      libcxx.to_s,
      libcxx.realpath.to_s,
      "/.cache/kandelo/",
      "/private/tmp/",
      "/nix/store/",
      "/home/runner/work/",
    ]
    formats = [
      texmf_dist/"web2c/pdftex/pdftex.fmt",
      texmf_dist/"web2c/pdftex/pdflatex.fmt",
      texmf_dist/"web2c/pdftex/latex.fmt",
    ]
    formats.each do |format|
      contents = format.binread
      markers.each do |marker|
        odie "#{format.basename} contains builder path #{marker}" if contents.include?(marker)
      end
      odie "#{format.basename} contains a builder home path" if contents.match?(%r{/Users/[^/]+/})
      odie "#{format.basename} contains a Homebrew Cellar path" if contents.include?("/Cellar/")
    end
  end

  def texlive_test_document(user_package: false)
    document = <<~'TEX'
      \documentclass{article}
      \usepackage{amsmath}
      \usepackage{tikz}
    TEX
    document << "\\usepackage{kandelo-user}\n" if user_package
    document << <<~'TEX'
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
