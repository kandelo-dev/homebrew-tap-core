#!/usr/bin/env perl
use strict;
use warnings;

use File::Basename qw(dirname);
use File::Path qw(make_path);
use TeXLive::TLPDB;
use TeXLive::TLUtils;

@ARGV == 5
  or die "usage: generate-texlive-runtime-config.pl ROOT SELECTED OUTPUT SNAPSHOT KPSEWHICH\n";
my ($root, $selected_file, $output_dir, $snapshot, $kpsewhich) = @ARGV;

-d "$root/texmf-dist" or die "missing generator texmf-dist: $root\n";
-f "$root/tlpkg/texlive.tlpdb" or die "missing generator tlpdb: $root\n";
-d "$root/tlpkg/TeXLive" or die "missing generator TeX Live modules: $root\n";
-f $selected_file or die "missing selected-package list: $selected_file\n";
-x $kpsewhich or die "host kpsewhich is not executable: $kpsewhich\n";
$snapshot =~ /\A[0-9]{8}\z/ or die "invalid TeX Live snapshot: $snapshot\n";

open(my $selected_fh, "<", $selected_file)
  or die "cannot read $selected_file: $!\n";
my %selected;
while (my $line = <$selected_fh>) {
  chomp $line;
  $line =~ /\A[-+.0-9A-Za-z]+\z/ or die "invalid selected package: $line\n";
  exists $selected{$line} and die "duplicate selected package: $line\n";
  $selected{$line} = 1;
}
close($selected_fh) or die "cannot close $selected_file: $!\n";
scalar(keys %selected) == 784
  or die "expected 784 selected packages, found " . scalar(keys %selected) . "\n";

my $tlpdb = TeXLive::TLPDB->new(root => $root)
  or die "could not load pinned TeX Live package database\n";
for my $name (keys %selected) {
  $tlpdb->get_package($name) or die "selected package missing from tlpdb: $name\n";
}
# TLUtils reads the usertree option from this metadata record. It owns no
# language records or payload and is not part of the 784-package closure.
my $installation_metadata = "00texlive.installation";
$tlpdb->get_package($installation_metadata)
  or die "installation metadata missing from tlpdb\n";
for my $name ($tlpdb->list_packages) {
  $tlpdb->remove_tlpobj($name)
    unless exists $selected{$name} or $name eq $installation_metadata;
}
my @filtered_packages = $tlpdb->list_packages;
scalar(@filtered_packages) == 785
  or die "filtered TeX Live package database has the wrong package count\n";

make_path($output_dir);
TeXLive::TLUtils::create_language_dat($tlpdb, "$output_dir/language.dat", undef);
TeXLive::TLUtils::create_language_def($tlpdb, "$output_dir/language.def", undef);
TeXLive::TLUtils::create_language_lua($tlpdb, "$output_dir/language.dat.lua", undef);

my $runtime_root = "$output_dir/runtime";
my $runtime_config = "$runtime_root/texmf-config";
my $runtime_var = "$runtime_root/texmf-var";
my $runtime_home = "$runtime_root/home";
my $runtime_local = "$runtime_root/texmf-local";
make_path($runtime_config, $runtime_var, $runtime_home, $runtime_local);
$ENV{PATH} = dirname($kpsewhich) . ":" . $ENV{PATH};
$ENV{HOME} = $runtime_home;
$ENV{LC_ALL} = "C";
$ENV{TZ} = "UTC";
$ENV{TEXMFCNF} = "$root/texmf-dist/web2c";
$ENV{TEXMFROOT} = $root;
$ENV{TEXMFDIST} = "$root/texmf-dist";
$ENV{TEXMFCONFIG} = $runtime_config;
$ENV{TEXMFSYSCONFIG} = $runtime_config;
$ENV{TEXMFVAR} = $runtime_var;
$ENV{TEXMFSYSVAR} = $runtime_var;
$ENV{TEXMFHOME} = "$runtime_home/texmf";
$ENV{TEXMFLOCAL} = $runtime_local;
$ENV{TEXFONTMAPS} = "$runtime_var/fonts/map//:$root/texmf-dist/fonts/map//";

my $updmap_config = "$output_dir/updmap.cfg";
TeXLive::TLUtils::create_updmap($tlpdb, $updmap_config);
open(my $updmap_fh, "<", $updmap_config)
  or die "cannot read generated $updmap_config: $!\n";
my @updmap_lines = <$updmap_fh>;
close($updmap_fh) or die "cannot close generated $updmap_config: $!\n";
my @map_lines = grep { /\A(?:Map|MixedMap|KanjiMap) / } @updmap_lines;
scalar(@map_lines) == 52
  or die "expected 52 selected font-map entries, found " . scalar(@map_lines) . "\n";

