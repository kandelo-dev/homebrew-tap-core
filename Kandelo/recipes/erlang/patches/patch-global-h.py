#!/usr/bin/env python3
"""Patch OTP's ESTACK/WSTACK initialization for wasm32.

LLVM's wasm32 backend miscompiles aggregate initialization of structs that
contain pointers to shadow-stack local arrays at -O2. The replacement performs
the same initialization field by field. The recipe accepts only the exact
checksum-bound OTP source, and this script still fails closed if any expected
macro boundary is missing, duplicated, or only partially patched.
"""

from pathlib import Path
import sys


PATCH_MARKERS = (
    "static inline ErtsEStack estack_make_default_",
    "static inline ErtsWStack wstack_make_default_",
    "#ifdef __wasm32__\n#define DECLARE_ESTACK",
    "#ifdef __wasm32__\n#define WSTACK_DECLARE",
)


def replace_region(content, start_marker, end_marker, replacement, label):
    start_count = content.count(start_marker)
    end_count = content.count(end_marker)
    if start_count != 1 or end_count != 1:
        raise SystemExit(
            f"{label}: expected one bounded source region, "
            f"found start={start_count} end={end_count}"
        )
    start = content.index(start_marker)
    end = content.index(end_marker, start)
    if end <= start:
        raise SystemExit(f"{label}: source region boundaries are reversed")
    return content[:start] + replacement + content[end:]


