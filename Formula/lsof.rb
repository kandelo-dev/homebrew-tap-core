require (Tap.fetch("automattic", "kandelo-homebrew").path/"Kandelo/formula_support/kandelo_formula_support").to_s

class Lsof < Formula
  include KandeloFormulaSupport

  desc "Utility to list open files inside Kandelo"
  homepage "https://github.com/lsof-org/lsof"
  url "https://github.com/lsof-org/lsof/releases/download/4.99.7/lsof-4.99.7.tar.gz"
  sha256 "4a10391aab0b8ce1f539e82a1966693b2a6cf225972a6504ebb7ec4fa71675de"
  license "lsof"

  depends_on "binaryen" => :build
  depends_on "wabt" => :build

  skip_clean "bin/lsof"

  def install
    kandelo_require_arch!("wasm32")

    kandelo_wasm_build do
      # lsof records CC and CFLAGS in its runtime `-v` output. Keep those
      # values truthful but independent of the builder's checkout and temp
      # directory; the activated SDK already resolves this compiler via PATH.
      ENV["CC"] = "#{kandelo_arch}posix-cc"
      stable_source = "/usr/src/lsof-#{version}"
      ENV["CFLAGS"] = "-O2 -gline-tables-only -fdebug-compilation-dir=#{stable_source}"

      # Kandelo intentionally does not expose Linux AF_PACKET or AF_NETLINK
      # UAPI. Upstream only uses these headers for optional, guarded names.
      inreplace "lib/dialects/linux/dlsof.h" do |s|
        s.gsub! <<~OLD, <<~NEW
          #    include <linux/if_ether.h>
          #    include <linux/netlink.h>
        OLD
          #    if defined(__has_include)
          #        if __has_include(<linux/if_ether.h>)
          #            include <linux/if_ether.h>
          #        endif
          #        if __has_include(<linux/netlink.h>)
          #            include <linux/netlink.h>
          #        endif
          #    else
          #        include <linux/if_ether.h>
          #        include <linux/netlink.h>
          #    endif
        NEW
      end

      # Upstream selects the target dialect from --host but still reads the
      # build host's uname. Match Kandelo's sys_uname release so generated test
      # metadata describes the target and remains independent of the builder.
      inreplace "configure", "LSOF_VSTR=$(uname -r)", 'LSOF_VSTR="${LSOF_VSTR:-$(uname -r)}"'
      ENV["LSOF_VSTR"] = "1.0.0"

      build = Utils.safe_popen_read("./config.guess").strip
      system "./configure",
        "--build=#{build}",
        "--host=wasm32-unknown-linux-gnu",
        "--prefix=#{prefix}",
        "--disable-dependency-tracking",
        "--disable-liblsof",
        "--without-selinux"
      system "make", "-j#{ENV.make_jobs}", "lsof"
      kandelo_fork_instrument(buildpath/"lsof")
      kandelo_validate_wasm_artifact(buildpath/"lsof", fork: :required)
    end

    kandelo_install_bin(buildpath, "lsof", "lsof")
    man8.install "Lsof.8" => "lsof.8"
  end

  test do
    version_output = kandelo_run_wasm(bin/"lsof", ["-v"], merge_stderr: true)
    assert_match(/revision: 4\.99\.7$/, version_output)

    workdir = testpath/"working-directory"
    workdir.mkpath
    fields = kandelo_run_wasm(
      bin/"lsof",
      ["-nP", "-a", "-p", "100", "-d", "cwd,0-2", "-Fpcfnt"],
      env: { "KERNEL_CWD" => workdir },
    )
    assert_match(/^p100$/, fields)
    assert_match(/^clsof\.wasm$/, fields)
    assert_match(/^fcwd\ntDIR\nn#{Regexp.escape(workdir.to_s)}$/, fields)
    assert_match(%r{^f0\ntFIFO\nn/dev/stdin$}, fields)
    assert_match(%r{^f1\ntFIFO\nn/dev/stdout$}, fields)
    assert_match(%r{^f2\ntFIFO\nn/dev/stderr$}, fields)
  end
end