my $updmap = "$root/texmf-dist/scripts/texlive/updmap.pl";
-f $updmap or die "missing pinned updmap.pl: $updmap\n";
my $updmap_stdout = "$output_dir/updmap.stdout";
my $updmap_stderr = "$output_dir/updmap.stderr";
open(my $saved_stdout, ">&", \*STDOUT) or die "cannot save stdout: $!\n";
open(my $saved_stderr, ">&", \*STDERR) or die "cannot save stderr: $!\n";
open(STDOUT, ">", $updmap_stdout) or die "cannot capture updmap stdout: $!\n";
open(STDERR, ">", $updmap_stderr) or die "cannot capture updmap stderr: $!\n";
my $updmap_status = system($^X, $updmap, "--sys", "--nohash", "--cnffile", $updmap_config);
open(STDOUT, ">&", $saved_stdout) or die "cannot restore stdout: $!\n";
open(STDERR, ">&", $saved_stderr) or die "cannot restore stderr: $!\n";
close($saved_stdout) or die "cannot close saved stdout: $!\n";
close($saved_stderr) or die "cannot close saved stderr: $!\n";
open(my $updmap_stdout_fh, "<", $updmap_stdout)
  or die "cannot read updmap stdout: $!\n";
open(my $updmap_stderr_fh, "<", $updmap_stderr)
  or die "cannot read updmap stderr: $!\n";
my $updmap_output = do {
  local $/;
  <$updmap_stdout_fh>;
};
my $updmap_errors = do {
  local $/;
  <$updmap_stderr_fh>;
};
close($updmap_stdout_fh) or die "cannot close updmap stdout: $!\n";
close($updmap_stderr_fh) or die "cannot close updmap stderr: $!\n";
print STDOUT $updmap_output if length($updmap_output);
print STDERR $updmap_errors if length($updmap_errors);
$updmap_status == 0 or die "pinned updmap failed with status $updmap_status\n";
length($updmap_errors) == 0 or die "pinned updmap emitted diagnostics\n";
$updmap_output !~ /\b(?:WARNING|ERROR)\b/i
  or die "pinned updmap reported a warning or error\n";

sub normalize_banner {
  my ($path, $comment) = @_;
  open(my $input, "<", $path) or die "cannot read generated $path: $!\n";
  local $/;
  my $contents = <$input>;
  close($input) or die "cannot close generated $path: $!\n";

  my $replacement = "$comment Generated by TeX Live $snapshot selected package closure\n";
  my $count = ($contents =~ s/\A\Q$comment\E Generated by [^\n]+ on [^\n]+\n/$replacement/);
  $count == 1 or die "unexpected generated banner in $path\n";

  open(my $output, ">", $path) or die "cannot normalize generated $path: $!\n";
  print {$output} $contents or die "cannot write normalized $path: $!\n";
  close($output) or die "cannot close normalized $path: $!\n";
}

normalize_banner("$output_dir/language.dat", "%");
normalize_banner("$output_dir/language.dat.lua", "--");
normalize_banner($updmap_config, "#");

sub normalize_map_header {
  my ($path, $filename) = @_;
  open(my $input, "<", $path) or die "cannot read generated $path: $!\n";
  my @lines = <$input>;
  close($input) or die "cannot close generated $path: $!\n";
  scalar(@lines) > 6 or die "generated map is unexpectedly short: $path\n";
  $lines[0] =~ /\A% .*:\n\z/ or die "unexpected generated map filename header: $path\n";
  $lines[1] eq "% maintained by updmap[-sys] (multi).\n"
    or die "unexpected generated map maintenance header: $path\n";
  $lines[2] eq "% Don't change this file directly. Use updmap[-sys] instead.\n"
    or die "unexpected generated map edit header: $path\n";
  $lines[3] eq "% See the updmap documentation.\n"
    or die "unexpected generated map documentation header: $path\n";
  $lines[4] eq "% A log of the run that created this file is available here:\n"
    or die "unexpected generated map log header: $path\n";
  $lines[5] =~ /\A% .*updmap\.log\n\z/ or die "unexpected generated map log path: $path\n";
  $lines[0] = "% $filename:\n";
  $lines[5] = "% Generated by TeX Live $snapshot selected package closure\n";

  open(my $output, ">", $path) or die "cannot normalize generated $path: $!\n";
  print {$output} @lines or die "cannot write normalized $path: $!\n";
  close($output) or die "cannot close normalized $path: $!\n";
}

my $pdftex_map_dir = "$runtime_var/fonts/map/pdftex/updmap";
for my $filename (qw(pdftex.map pdftex_dl14.map pdftex_ndl14.map)) {
  normalize_map_header("$pdftex_map_dir/$filename", $filename);
}