def patch(path):
    source = Path(path)
    content = source.read_text()

    present = [marker in content for marker in PATCH_MARKERS]
    if any(present):
        if not all(present) or any(content.count(marker) != 1 for marker in PATCH_MARKERS):
            raise SystemExit(f"{source}: partially applied wasm32 stack patch")
        print(f"  {source}: already patched")
        return

    estack_default = r"""#ifdef __wasm32__
static inline ErtsEStack estack_make_default_(Eterm *arr, ErtsAlcType_t at) {
    ErtsEStack s;
    s.start = arr; s.sp = arr;
    s.end = arr + DEF_ESTACK_SIZE;
    s.edefault = arr; s.alloc_type = at;
    return s;
}
#define ESTACK_DEFAULT_VALUE(estack_default_stack_array, alloc_type)    \
    estack_make_default_((estack_default_stack_array), (alloc_type))
#else
#define ESTACK_DEFAULT_VALUE(estack_default_stack_array, alloc_type)    \
    (ErtsEStack) {                                                      \
        estack_default_stack_array,  /* start */                        \
        estack_default_stack_array,  /* sp */                           \
        estack_default_stack_array + DEF_ESTACK_SIZE, /* end */         \
        estack_default_stack_array,  /* default */                      \
        alloc_type /* alloc_type */                                     \
    }
#endif"""
    content = replace_region(
        content,
        "#define ESTACK_DEFAULT_VALUE(estack_default_stack_array, alloc_type)",
        "\n\n#define DECLARE_ESTACK",
        estack_default,
        "ESTACK_DEFAULT_VALUE",
    )

    declare_estack = """#ifdef __wasm32__
#define DECLARE_ESTACK(s)\t\t\t\t\\
    Eterm ESTK_DEF_STACK(s)[DEF_ESTACK_SIZE];\t\t\\
    ErtsEStack s;\t\t\t\t\t\\
    (s).start = ESTK_DEF_STACK(s);\t\t\t\\
    (s).sp = ESTK_DEF_STACK(s);\t\t\t\t\\
    (s).end = ESTK_DEF_STACK(s) + DEF_ESTACK_SIZE;\t\\
    (s).edefault = ESTK_DEF_STACK(s);\t\t\t\\
    (s).alloc_type = ERTS_ALC_T_ESTACK
#else
#define DECLARE_ESTACK(s)\t\t\t\t\\
    Eterm ESTK_DEF_STACK(s)[DEF_ESTACK_SIZE];\t\t\\
    ErtsEStack s = {\t\t\t\t\t\\
        ESTK_DEF_STACK(s),  /* start */ \t\t\\
        ESTK_DEF_STACK(s),  /* sp */\t\t\t\\
        ESTK_DEF_STACK(s) + DEF_ESTACK_SIZE, /* end */\t\\
        ESTK_DEF_STACK(s),  /* default */\t\t\\
        ERTS_ALC_T_ESTACK /* alloc_type */\t\t\\
    }
#endif"""
    content = replace_region(
        content,
        "#define DECLARE_ESTACK(s)",
        "\n\n#define ESTACK_CHANGE_ALLOCATOR",
        declare_estack,
        "DECLARE_ESTACK",
    )

    wstack_default = r"""#ifdef __wasm32__
static inline ErtsWStack wstack_make_default_(UWord *arr, ErtsAlcType_t at) {
    ErtsWStack s;
    s.wstart = arr; s.wsp = arr;
    s.wend = arr + DEF_WSTACK_SIZE;
    s.wdefault = arr; s.alloc_type = at;
    return s;
}
#define WSTACK_DEFAULT_VALUE(wstack_default_stack_array, alloc_type)    \
    wstack_make_default_((wstack_default_stack_array), (alloc_type))
#else
#define WSTACK_DEFAULT_VALUE(wstack_default_stack_array, alloc_type)    \
    (ErtsWStack) {                                                      \
        wstack_default_stack_array,  /* start */                        \
        wstack_default_stack_array,  /* sp */                           \
        wstack_default_stack_array + DEF_ESTACK_SIZE, /* end */         \
        wstack_default_stack_array,  /* default */                      \
        alloc_type /* alloc_type */                                     \
    }
#endif"""
    content = replace_region(
        content,
        "#define WSTACK_DEFAULT_VALUE(wstack_default_stack_array, alloc_type)",
        "\n\n#define WSTACK_DECLARE",
        wstack_default,
        "WSTACK_DEFAULT_VALUE",
    )

    declare_wstack = """#ifdef __wasm32__
#define WSTACK_DECLARE(s)\t\t\t\t\\
    UWord WSTK_DEF_STACK(s)[DEF_WSTACK_SIZE];\t\t\\
    ErtsWStack s;\t\t\t\t\t\\
    (s).wstart = WSTK_DEF_STACK(s);\t\t\t\\
    (s).wsp = WSTK_DEF_STACK(s);\t\t\t\\
    (s).wend = WSTK_DEF_STACK(s) + DEF_WSTACK_SIZE;\t\\
    (s).wdefault = WSTK_DEF_STACK(s);\t\t\t\\
    (s).alloc_type = ERTS_ALC_T_ESTACK
#else
#define WSTACK_DECLARE(s)\t\t\t\t\\
    UWord WSTK_DEF_STACK(s)[DEF_WSTACK_SIZE];\t\t\\
    ErtsWStack s = {\t\t\t\t\t\\
        WSTK_DEF_STACK(s),  /* wstart */ \t\t\\
        WSTK_DEF_STACK(s),  /* wsp */\t\t\t\\
        WSTK_DEF_STACK(s) + DEF_WSTACK_SIZE, /* wend */\t\\
        WSTK_DEF_STACK(s),  /* wdflt */ \t\t\\
        ERTS_ALC_T_ESTACK /* alloc_type */\t\t\\
    }
#endif"""
    content = replace_region(
        content,
        "#define WSTACK_DECLARE(s)",
        "\n#define DECLARE_WSTACK",
        declare_wstack,
        "WSTACK_DECLARE",
    )

    missing = [marker for marker in PATCH_MARKERS if content.count(marker) != 1]
    if missing:
        raise SystemExit(f"{source}: incomplete wasm32 stack patch: {missing}")

    source.write_text(content)
    print(f"  {source}: patched")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-global.h>")
        sys.exit(1)
    patch(sys.argv[1])
