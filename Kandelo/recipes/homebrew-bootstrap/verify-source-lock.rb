#!/usr/bin/env ruby
# frozen_string_literal: true

require "digest"
require "json"
require "optparse"

SHA256 = /\A[0-9a-f]{64}\z/
GIT_OID = /\A[0-9a-f]{40}\z/

def fail_lock(message)
  raise "homebrew-bootstrap source lock: #{message}"
end

def exact_keys(value, expected, label)
  fail_lock("#{label} must be an object") unless value.instance_of?(Hash)
  actual = value.keys.sort
  wanted = expected.sort
  fail_lock("#{label} fields must be exactly #{wanted.join(", ")}; got #{actual.join(", ")}") unless actual == wanted
end

def string_field(value, pattern, label)
  fail_lock("#{label} is invalid") unless value.instance_of?(String) && pattern.match?(value)
end

def positive_integer(value, label)
  fail_lock("#{label} must be a positive integer") unless value.instance_of?(Integer) && value.positive?
end

def regular_file(path, label)
  stat = File.lstat(path)
  fail_lock("#{label} must be a regular non-symlink file: #{path}") unless stat.file? && !stat.symlink?
  stat
rescue SystemCallError => e
  fail_lock("#{label} is unavailable: #{e.message}")
end

def real_directory(path, label)
  stat = File.lstat(path)
  fail_lock("#{label} must be a real directory: #{path}") unless stat.directory? && !stat.symlink?
  File.realpath(path)
rescue SystemCallError => e
  fail_lock("#{label} is unavailable: #{e.message}")
end

def file_sha256(path)
  Digest::SHA256.file(path).hexdigest
end

