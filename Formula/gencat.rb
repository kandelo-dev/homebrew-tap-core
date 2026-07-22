require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Gencat < Formula
  include KandeloFormulaSupport

  desc "Compile POSIX message catalogs for Kandelo"
  homepage "https://man.netbsd.org/gencat.1"
  url "https://raw.githubusercontent.com/NetBSD/src/16d4549f49e1a0303ad8d287696bb114ff1da3e1/usr.bin/gencat/gencat.c"
  version "1.37"
  sha256 "b5e640fea67f8066432ce02e5f13d1a9e3b4763a4c5f8cc5f3cc55baa12fdff4"
  license all_of: ["BSD-2-Clause", "BSD-3-Clause", "ISC"]

  depends_on KandeloFormulaSupport::BinaryenRequirement => [:build, :test]
  depends_on KandeloFormulaSupport::WabtRequirement => [:build, :test]

  skip_clean "bin/gencat"

  resource "queue-header" do
    url "https://raw.githubusercontent.com/NetBSD/src/f45f136e1025fac365e04da67b33bf8b395118e9/sys/sys/queue.h"
    sha256 "e05d47e82c2d3b2a08ecf70662d1857e019ab762a9ec8bdc2d473d987eb7adfd"
  end

  resource "manpage" do
    url "https://raw.githubusercontent.com/NetBSD/src/16d4549f49e1a0303ad8d287696bb114ff1da3e1/usr.bin/gencat/gencat.1"
    sha256 "606db067ddd22ab8603bc494a4ebd35f4309b7949f8f87d139873ff9f0886e4b"
  end

  def install
    kandelo_require_arch!("wasm32")
    compat = buildpath/"compat"
    (compat/"sys").mkpath
    resource("queue-header").stage { (compat/"sys").install "queue.h" }
    (compat/"sys/cdefs.h").write <<~HEADER
      #ifndef KANDELO_NETBSD_CDEFS_COMPAT_H
      #define KANDELO_NETBSD_CDEFS_COMPAT_H
      #include <stdint.h>
      #ifdef __cplusplus
      # define __BEGIN_DECLS extern "C" {
      # define __END_DECLS }
      #else
      # define __BEGIN_DECLS
      # define __END_DECLS
      #endif
      #define __dead __attribute__((__noreturn__))
      #define __format_arg(x)
      #endif
    HEADER
    (compat/"nls_catalog_format.h").write <<~HEADER
      #ifndef KANDELO_NETBSD_NLS_CATALOG_FORMAT_H
      #define KANDELO_NETBSD_NLS_CATALOG_FORMAT_H
      #include <stdint.h>

      /* Fixed big-endian NetBSD catalog records consumed by musl catopen. */
      #define _NLS_MAGIC UINT32_C(0xff88ff89)
      struct _nls_cat_hdr {
        int32_t __magic;
        int32_t __nsets;
        int32_t __mem;
        int32_t __msg_hdr_offset;
        int32_t __msg_txt_offset;
      };
      struct _nls_set_hdr {
        int32_t __setno;
        int32_t __nmsgs;
        int32_t __index;
      };
      struct _nls_msg_hdr {
        int32_t __msgno;
        int32_t __msglen;
        int32_t __offset;
      };
      _Static_assert(sizeof(struct _nls_cat_hdr) == 20, "catalog header layout");
      _Static_assert(sizeof(struct _nls_set_hdr) == 12, "set header layout");
      _Static_assert(sizeof(struct _nls_msg_hdr) == 12, "message header layout");
      #endif
    HEADER

    inreplace buildpath/"gencat.c", "#define _NLS_PRIVATE\n", ""
    inreplace buildpath/"gencat.c", "#include <nl_types.h>\n",
              "#include <nl_types.h>\n#include \"nls_catalog_format.h\"\n"

    artifact = buildpath/"gencat.wasm"
    kandelo_wasm_build do |root|
      stable_source = "/usr/src/netbsd-gencat-#{version}"
      mapped_roots = {
        buildpath.to_s               => stable_source,
        root.to_s                    => "/usr/src/kandelo",
        Pathname(root).realpath.to_s => "/usr/src/kandelo",
        "/nix/store"                 => "/usr/src/toolchain",
      }
      prefix_maps = mapped_roots.uniq.flat_map do |from, to|
        [
          "-ffile-prefix-map=#{from}=#{to}",
          "-fdebug-prefix-map=#{from}=#{to}",
          "-fmacro-prefix-map=#{from}=#{to}",
        ]
      end

      # Only NetBSD's documented wire records are supplied locally. Both this
      # producer and the consumer in `test do` use Kandelo's public musl header.
      system kandelo_cc,
        "-std=c17", "-O2", "-gline-tables-only", "-D_POSIX_C_SOURCE=200809L",
        '-Dgetprogname()="gencat"', "-I#{compat}",
        "-fdebug-compilation-dir=#{stable_source}", *prefix_maps,
        buildpath/"gencat.c", "-o", artifact
      kandelo_validate_wasm_artifact(
        artifact,
        fork:            :forbidden,
        forbidden_paths: [buildpath.to_s, prefix.to_s],
      )
    end

    kandelo_install_bin(buildpath, artifact.basename, "gencat")
    resource("manpage").stage { man1.install "gencat.1" }
  end

  test do
    assert_path_exists man1/"gencat.1"

    workspace = testpath/"workspace"
    workspace.mkpath
    (workspace/"initial.msg").write <<~MSG
      $set 1
      1 original
      2 delete-me
    MSG
    (workspace/"secondary.msg").write <<~MSG
      $set 2
      1 preserved
    MSG
    (workspace/"default.msg").write "2 second\n1 first\n3 \n4 continued \\\nacross lines\n"
    (workspace/"update.msg").write <<~'MSG'
      $set 1
      1 replaced
      2
      $quote "
      3 "added\040value"
    MSG

    mount = { "/work" => workspace }
    env = { "KERNEL_CWD" => "/work" }
    assert_empty kandelo_run_wasm(
      bin/"gencat", ["messages.cat", "initial.msg", "secondary.msg"],
      env: env, writable_host_directories: mount
    )
    assert_equal [0xFF, 0x88, 0xFF, 0x89], (workspace/"messages.cat").binread.bytes.first(4)
    assert_empty kandelo_run_wasm(
      bin/"gencat", ["default.cat", "default.msg"],
      env: env, writable_host_directories: mount
    )

    streamed = kandelo_run_wasm(bin/"gencat", ["-", "-"], stdin: "1 streamed\n").b
    assert_equal [0xFF, 0x88, 0xFF, 0x89], streamed.bytes.first(4)

    # Updating through stdin exercises existing-catalog parsing, replacement,
    # deletion, quoted escapes, seeking, and truncation.
    assert_empty kandelo_run_wasm(
      bin/"gencat", ["messages.cat", "-"],
      env: env, stdin: (workspace/"update.msg").read, writable_host_directories: mount
    )

    source = testpath/"catalog-reader.c"
    reader = testpath/"catalog-reader.wasm"
    source.write <<~C
      #include <nl_types.h>
      #include <stdio.h>
      #include <string.h>

      int main(void) {
        nl_catd catalog = catopen("/work/messages.cat", 0);
        if (catalog == (nl_catd)-1) return 1;
        puts(catgets(catalog, 1, 1, "missing-replacement"));
        puts(catgets(catalog, 1, 2, "deleted"));
        puts(catgets(catalog, 1, 3, "missing-addition"));
        puts(catgets(catalog, 2, 1, "missing-preserved"));
        puts(catgets(catalog, 9, 9, "fallback"));
        if (catclose(catalog) != 0) return 2;

        catalog = catopen("/work/default.cat", 0);
        if (catalog == (nl_catd)-1) return 3;
        puts(catgets(catalog, NL_SETD, 1, "missing-first"));
        puts(catgets(catalog, NL_SETD, 2, "missing-second"));
        printf("empty=%zu\\n", strlen(catgets(catalog, NL_SETD, 3, "missing-empty")));
        puts(catgets(catalog, NL_SETD, 4, "missing-continuation"));
        return catclose(catalog) == 0 ? 0 : 4;
      }
    C
    kandelo_wasm_build do
      system kandelo_cc, "-std=c17", "-O2", source, "-o", reader
      kandelo_validate_wasm_artifact(reader, fork: :forbidden)
    end
    expected = <<~EXPECTED
      replaced
      deleted
      added value
      preserved
      fallback
      first
      second
      empty=0
      continued across lines
    EXPECTED
    assert_equal expected, kandelo_run_wasm(
      reader, [], env: env, writable_host_directories: mount
    )

    missing = kandelo_run_wasm(
      bin/"gencat", ["missing.cat", "absent.msg"],
      env: env, merge_stderr: true, writable_host_directories: mount, expected_status: 1
    )
    assert_match "Unable to read absent.msg", missing

    invalid = kandelo_run_wasm(
      bin/"gencat", ["invalid.cat", "-"],
      env: env, stdin: "$set 0\n1 invalid\n", merge_stderr: true,
      writable_host_directories: mount, expected_status: 1
    )
    assert_match "setId's must be greater than zero", invalid
  end

  bottle do
    root_url "https://ghcr.io/v2/kandelo-dev/homebrew-tap-core"
    sha256 cellar: :any_skip_relocation, wasm32_kandelo: "15797b34192270f24156cdb4c8ff354f486c2dda787291390b0f00218b43aee0"
  end

end
