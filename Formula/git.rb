require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Git < Formula
  include KandeloFormulaSupport

  GUEST_HOMEBREW_PREFIX = "/home/linuxbrew/.linuxbrew".freeze
  GUEST_OPT_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/git".freeze
  GUEST_GIT_BIN = "#{GUEST_OPT_PREFIX}/bin".freeze
  GUEST_GIT_EXEC_PATH = "#{GUEST_OPT_PREFIX}/libexec/git-core".freeze
  GUEST_GIT_TEMPLATES = "#{GUEST_OPT_PREFIX}/share/git-core/templates".freeze
  GUEST_COREUTILS_BIN = "#{GUEST_HOMEBREW_PREFIX}/opt/coreutils/bin".freeze
  GUEST_DASH = "#{GUEST_HOMEBREW_PREFIX}/opt/dash/bin/dash".freeze
  GUEST_DIFFUTILS_BIN = "#{GUEST_HOMEBREW_PREFIX}/opt/diffutils/bin".freeze
  GUEST_GREP_BIN = "#{GUEST_HOMEBREW_PREFIX}/opt/grep/bin".freeze
  GUEST_LESS_BIN = "#{GUEST_HOMEBREW_PREFIX}/opt/less/bin".freeze
  GUEST_LIBCURL_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/libcurl".freeze
  GUEST_OPENSSL_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/openssl".freeze
  GUEST_SED_BIN = "#{GUEST_HOMEBREW_PREFIX}/opt/sed/bin".freeze
  GUEST_VIM_BIN = "#{GUEST_HOMEBREW_PREFIX}/opt/vim/bin".freeze
  GUEST_VIM_RUNTIME = "#{GUEST_HOMEBREW_PREFIX}/opt/vim/share/vim/vim92".freeze
  GUEST_ZLIB_PREFIX = "#{GUEST_HOMEBREW_PREFIX}/opt/zlib".freeze
  COREUTILS_COMMANDS = %w[
    basename
    cat
    cp
    cut
    dirname
    env
    expr
    ls
    mkdir
    mktemp
    mv
    rm
    rmdir
    seq
    sort
    touch
    tr
    uname
    wc
  ].freeze
  BUILTIN_ALIASES = %w[
    git-receive-pack
    git-upload-archive
    git-upload-pack
  ].freeze
  LINKED_PROGRAMS = %w[
    git
    git-remote-http
    git-sh-i18n--envsubst
  ].freeze
  SHELL_COMMANDS = %w[
    git-difftool--helper
    git-merge-octopus
    git-merge-one-file
    git-merge-resolve
    git-mergetool
    git-quiltimport
    git-submodule
    git-web--browse
  ].freeze
  SHELL_LIBRARIES = %w[
    git-mergetool--lib
    git-sh-i18n
    git-sh-setup
  ].freeze
  PERL_HOOK_SAMPLES = %w[
    fsmonitor-watchman.sample
    pre-rebase.sample
    prepare-commit-msg.sample
  ].freeze

  desc "Distributed version control system for Kandelo"
  homepage "https://git-scm.com/"
  url "https://www.kernel.org/pub/software/scm/git/git-2.47.1.tar.xz"
  mirror "https://mirrors.edge.kernel.org/pub/software/scm/git/git-2.47.1.tar.xz"
  sha256 "f3d8f9bb23ae392374e91cd9d395970dabc5b9c5ee72f39884613cd84a6ed310"
  license all_of: [
    "GPL-2.0-only",
    "GPL-2.0-or-later",
    "LGPL-2.1-or-later",
    "BSD-3-Clause",
    "MIT",
  ]

  depends_on "binaryen" => :build
  depends_on "pkgconf" => :build
  depends_on "wabt" => :build
  depends_on "automattic/kandelo-homebrew/coreutils"
  depends_on "automattic/kandelo-homebrew/dash"
  depends_on "automattic/kandelo-homebrew/diffutils"
  depends_on "automattic/kandelo-homebrew/grep"
  depends_on "automattic/kandelo-homebrew/less"
  depends_on "automattic/kandelo-homebrew/libcurl"
  depends_on "automattic/kandelo-homebrew/openssl"
  depends_on "automattic/kandelo-homebrew/sed"
  depends_on "automattic/kandelo-homebrew/vim"
  depends_on "automattic/kandelo-homebrew/zlib"

  skip_clean "bin/git",
             "libexec/git-core/git-remote-http",
             "libexec/git-core/git-sh-i18n--envsubst"

  resource "man" do
    url "https://www.kernel.org/pub/software/scm/git/git-manpages-2.47.1.tar.xz"
    sha256 "ffc2005a89b056c0727b667f6beda0068371619762ea4844ad0229091befee13"
  end

  patch :DATA

  def install
    kandelo_require_arch!("wasm32")
    odie "git #{version} manpage resource is version #{resource("man").version}" if version != resource("man").version

    libcurl = formula_opt_prefix("automattic/kandelo-homebrew/libcurl")
    openssl = formula_opt_prefix("automattic/kandelo-homebrew/openssl")
    zlib = formula_opt_prefix("automattic/kandelo-homebrew/zlib")

    kandelo_wasm_build do |root|
      ENV["PKG_CONFIG_LIBDIR"] = [
        libcurl/"lib/pkgconfig",
        openssl/"lib/pkgconfig",
        zlib/"lib/pkgconfig",
      ].join(":")
      ENV.delete("PKG_CONFIG_PATH")
      ENV.delete("PKG_CONFIG_SYSROOT_DIR")

      pkgconf = formula_opt_bin("pkgconf")/"pkg-config"
      curl_version = Utils.safe_popen_read(pkgconf, "--modversion", "libcurl").strip
      odie "git #{version} requires the tap libcurl, found #{curl_version}" if curl_version.empty?
      curl_flags = Utils.safe_popen_read(pkgconf, "--static", "--libs", "libcurl").split
      path_maps = {
        buildpath.to_s => "/usr/src/git-#{version}",
        root.to_s      => "/usr/src/kandelo",
        libcurl.to_s   => GUEST_LIBCURL_PREFIX,
        openssl.to_s   => GUEST_OPENSSL_PREFIX,
        zlib.to_s      => GUEST_ZLIB_PREFIX,
        "/nix/store"   => "/usr/src/toolchain",
      }
      prefix_map_flags = path_maps.flat_map do |source, destination|
        [
          "-ffile-prefix-map=#{source}=#{destination}",
          "-fdebug-prefix-map=#{source}=#{destination}",
          "-fmacro-prefix-map=#{source}=#{destination}",
        ]
      end

      make_args = [
        "uname_S=Wasm32",
        "prefix=#{GUEST_HOMEBREW_PREFIX}",
        "gitexecdir=#{GUEST_GIT_EXEC_PATH}",
        "template_dir=#{GUEST_GIT_TEMPLATES}",
        "SHELL_PATH=/bin/sh",
        "RUNTIME_SHELL_PATH=#{GUEST_DASH}",
        "DEFAULT_EDITOR=#{GUEST_VIM_BIN}/vi",
        "CC=#{ENV.fetch("CC")}",
        "AR=#{ENV.fetch("AR")}",
        "RANLIB=#{ENV.fetch("RANLIB")}",
        "CFLAGS=-O2 -gline-tables-only #{prefix_map_flags.join(" ")}",
        "CROSS_COMPILING=YesPlease",
        "NO_PERL=YesPlease",
        "NO_PYTHON=YesPlease",
        "NO_TCLTK=YesPlease",
        "NO_GETTEXT=YesPlease",
        "NO_EXPAT=YesPlease",
        "NO_REGEX=NeedsStartEnd",
        "CURLDIR=#{libcurl}",
        "CURL_CONFIG=false",
        "CURL_LDFLAGS=#{curl_flags.join(" ")}",
        "OPENSSLDIR=#{openssl}",
        "ZLIB_PATH=#{zlib}",
        "PTHREAD_CFLAGS=-pthread",
        "PTHREAD_LIBS=-pthread",
        "HAVE_CLOCK_GETTIME=YesPlease",
        "HAVE_CLOCK_MONOTONIC=YesPlease",
        "HAVE_GETDELIM=YesPlease",
        "HAVE_PATHS_H=YesPlease",
        "HAVE_DEV_TTY=YesPlease",
      ]
      system "make", "-j#{ENV.make_jobs}", *make_args,
        *LINKED_PROGRAMS, "build-sh-script", *SHELL_LIBRARIES
      system "make", "-C", "templates", "SHELL_PATH=#{GUEST_DASH}", "PERL_PATH=/usr/bin/perl"
      PERL_HOOK_SAMPLES.each { |hook| rm buildpath/"templates/blt/hooks"/hook }

      SHELL_COMMANDS.each do |program|
        inreplace buildpath/program, "#!/bin/sh\n", "#!#{GUEST_DASH}\n"
      end

      LINKED_PROGRAMS.each do |program|
        artifact = kandelo_fork_instrument(buildpath/program)
        kandelo_validate_wasm_artifact(
          artifact,
          fork:            :required,
          forbidden_paths: [libcurl, openssl, zlib],
        )
      end
    end

    kandelo_install_bin(buildpath, "git", "git")
    git_core = libexec/"git-core"
    LINKED_PROGRAMS.drop(1).each do |program|
      chmod 0755, buildpath/program
      git_core.install buildpath/program
    end
    BUILTIN_ALIASES.each { |program| bin.install_symlink "git" => program }
    git_core.install_symlink "git-remote-http" => "git-remote-https"
    git_core.install_symlink "git-remote-http" => "git-remote-ftp"
    git_core.install_symlink "git-remote-http" => "git-remote-ftps"
    git_core.install(*SHELL_COMMANDS.map { |program| buildpath/program })
    git_core.install(*SHELL_LIBRARIES.map { |program| buildpath/program })
    (git_core/"mergetools").install (buildpath/"mergetools").children
    (share/"git-core/templates").install (buildpath/"templates/blt").children
    man.install resource("man")
  end

  test do
    assert_match(/^git version 2\.47\.1$/, kandelo_run_wasm(bin/"git", ["--version"]))
    assert_equal "#{GUEST_GIT_EXEC_PATH}\n", kandelo_run_wasm(bin/"git", ["--exec-path"])

    git_core = libexec/"git-core"
    coreutils_bin = formula_opt_bin("automattic/kandelo-homebrew/coreutils")
    vim_runtime = formula_opt_prefix("automattic/kandelo-homebrew/vim")/"share/vim/vim92"
    runtime_programs = {}
    runtime_programs["#{GUEST_GIT_BIN}/git"] = bin/"git"
    runtime_programs["#{GUEST_GIT_EXEC_PATH}/git-remote-http"] = git_core/"git-remote-http"
    runtime_programs["#{GUEST_GIT_EXEC_PATH}/git-remote-https"] = git_core/"git-remote-http"
    runtime_programs["#{GUEST_GIT_EXEC_PATH}/git-remote-ftp"] = git_core/"git-remote-http"
    runtime_programs["#{GUEST_GIT_EXEC_PATH}/git-remote-ftps"] = git_core/"git-remote-http"
    runtime_programs["#{GUEST_GIT_EXEC_PATH}/git-sh-i18n--envsubst"] = git_core/"git-sh-i18n--envsubst"
    COREUTILS_COMMANDS.each do |program|
      runtime_programs["#{GUEST_COREUTILS_BIN}/#{program}"] = coreutils_bin/program
    end
    runtime_programs["#{GUEST_DIFFUTILS_BIN}/diff"] = formula_opt_bin("automattic/kandelo-homebrew/diffutils")/"diff"
    runtime_programs["#{GUEST_GREP_BIN}/grep"] = formula_opt_bin("automattic/kandelo-homebrew/grep")/"grep"
    runtime_programs["#{GUEST_LESS_BIN}/less"] = formula_opt_bin("automattic/kandelo-homebrew/less")/"less"
    runtime_programs["#{GUEST_SED_BIN}/sed"] = formula_opt_bin("automattic/kandelo-homebrew/sed")/"sed"
    runtime_programs["#{GUEST_VIM_BIN}/vi"] = formula_opt_bin("automattic/kandelo-homebrew/vim")/"vi"
    runtime_programs["#{GUEST_VIM_BIN}/vim"] = formula_opt_bin("automattic/kandelo-homebrew/vim")/"vim"
    runtime_programs[GUEST_DASH] = formula_opt_bin("automattic/kandelo-homebrew/dash")/"dash"
    BUILTIN_ALIASES.each do |program|
      runtime_programs["#{GUEST_GIT_BIN}/#{program}"] = bin/program
    end
    SHELL_COMMANDS.each do |program|
      script = (git_core/program).binread
      assert_equal "#!#{GUEST_DASH}\n", script.lines.first
      runtime_programs["#{GUEST_GIT_EXEC_PATH}/#{program}"] = git_core/program
    end
    runtime_files = {}
    SHELL_LIBRARIES.each do |program|
      runtime_files["#{GUEST_GIT_EXEC_PATH}/#{program}"] = git_core/program
    end
    (git_core/"mergetools").glob("**/*").select(&:file?).each do |file|
      relative = file.relative_path_from(git_core/"mergetools")
      runtime_files["#{GUEST_GIT_EXEC_PATH}/mergetools/#{relative}"] = file
    end
    templates = share/"git-core/templates"
    templates.glob("**/*").select(&:file?).each do |file|
      relative = file.relative_path_from(templates)
      runtime_files["#{GUEST_GIT_TEMPLATES}/#{relative}"] = file
    end
    assert_path_exists vim_runtime/"autoload/paste.vim"
    assert_path_exists vim_runtime/"defaults.vim"
    assert_path_exists vim_runtime/"syntax/gitcommit.vim"
    assert_path_exists vim_runtime/"syntax/vim.vim"
    vim_runtime_files = vim_runtime.glob("**/*").select(&:file?)
    assert_operator vim_runtime_files.length, :>, 2_000
    editor_runtime_files = runtime_files.dup
    vim_runtime_files.each do |file|
      relative = file.relative_path_from(vim_runtime)
      editor_runtime_files["#{GUEST_VIM_RUNTIME}/#{relative}"] = file
    end
    host_path_pattern = %r{/(?:private/tmp/|Users/|home/runner/(?:_work|work)/|nix/store/)}
    PERL_HOOK_SAMPLES.each { |hook| refute_path_exists templates/"hooks"/hook }
    (templates/"hooks").glob("*.sample").each do |hook|
      assert_equal "#!#{GUEST_DASH}\n", hook.binread.lines.first
    end
    templates.glob("**/*").select(&:file?).each do |file|
      refute_match host_path_pattern, file.binread
    end
    assert_path_exists share/"git-core/templates/hooks/pre-commit.sample"
    assert_predicate bin/"git-upload-pack", :symlink?
    assert_path_exists git_core/"git-submodule"
    assert_path_exists git_core/"git-sh-setup"
    assert_path_exists git_core/"mergetools/vimdiff"
    assert_path_exists man1/"git.1"
    assert_path_exists man5/"gitrepository-layout.5"
    assert_path_exists man7/"gitworkflows.7"
    refute_path_exists git_core/"git-filter-branch"
    refute_path_exists git_core/"git-request-pull"

    repo = testpath/"repo"
    mount = { "/work" => testpath }
    env = {
      "GIT_CONFIG_NOSYSTEM" => "1",
      "GIT_CONFIG_COUNT"    => "3",
      "GIT_CONFIG_KEY_0"    => "user.name",
      "GIT_CONFIG_VALUE_0"  => "Kandelo Test",
      "GIT_CONFIG_KEY_1"    => "user.email",
      "GIT_CONFIG_VALUE_1"  => "test@kandelo.invalid",
      "GIT_CONFIG_KEY_2"    => "gc.auto",
      "GIT_CONFIG_VALUE_2"  => "0",
      "KERNEL_CWD"          => "/work",
      "KERNEL_PATH"         => [
        GUEST_GIT_EXEC_PATH,
        GUEST_GIT_BIN,
        GUEST_COREUTILS_BIN,
        GUEST_DIFFUTILS_BIN,
        GUEST_GREP_BIN,
        GUEST_LESS_BIN,
        GUEST_SED_BIN,
        GUEST_VIM_BIN,
      ].join(":"),
    }
    run_git = lambda do |argv, guest_env: {}, **options|
      kandelo_run_wasm(
        bin/"git", argv,
        argv0:                     "#{GUEST_GIT_BIN}/git",
        env:                       env.merge(guest_env),
        exec_programs:             runtime_programs,
        guest_files:               runtime_files,
        writable_host_directories: mount,
        **options
      )
    end

    init = run_git.call(["init", "repo"], merge_stderr: true)
    assert_match(/Initialized empty Git repository/, init)
    refute_match(/templates not found/, init)
    copied_templates = repo/".git"
    PERL_HOOK_SAMPLES.each { |hook| refute_path_exists copied_templates/"hooks"/hook }
    (copied_templates/"hooks").glob("*.sample").each do |hook|
      assert_equal "#!#{GUEST_DASH}\n", hook.binread.lines.first
    end
    copied_templates.glob("**/*").select(&:file?).each do |file|
      refute_match host_path_pattern, file.binread
    end

    (repo/"tracked.txt").write "Kandelo Git\n"
    assert_empty run_git.call(["-C", "repo", "add", "tracked.txt"])
    commit = run_git.call(["-C", "repo", "commit", "-m", "initial"], merge_stderr: true)
    assert_match(/\[master \(root-commit\) [0-9a-f]+\] initial/, commit)
    assert_equal "initial\n",
      run_git.call(["-C", "repo", "log", "-1", "--format=%s"])

    clone = run_git.call(
      ["clone", "file:///work/repo", "clone"], merge_stderr: true, expected_fork_descendants: 1
    )
    assert_match(/Cloning into 'clone'/, clone)
    assert_equal "Kandelo Git\n", (testpath/"clone/tracked.txt").read
    assert_empty run_git.call(
      ["-C", "clone", "submodule", "status"], merge_stderr: true, expected_fork_descendants: 1
    )

    shell_alias = <<~'SH'.chomp
      alias.kandelo=!git --version > nested-version.txt && \
        printf 'alias=%s\n' "$KANDELO_ALIAS_VALUE" > alias.txt
    SH
    assert_empty run_git.call(
      ["-C", "clone", "-c", shell_alias, "kandelo"],
      guest_env: { "KANDELO_ALIAS_VALUE" => "shell-ok" }, expected_fork_descendants: 2,
    )
    assert_equal "git version 2.47.1\n", (testpath/"clone/nested-version.txt").read
    assert_equal "alias=shell-ok\n", (testpath/"clone/alias.txt").read

    paged_log = kandelo_run_pty_wasm(
      bin/"git", ["-C", "clone", "--paginate", "-c", "color.ui=false", "log", "--oneline"],
      argv0:                     "#{GUEST_GIT_BIN}/git",
      env:                       env.merge("HOME" => "/work", "LESS" => "RX", "TERM" => "xterm-256color"),
      exec_programs:             runtime_programs,
      guest_files:               runtime_files,
      inputs:                    ["q"],
      writable_host_directories: mount,
      expected_fork_descendants: 1
    )
    assert_match(/\b[0-9a-f]{7,}\b.*\binitial\b/, paged_log)
    assert_match(/\(END\)/, paged_log)

    mergetool_help = run_git.call(
      ["-C", "clone", "mergetool", "--tool-help"],
      merge_stderr:              true,
      expected_fork_descendants: 4,
    )
    assert_match(/git mergetool --tool=<tool>/, mergetool_help)
    assert_match(/vimdiff/, mergetool_help)

    (testpath/"clone/editor.txt").write "editor workflow\n"
    assert_empty run_git.call(["-C", "clone", "add", "editor.txt"])
    %w[GIT_EDITOR VISUAL EDITOR].each { |variable| refute env.key?(variable) }
    vim_init = [
      "set nomore",
      "runtime autoload/paste.vim",
      "if !exists('*paste#Paste') | cquit | endif",
    ].join(" | ")
    editor_commit = kandelo_run_pty_wasm(
      bin/"git", ["-C", "clone", "commit"],
      argv0:                     "#{GUEST_GIT_BIN}/git",
      env:                       env.merge(
        "HOME"    => "/work",
        "TERM"    => "xterm-256color",
        "VIMINIT" => vim_init,
      ),
      exec_programs:             runtime_programs,
      guest_files:               editor_runtime_files,
      inputs:                    ["i", "editor commit", "\e", ":wq\r"],
      writable_host_directories: mount,
      expected_fork_descendants: 1
    )
    assert_match(/\[master [0-9a-f]+\] editor commit/, editor_commit)
    assert_equal "editor commit\n", run_git.call(["-C", "clone", "log", "-1", "--format=%s"])

    assert_equal "envsubst-ok\n", kandelo_run_wasm(
      git_core/"git-sh-i18n--envsubst", ["$KANDELO_ENVSUBST"],
      env: { "KANDELO_ENVSUBST" => "envsubst-ok" }, stdin: "$KANDELO_ENVSUBST\n"
    )

    capabilities = kandelo_run_wasm(
      git_core/"git-remote-http", ["origin", "http://example.invalid/"], stdin: "capabilities\n\n"
    )
    assert_match(/^fetch\n/, capabilities)
    assert_match(/^option\n/, capabilities)
    assert_match(/^object-format\n/, capabilities)

    remote_head = run_git.call(
      ["ls-remote", "https://github.com/git/git.git", "HEAD"],
      network: true, expected_fork_descendants: 1,
    )
    assert_match(/\A[0-9a-f]{40}\tHEAD\n\z/, remote_head)
  end
