#!/usr/bin/env python3
"""Install the reviewed wasm32 -O1 ERTS object rules exactly once."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import NoReturn


BEGIN_MARKER = "# BEGIN Kandelo wasm32 LLVM -O1 translation-unit workarounds"
END_MARKER = "# END Kandelo wasm32 LLVM -O1 translation-unit workarounds"
ANCHOR = "$(OBJDIR)/beam_emu.o: beam/emu/beam_emu.c"
RULES = (
    (
        "erl_unicode",
        "erl_unicode.c",
        "iodata traversal can corrupt its heap-backed ESTACK at -O2",
    ),
    (
        "erl_bif_chksum",
        "erl_bif_chksum.c",
        "checksum iodata traversal can corrupt its heap-backed ESTACK at -O2",
    ),
    (
        "erl_db_util",
        "erl_db_util.c",
        "db_is_fully_bound can read outside its DMC stack at -O2",
    ),
    (
        "erl_db_hash",
        "erl_db_hash.c",
        "match_traverse can corrupt its heap-backed DMC stack at -O2",
    ),
    (
        "erl_db",
        "erl_db.c",
        "keep the ETS implementation at one optimizer boundary",
    ),
)


def die(message: str) -> NoReturn:
    raise SystemExit(f"ERROR: {message}")


def render_block() -> str:
    lines = [
        BEGIN_MARKER,
        "# WHY: LLVM 21's wasm32 -O2 lowering miscompiles OTP's inline-to-heap",
        "# work-stack transition in these translation units. Keep the workaround",
        "# bounded and observable; see Kandelo/erlang-wasm32-optimizer-workarounds.md.",
    ]
    for object_name, source_name, reason in RULES:
        lines.extend(
            (
                f"# wasm32: {reason}.",
                f"$(OBJDIR)/{object_name}.o: beam/{source_name}",
                "\t$(V_CC) $(subst -O2,-O1,$(CFLAGS)) $(INCLUDES) -c $< -o $@",
                "",
            )
        )
    lines.append(END_MARKER)
    return "\n".join(lines)


def validate_complete(content: str, block: str) -> None:
    if content.count(BEGIN_MARKER) != 1 or content.count(END_MARKER) != 1:
        die("the generated ERTS Makefile does not contain one complete O1 marker pair")
    if content.count(block) != 1:
        die("the generated ERTS Makefile contains a partial or edited O1 rule block")
    if content.count(ANCHOR) != 1:
        die("the generated ERTS Makefile does not contain exactly one beam_emu rule anchor")
    for object_name, source_name, _reason in RULES:
        target = f"$(OBJDIR)/{object_name}.o: beam/{source_name}"
        if content.count(target) != 1:
            die(f"the generated ERTS Makefile does not contain exactly one {object_name} O1 rule")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        die(f"usage: {Path(argv[0]).name} GENERATED_ERTS_MAKEFILE")

    makefile = Path(argv[1])
    try:
        metadata = makefile.lstat()
    except OSError as error:
        die(f"cannot inspect generated ERTS Makefile {makefile}: {error}")
    if not stat.S_ISREG(metadata.st_mode) or makefile.is_symlink():
        die(f"generated ERTS Makefile must be a regular non-symlink file: {makefile}")

    try:
        original = makefile.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        die(f"cannot read generated ERTS Makefile {makefile}: {error}")

    block = render_block()
    target_lines = [
        f"$(OBJDIR)/{object_name}.o: beam/{source_name}"
        for object_name, source_name, _reason in RULES
    ]
    has_marker = BEGIN_MARKER in original or END_MARKER in original
    has_target = any(target in original for target in target_lines)

    if has_marker or has_target:
        # WHY: accepting a partly applied rule set can silently rebuild only
        # some affected objects at -O1. Treat only our exact complete block as
        # idempotent; every mixed or upstream-conflicting state is an error.
        validate_complete(original, block)
        return 0

    if original.count(ANCHOR) != 1:
        die(
            "expected exactly one beam_emu rule anchor before installing "
            f"the O1 rules, found {original.count(ANCHOR)}"
        )

    updated = original.replace(ANCHOR, f"{block}\n\n{ANCHOR}", 1)
    validate_complete(updated, block)

    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=makefile.parent,
            prefix=f".{makefile.name}.kandelo-o1.",
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as temporary:
            os.fchmod(temporary.fileno(), stat.S_IMODE(metadata.st_mode))
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, makefile)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    # Verify the bytes installed through the atomic replacement, not merely
    # the in-memory candidate that was intended for it.
    validate_complete(makefile.read_text(encoding="utf-8"), block)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
