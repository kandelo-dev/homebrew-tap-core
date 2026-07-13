require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Nethack < Formula
  include KandeloFormulaSupport

  desc "Single-player roguelike video game for Kandelo"
  homepage "https://www.nethack.org/"
  url "https://www.nethack.org/download/3.6.7/nethack-367-src.tgz"
  version "3.6.7"
  sha256 "98cf67df6debf9668a61745aa84c09bcab362e5d33f5b944ec5155d44d2aacb2"
  license "NGPL"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/ncurses"

  skip_clean "bin/nethack"

  SOURCE_DATE_EPOCH = "1676246400".freeze
  GUEST_OPT_PREFIX = "/home/linuxbrew/.linuxbrew/opt/nethack".freeze
  GUEST_STATE_DIR = "/home/linuxbrew/.linuxbrew/var/nethack".freeze
  STATE_FILES = %w[perm record logfile xlogfile].freeze

  def install
    kandelo_require_arch!("wasm32")
    ncurses = formula_opt_prefix("automattic/kandelo-homebrew/ncurses")

    cd "sys/unix" do
      system "sh", "setup.sh", "hints/linux"
    end

    configure_port(ncurses)
    build_host_data

    kandelo_wasm_build do |root|
      system "make", "-C", "src", "clean"
      system "make", "-C", "src",
        "CC=#{kandelo_cc(root)}",
        "LINK=#{kandelo_cc(root)}",
        "AR=#{kandelo_ar(root)}",
        "RANLIB=#{kandelo_ranlib(root)}",
        "nethack"
    end

    kandelo_fork_instrument buildpath/"src/nethack"
    kandelo_validate_wasm_artifact(buildpath/"src/nethack", fork: :required)
    kandelo_install_bin(buildpath/"src", "nethack", "nethack")

    data_dir = share/"nethack"
    data_dir.install "dat/nhdat"
    data_dir.install "dat/symbols" if (buildpath/"dat/symbols").exist?
    data_dir.install "dat/license"
    man6.install "doc/nethack.6"
  end

  def post_install
    state_dir = var/"nethack"
    (state_dir/"save").mkpath
    STATE_FILES.each do |name|
      state_file = state_dir/name
      state_file.write "" unless state_file.exist?
    end
  end

  def configure_port(ncurses)
    inreplace "include/config.h" do |s|
      s.gsub!(%r{^/\*\s*#define CURSES_GRAPHICS\s*\*/}, "#define CURSES_GRAPHICS")
      s.gsub!(/^#define COMPRESS .*$/, "/* COMPRESS disabled for Kandelo */")
      s.gsub!(/^#define COMPRESS_EXTENSION .*$/, "/* COMPRESS_EXTENSION disabled for Kandelo */")
      s.gsub!(/^#define SYSCF(?:\s.*)?$/, "/* SYSCF disabled for Kandelo */")
      s.gsub!(/^#define SYSCF_FILE .*$/, "/* SYSCF_FILE disabled for Kandelo */")
      s.sub!("/* #define REPRODUCIBLE_BUILD */", "#define REPRODUCIBLE_BUILD")
    end

    inreplace "include/system.h",
      "E void FDECL(tputs, (const char *, int, int (*)()));",
      "E int FDECL(tputs, (const char *, int, int (*)(int)));"

    indent_c = lambda do |body|
      body.lines.map { |line| line.start_with?("#") ? line : "    #{line}" }.join
    end
    before_backspace = indent_c.call(<<~C)
      if (!(BC = Tgetstr("le"))) /* both termcap and terminfo use le */
      #ifdef TERMINFO
          error("Terminal must backspace.");
      #else
          if (!(BC = Tgetstr("bc"))) { /* termcap also uses bc/bs */
      #ifndef MINIMAL_TERM
              if (!tgetflag("bs"))
                  error("Terminal must backspace.");
      #endif
              BC = tbufptr;
              tbufptr += 2;
              *BC = '\\b';
          }
      #endif
    C
    after_backspace = indent_c.call(<<~C)
      if (!(BC = Tgetstr("le"))) { /* both termcap and terminfo use le */
      #ifdef TERMINFO
          BC = tbufptr;
          tbufptr += 2;
          *BC = '\\b';
      #else
          if (!(BC = Tgetstr("bc"))) { /* termcap also uses bc/bs */
      #ifndef MINIMAL_TERM
              if (!tgetflag("bs"))
                  error("Terminal must backspace.");
      #endif
              BC = tbufptr;
              tbufptr += 2;
              *BC = '\\b';
          }
      #endif
      }
    C
    inreplace "win/tty/termcap.c", before_backspace, after_backspace

    inreplace "include/qtext.h" do |s|
      s.sub!("#define LEN_HDR 3 /* Maximum length of a category name */",
        "#define LEN_HDR 3 /* Maximum length of a category name */\n\n" \
        "#include <stdint.h>\ntypedef int32_t qt_offset_t;")
      s.gsub!("long offset, size, summary_size;", "qt_offset_t offset, size, summary_size;")
      s.gsub!("long offset[N_HDR];", "qt_offset_t offset[N_HDR];")
    end

    inreplace "src/questpgr.c" do |s|
      s.gsub!("construct_qtlist, (long)", "construct_qtlist, (qt_offset_t)")
      s.gsub!("long hdr_offset;", "qt_offset_t hdr_offset;")
      s.gsub!("long qt_offsets[N_HDR];", "qt_offset_t qt_offsets[N_HDR];")
      s.gsub!("Fread(qt_offsets, sizeof (long), n_classes, msg_file);",
        "Fread(qt_offsets, sizeof qt_offsets[0], n_classes, msg_file);")
    end

    inreplace "util/makedefs.c" do |s|
      s.gsub!("sizeof(char) * LEN_HDR + sizeof(long)",
        "sizeof(char) * LEN_HDR + sizeof qt_hdr.offset[0]")
      s.gsub!("sizeof(long),\n                  qt_hdr.n_hdr, ofp);",
        "sizeof qt_hdr.offset[0],\n                  qt_hdr.n_hdr, ofp);")
      s.gsub!('Fprintf(stderr, "%s @ %ld, ", qt_hdr.id[i], qt_hdr.offset[i]);',
        'Fprintf(stderr, "%s @ %ld, ", qt_hdr.id[i], (long) qt_hdr.offset[i]);')
    end

    global_h = buildpath/"include/global.h"
    global_source = global_h.read
    global_source.sub!("#include <stdio.h>",
      "#include <stdio.h>\n#include <stdint.h>\n\n#define PORT_ID \"Kandelo\"")
    %w[version_info savefile_info].each do |name|
      definition = global_source[/struct #{name} \{.*?^\};/m]
      odie "missing NetHack #{name} serialization structure" if definition.nil?

      global_source.gsub!(definition, definition.gsub("unsigned long", "uint32_t"))
    end
    File.write(global_h, global_source)

    inreplace "util/makedefs.c" do |s|
      %w[incarnation feature_set entity_count struct_sizes1 struct_sizes2].each do |field|
        s.gsub!("version.#{field},", "(unsigned long) version.#{field},")
      end
      s.gsub!("                        msg_hdr[i].qt_msg[j].offset,",
        "                        (long) msg_hdr[i].qt_msg[j].offset,")
      s.gsub!("                        msg_hdr[i].qt_msg[j].size);",
        "                        (long) msg_hdr[i].qt_msg[j].size);")
      s.gsub!("                            msg_hdr[i].qt_msg[j].summary_size);",
        "                            (long) msg_hdr[i].qt_msg[j].summary_size);")
    end

    inreplace "include/tradstdc.h" do |s|
      s.gsub!(/^#define __warn_unused_result__.*$/,
        "/* empty definition suppressed for host SDK compatibility */")
      s.gsub!(/^#define warn_unused_result.*$/,
        "/* empty definition suppressed for host SDK compatibility */")
    end

    inreplace "src/Makefile" do |s|
      s.gsub!(/^PREFIX=.*$/, "PREFIX=#{GUEST_OPT_PREFIX}")
      s.gsub!(/^HACKDIR=.*$/, "HACKDIR=#{GUEST_OPT_PREFIX}/share/nethack")
      s.gsub!(/^VARDIR\s*=.*$/, "VARDIR=#{GUEST_STATE_DIR}")
      s.gsub!(/^CFLAGS=-g -O /, "CFLAGS=-O2 ")
      s.gsub!(/^CFLAGS\+=-DCOMPRESS=.*$/, "# external compression disabled for Kandelo")
      s.gsub!(/^CFLAGS\+=-DSYSCF .*$/, "# multi-user sysconf disabled for Kandelo")
      s.gsub!(/^CFLAGS\+=-DCONFIG_ERROR_SECURE=.*$/, "# secure sysconf errors disabled for Kandelo")
      s.gsub!(/^WINTTYLIB\s*=.*$/, "WINTTYLIB=-lncursesw -ltinfow")
      s.gsub!(/^WINCURSESLIB\s*=.*$/, "WINCURSESLIB=-lncursesw -ltinfow")
      s.sub!(/^CFLAGS\+=-DCURSES_GRAPHICS$/,
        "CFLAGS+=-DCURSES_GRAPHICS\n" \
        "CFLAGS+=-DVAR_PLAYGROUND=\\\"#{GUEST_STATE_DIR}\\\"\n" \
        "CFLAGS+=-I#{ncurses}/include/ncursesw -I#{ncurses}/include\n" \
        "LFLAGS+=-L#{ncurses}/lib")
      s.gsub!(/^LFLAGS=-rdynamic$/, "LFLAGS+=-rdynamic -L#{ncurses}/lib")
    end
  end

  def build_host_data
    host_env = kandelo_host_tool("env")
    host_make = [host_env.to_s, "SOURCE_DATE_EPOCH=#{SOURCE_DATE_EPOCH}", "make"]

    system(*host_make, "CC=cc", "LD=cc", "-C", "util",
      "makedefs", "dgn_comp", "lev_comp", "dlb", "recover")
    system(*host_make, "CC=cc", "LD=cc", "-C", "dat", "all")
    system(*host_make, "CC=cc", "LD=cc", "dlb")
    odie "NetHack data archive was not generated" unless (buildpath/"dat/nhdat").exist?

    future = Time.utc(2040, 1, 1, 1, 1)
    %w[
      util/makedefs util/dgn_comp util/lev_comp util/dlb util/recover
      include/onames.h include/pm.h include/date.h include/vis_tab.h dat/nhdat
    ].each do |path|
      artifact = buildpath/path
      File.utime(future, future, artifact) if artifact.exist?
    end
  end

  test do
    state_seed = testpath/"nethack-state"
    (state_seed/"save").mkpath
    assert_path_exists var/"nethack/save"
    STATE_FILES.each do |name|
      assert_path_exists var/"nethack"/name
      (state_seed/name).write ""
    end

    data_files = %w[nhdat symbols license].to_h do |name|
      ["#{GUEST_OPT_PREFIX}/share/nethack/#{name}", (share/"nethack"/name).to_s]
    end
    STATE_FILES.each do |name|
      data_files["#{GUEST_STATE_DIR}/#{name}"] = (state_seed/name).to_s
    end
    state_directories = ["#{GUEST_STATE_DIR}/save"]

    binary = File.binread(bin/"nethack")
    assert_includes binary, "#{GUEST_OPT_PREFIX}/share/nethack"
    assert_includes binary, GUEST_STATE_DIR
    refute_includes binary, prefix.to_s
    refute_includes binary, buildpath.to_s
    %w[/private/tmp/ /nix/store/ /Users/].each do |builder_path|
      refute_includes binary, builder_path
    end

    version_output = kandelo_run_wasm(
      bin/"nethack", ["--version"],
      guest_files: data_files
    )
    assert_equal "Kandelo NetHack Version 3.6.7 - last revision Mon Feb 13 00:00:00 2023.\n\n",
      version_output

    paths_output = kandelo_run_wasm(
      bin/"nethack", ["--showpaths"],
      guest_files: data_files
    )
    assert_includes paths_output, %Q("#{GUEST_OPT_PREFIX}/share/nethack/symbols")
    assert_includes paths_output, "collected inside:\n    \"nhdat\""
    assert_includes paths_output, %Q([scoredir  ]="#{GUEST_STATE_DIR}/")
    refute_includes paths_output, prefix.to_s

    score_output = kandelo_run_wasm(
      bin/"nethack", ["-s"],
      guest_files: data_files
    )
    assert_includes score_output, "Cannot find any current entries for you."

    transcript = kandelo_run_pty_wasm(
      bin/"nethack",
      ["-u", "KandeloTest-Wiz-Hum-Mal-Neu"],
      env:               {
        "HOME"       => "/home/user",
        "KERNEL_CWD" => "/tmp",
        "TERM"       => "xterm",
        "TIMEOUT"    => "30000",
      },
      inputs:            [" ", "S", "y"],
      rerun_inputs:      [" ", "S", "y"],
      guest_files:       data_files,
      guest_directories: state_directories,
      writable_guest_directories: [GUEST_STATE_DIR],
      initial_delay_ms:  1000,
      input_delay_ms:    400,
    )
    assert_includes transcript, "Welcome to NetHack"
    assert_includes transcript, "You are a neutral male human Wizard."
    assert_includes transcript, "Restoring save file"
    assert_operator transcript.scan("Saving...").length, :>=, 2
    assert_includes transcript, "\e[?1049h"
    assert_includes transcript, "\e[?1049l"
  end
end