def load_lock(path)
  regular_file(path, "lock")
  lock = JSON.parse(File.binread(path))
  exact_keys(lock, %w[kind license outputs package patch prepared schema source], "lock")
  fail_lock("unsupported schema #{lock["schema"]}") unless lock["schema"] == 1
  unless lock["kind"] == "kandelo-homebrew-bootstrap-tap-recipe-lock"
    fail_lock("unsupported kind #{lock["kind"].inspect}")
  end

  package = lock.fetch("package")
  exact_keys(package, %w[arch name version], "package")
  fail_lock("package.name must be homebrew-bootstrap") unless package["name"] == "homebrew-bootstrap"
  string_field(package["version"], /\A[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-g[0-9a-f]{7,40}\z/, "package.version")
  fail_lock("package.arch must be wasm32") unless package["arch"] == "wasm32"

  source = lock.fetch("source")
  exact_keys(
    source,
    %w[archive_sha256 archive_url commit_timestamp repository revision tree_git_oid],
    "source",
  )
  unless source["repository"] == "https://github.com/Homebrew/brew.git"
    fail_lock("source.repository must identify anonymous upstream Homebrew")
  end
  string_field(source["revision"], GIT_OID, "source.revision")
  expected_url = "https://github.com/Homebrew/brew/archive/#{source["revision"]}.tar.gz"
  fail_lock("source.archive_url must be #{expected_url}") unless source["archive_url"] == expected_url
  string_field(source["archive_sha256"], SHA256, "source.archive_sha256")
  string_field(source["tree_git_oid"], GIT_OID, "source.tree_git_oid")
  positive_integer(source["commit_timestamp"], "source.commit_timestamp")

  patch = lock.fetch("patch")
  exact_keys(patch, %w[path sha256], "patch")
  unless patch["path"] == "patches/0001-add-kandelo-wasm-bottle-tags.patch"
    fail_lock("patch.path must identify the reviewed Kandelo patch")
  end
  string_field(patch["sha256"], SHA256, "patch.sha256")

  license = lock.fetch("license")
  exact_keys(license, %w[expression kandelo_patch upstream], "license")
  unless license["expression"] == "BSD-2-Clause AND GPL-2.0-or-later"
    fail_lock("license.expression must preserve both license boundaries")
  end
  upstream_license = license.fetch("upstream")
  exact_keys(upstream_license, %w[bytes path sha256 spdx], "license.upstream")
  unless upstream_license["spdx"] == "BSD-2-Clause" && upstream_license["path"] == "LICENSE.txt"
    fail_lock("license.upstream must identify Homebrew's LICENSE.txt")
  end
  string_field(upstream_license["sha256"], SHA256, "license.upstream.sha256")
  positive_integer(upstream_license["bytes"], "license.upstream.bytes")
  patch_license = license.fetch("kandelo_patch")
  exact_keys(patch_license, %w[evidence_path evidence_sha256 spdx], "license.kandelo_patch")
  unless patch_license["spdx"] == "GPL-2.0-or-later" &&
         patch_license["evidence_path"] == "PATCH-LICENSE.md"
    fail_lock("license.kandelo_patch must identify the reviewed Kandelo evidence")
  end
  string_field(patch_license["evidence_sha256"], SHA256, "license.kandelo_patch.evidence_sha256")

  prepared = lock.fetch("prepared")
  exact_keys(
    prepared,
    %w[archive_format patched_tree_git_oid patched_tree_sha256 portable_ruby_version],
    "prepared",
  )
  string_field(prepared["patched_tree_git_oid"], GIT_OID, "prepared.patched_tree_git_oid")
  string_field(prepared["patched_tree_sha256"], SHA256, "prepared.patched_tree_sha256")
  string_field(prepared["portable_ruby_version"], /\A[0-9]+\.[0-9]+\.[0-9]+(?:_[0-9]+)?\z/, "prepared.portable_ruby_version")
  unless prepared["archive_format"] == "kandelo-deterministic-zip-v1"
    fail_lock("prepared.archive_format is unsupported")
  end

  outputs = lock.fetch("outputs")
  exact_keys(outputs, %w[archive environment], "outputs")
  {
    "archive" => "homebrew-bootstrap.zip",
    "environment" => "homebrew-brew.env",
  }.each do |name, expected_path|
    output = outputs.fetch(name)
    exact_keys(output, %w[bytes path sha256], "outputs.#{name}")
    fail_lock("outputs.#{name}.path must be #{expected_path}") unless output["path"] == expected_path
    string_field(output["sha256"], SHA256, "outputs.#{name}.sha256")
    positive_integer(output["bytes"], "outputs.#{name}.bytes")
  end

  lock
rescue JSON::ParserError => e
  fail_lock("lock is invalid JSON: #{e.message}")
end

def verify_file(path, expected, label)
  stat = regular_file(path, label)
  fail_lock("#{label} has #{stat.size} bytes, expected #{expected.fetch("bytes")}") unless stat.size == expected.fetch("bytes")
  actual = file_sha256(path)
  fail_lock("#{label} SHA-256 #{actual} does not match #{expected.fetch("sha256")}") unless actual == expected.fetch("sha256")
end

def provenance(lock, upstream_tree, patched_tree, patched_tree_sha256)
  {
    "schema" => 1,
    "kind" => "kandelo-homebrew-bootstrap-tap-recipe-provenance",
    "source_repository" => lock.dig("source", "repository"),
    "source_revision" => lock.dig("source", "revision"),
    "source_archive_sha256" => lock.dig("source", "archive_sha256"),
    "source_tree_git_oid" => upstream_tree,
    "patch_sha256" => lock.dig("patch", "sha256"),
    "patched_tree_git_oid" => patched_tree,
    "patched_tree_sha256" => patched_tree_sha256,
    "archive_format" => lock.dig("prepared", "archive_format"),
    "homebrew_archive_sha256" => lock.dig("outputs", "archive", "sha256"),
    "homebrew_environment_sha256" => lock.dig("outputs", "environment", "sha256"),
    "homebrew_bottle_arch" => lock.dig("package", "arch"),
    "homebrew_bottle_tag" => "#{lock.dig("package", "arch")}_kandelo",
  }
end

options = {}
parser = OptionParser.new do |opts|
  opts.on("--lock PATH") { |value| options["lock"] = value }
  opts.on("--field NAME") { |value| options["field"] = value }
  opts.on("--package-name NAME") { |value| options["package-name"] = value }
  opts.on("--package-version VERSION") { |value| options["package-version"] = value }
  opts.on("--target-arch ARCH") { |value| options["target-arch"] = value }
  opts.on("--source-url URL") { |value| options["source-url"] = value }
  opts.on("--source-sha256 SHA") { |value| options["source-sha256"] = value }
  opts.on("--source-dir PATH") { |value| options["source-dir"] = value }
  opts.on("--patch PATH") { |value| options["patch"] = value }
  opts.on("--license-evidence PATH") { |value| options["license-evidence"] = value }
  opts.on("--upstream-tree OID") { |value| options["upstream-tree"] = value }
  opts.on("--patched-tree OID") { |value| options["patched-tree"] = value }
  opts.on("--patched-tree-sha256 SHA") { |value| options["patched-tree-sha256"] = value }
  opts.on("--archive PATH") { |value| options["archive"] = value }
  opts.on("--environment PATH") { |value| options["environment"] = value }
  opts.on("--write-provenance PATH") { |value| options["write-provenance"] = value }
  opts.on("--provenance PATH") { |value| options["provenance"] = value }
end
parser.parse!
fail_lock("--lock is required") unless options["lock"]
lock = load_lock(options.fetch("lock"))

if options["field"]
  fail_lock("--field cannot be combined with other verification options") unless options.length == 2
  fields = {
    "source.tree_git_oid" => lock.dig("source", "tree_git_oid"),
    "source.commit_timestamp" => lock.dig("source", "commit_timestamp"),
    "patch.sha256" => lock.dig("patch", "sha256"),
    "prepared.patched_tree_git_oid" => lock.dig("prepared", "patched_tree_git_oid"),
    "prepared.patched_tree_sha256" => lock.dig("prepared", "patched_tree_sha256"),
    "outputs.archive.path" => lock.dig("outputs", "archive", "path"),
    "outputs.environment.path" => lock.dig("outputs", "environment", "path"),
  }
  fail_lock("unsupported field #{options["field"].inspect}") unless fields.key?(options["field"])
  puts fields.fetch(options["field"])
  exit
end

{
  "package-name" => lock.dig("package", "name"),
  "package-version" => lock.dig("package", "version"),
  "target-arch" => lock.dig("package", "arch"),
  "source-url" => lock.dig("source", "archive_url"),
  "source-sha256" => lock.dig("source", "archive_sha256"),
}.each do |name, expected|
  next unless options.key?(name)
  fail_lock("#{name} mismatch: expected #{expected}, got #{options[name]}") unless options[name] == expected
end

if options["source-dir"]
  source = real_directory(options.fetch("source-dir"), "source directory")
  portable_ruby = File.join(source, "Library/Homebrew/vendor/portable-ruby-version")
  regular_file(portable_ruby, "portable Ruby version")
  expected = "#{lock.dig("prepared", "portable_ruby_version")}\n"
  fail_lock("portable Ruby version must be exactly #{expected.inspect}") unless File.binread(portable_ruby) == expected
  upstream_license = lock.dig("license", "upstream")
  verify_file(File.join(source, upstream_license.fetch("path")), upstream_license, "upstream license")
end

if options["patch"]
  regular_file(options.fetch("patch"), "reviewed patch")
  actual = file_sha256(options.fetch("patch"))
  fail_lock("reviewed patch SHA-256 mismatch") unless actual == lock.dig("patch", "sha256")
end
if options["license-evidence"]
  regular_file(options.fetch("license-evidence"), "patch license evidence")
  actual = file_sha256(options.fetch("license-evidence"))
  fail_lock("patch license evidence SHA-256 mismatch") unless actual == lock.dig("license", "kandelo_patch", "evidence_sha256")
end

upstream_tree = options["upstream-tree"]
patched_tree = options["patched-tree"]
patched_tree_sha256 = options["patched-tree-sha256"]
if upstream_tree || patched_tree || patched_tree_sha256
  fail_lock("all tree identities are required together") unless upstream_tree && patched_tree && patched_tree_sha256
  fail_lock("upstream Git tree mismatch") unless upstream_tree == lock.dig("source", "tree_git_oid")
  fail_lock("patched Git tree mismatch") unless patched_tree == lock.dig("prepared", "patched_tree_git_oid")
  fail_lock("patched tree SHA-256 mismatch") unless patched_tree_sha256 == lock.dig("prepared", "patched_tree_sha256")
end

verify_file(options.fetch("archive"), lock.dig("outputs", "archive"), "output archive") if options["archive"]
verify_file(options.fetch("environment"), lock.dig("outputs", "environment"), "environment policy") if options["environment"]

expected_provenance = nil
if options["write-provenance"]
  fail_lock("tree identities are required to write provenance") unless upstream_tree
  fail_lock("--archive and --environment are required to write provenance") unless options["archive"] && options["environment"]
  expected_provenance = provenance(lock, upstream_tree, patched_tree, patched_tree_sha256)
  output = options.fetch("write-provenance")
  fail_lock("provenance output already exists: #{output}") if File.exist?(output) || File.symlink?(output)
  File.open(output, File::WRONLY | File::CREAT | File::EXCL, 0o600) do |file|
    file.write("#{JSON.pretty_generate(expected_provenance)}\n")
  end
end

if options["provenance"]
  regular_file(options.fetch("provenance"), "provenance")
  actual = JSON.parse(File.binread(options.fetch("provenance")))
  expected_provenance ||= provenance(
    lock,
    lock.dig("source", "tree_git_oid"),
    lock.dig("prepared", "patched_tree_git_oid"),
    lock.dig("prepared", "patched_tree_sha256"),
  )
  exact_keys(actual, expected_provenance.keys, "provenance")
  fail_lock("provenance differs from the source lock") unless actual == expected_provenance
end
