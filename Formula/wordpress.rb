require (Tap.fetch("kandelo-dev", "tap-core").path/"Kandelo/formula_support/kandelo_formula_support").to_s
require "digest"

class Wordpress < Formula
  include KandeloFormulaSupport

  EXPECTED_FILE_COUNT = 3_951
  EXPECTED_LOGICAL_BYTES = 86_075_858
  EXPECTED_TREE_SHA256 = "bcc068ee09f664333bc4eaeeffc158fd5cc53c8b00679e9fe1979e2965fed6ef".freeze
  EXPECTED_ROOT_ENTRIES = %w[
    index.php
    license.txt
    readme.html
    wp-activate.php
    wp-admin
    wp-blog-header.php
    wp-comments-post.php
    wp-config-sample.php
    wp-content
    wp-cron.php
    wp-includes
    wp-links-opml.php
    wp-load.php
    wp-login.php
    wp-mail.php
    wp-settings.php
    wp-signup.php
    wp-trackback.php
    xmlrpc.php
  ].freeze

  desc "Open-source publishing application files for Kandelo"
  homepage "https://wordpress.org/"
  url "https://wordpress.org/wordpress-7.0.tar.gz"
  sha256 "530c8fdeb16fb0affdb53eb727b6a04bb8d166621c20029e389cabb01a0fa921"
  license "GPL-2.0-or-later"

  # WHY: PHP interprets every supported WordPress deployment, while its
  # database and HTTP server are replaceable composition choices.
  depends_on "kandelo-dev/tap-core/php"

  def install
    kandelo_require_arch!("wasm32")

    # WHY: this Formula owns only immutable upstream application files.
    # Database state, wp-config.php, deployment-added plugins and their
    # activation, the web server, and service supervision are deployment
    # policy and belong in the consuming VFS.
    source_entries = buildpath.children
    actual_entries = source_entries.map { |path| path.basename.to_s }.sort
    odie "WordPress source root changed" if actual_entries != EXPECTED_ROOT_ENTRIES.sort
    pkgshare.install source_entries
  end

  test do
    files = pkgshare.glob("**/*", File::FNM_DOTMATCH)
                    .select(&:file?)
                    .sort_by { |path| path.relative_path_from(pkgshare).to_s.b }
    manifest = files.map do |path|
      relative = path.relative_path_from(pkgshare)
      "#{relative}\0#{path.size}\0#{Digest::SHA256.file(path).hexdigest}\n"
    end.join

    # The digest binds every installed regular-file path and byte, rather than
    # letting a small smoke fixture hide an incomplete application-data bottle.
    assert_equal EXPECTED_FILE_COUNT, files.length
    assert_equal EXPECTED_LOGICAL_BYTES, files.sum(&:size)
    assert_equal EXPECTED_TREE_SHA256, Digest::SHA256.hexdigest(manifest)

    assert_equal "49e97f629ce763abb2f8f1f13da74cb5a8a9a72a0e72a7477d1540c98cef5141",
      Digest::SHA256.file(pkgshare/"license.txt").hexdigest
    assert_match(/^\$wp_version = '7\.0';$/, (pkgshare/"wp-includes/version.php").read)

    refute_path_exists pkgshare/"wp-config.php"
    refute_path_exists pkgshare/"wp-content/db.php"
    refute_path_exists pkgshare/"wp-content/plugins/sqlite-database-integration"
  end
end
