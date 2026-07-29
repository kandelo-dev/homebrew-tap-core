#!/usr/bin/env python3
"""Validate the registry-free WordPress application-data Formula contract."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORMULA = ROOT / "Formula" / "wordpress.rb"

FORBIDDEN_COMPOSITION_POLICY = (
    "KANDELO_REGISTRY_BRIDGE",
    "KANDELO_TAP_RECIPE",
    "kandelo_build_package",
    "kandelo_build_tap_recipe",
    "packages/registry",
    "wordpress.vfs",
    'depends_on "kandelo-dev/tap-core/mariadb"',
    'depends_on "kandelo-dev/tap-core/nginx"',
    'depends_on "kandelo-dev/tap-core/dinit"',
    'depends_on "kandelo-dev/tap-core/msmtpd"',
)


def require_all(source: str, needles: tuple[str, ...]) -> None:
    for needle in needles:
        assert needle in source, f"{FORMULA} is missing {needle!r}"


def main() -> None:
    source = FORMULA.read_text()
    require_all(
        source,
        (
            'url "https://wordpress.org/wordpress-7.0.tar.gz"',
            'sha256 "530c8fdeb16fb0affdb53eb727b6a04bb8d166621c20029e389cabb01a0fa921"',
            'license "GPL-2.0-or-later"',
            'depends_on "kandelo-dev/tap-core/php"',
            'kandelo_require_arch!("wasm32")',
            "EXPECTED_ROOT_ENTRIES = %w[",
            'odie "WordPress source root changed" if actual_entries != EXPECTED_ROOT_ENTRIES.sort',
            "pkgshare.install source_entries",
            "EXPECTED_FILE_COUNT = 3_951",
            "EXPECTED_LOGICAL_BYTES = 86_075_858",
            'EXPECTED_TREE_SHA256 = "bcc068ee09f664333bc4eaeeffc158fd5cc53c8b00679e9fe1979e2965fed6ef"',
            "Digest::SHA256.file(path).hexdigest",
            'Digest::SHA256.file(pkgshare/"license.txt").hexdigest',
        ),
    )

    install = re.search(r"  def install\n(?P<body>.*?)\n  end", source, re.DOTALL)
    assert install is not None
    for forbidden in FORBIDDEN_COMPOSITION_POLICY:
        assert forbidden not in install.group("body"), (
            f"{FORMULA} install owns composite-image policy {forbidden!r}"
        )

    dependencies = re.findall(r'^\s*depends_on "([^"]+)"', source, re.MULTILINE)
    assert dependencies == ["kandelo-dev/tap-core/php"], dependencies

    # A package bottle must contain pristine core, not generated per-machine
    # state or either database choice used by Kandelo's current demos.
    require_all(
        source,
        (
            'refute_path_exists pkgshare/"wp-config.php"',
            'refute_path_exists pkgshare/"wp-content/db.php"',
            'refute_path_exists pkgshare/"wp-content/plugins/sqlite-database-integration"',
        ),
    )

    print("WordPress Formula contract: ok")


if __name__ == "__main__":
    main()