end

__END__
diff --git a/Makefile b/Makefile
index 2f256b3..182767d 100644
--- a/Makefile
+++ b/Makefile
@@ -2353,10 +2353,13 @@ BASIC_CFLAGS += -DDEFAULT_PAGER='$(DEFAULT_PAGER_CQ_SQ)'
 endif

-ifdef SHELL_PATH
-SHELL_PATH_CQ = "$(subst ",\",$(subst \,\\,$(SHELL_PATH)))"
-SHELL_PATH_CQ_SQ = $(subst ','\'',$(SHELL_PATH_CQ))
+ifndef RUNTIME_SHELL_PATH
+	RUNTIME_SHELL_PATH = $(SHELL_PATH)
+endif
+ifdef RUNTIME_SHELL_PATH
+RUNTIME_SHELL_PATH_CQ = "$(subst ",\",$(subst \,\\,$(RUNTIME_SHELL_PATH)))"
+RUNTIME_SHELL_PATH_CQ_SQ = $(subst ','\'',$(RUNTIME_SHELL_PATH_CQ))

-BASIC_CFLAGS += -DSHELL_PATH='$(SHELL_PATH_CQ_SQ)'
+BASIC_CFLAGS += -DSHELL_PATH='$(RUNTIME_SHELL_PATH_CQ_SQ)'
 endif

 GIT_USER_AGENT_SQ = $(subst ','\'',$(GIT_USER_AGENT))
