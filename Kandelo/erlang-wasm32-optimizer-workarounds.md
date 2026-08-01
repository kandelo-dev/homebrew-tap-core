# Erlang wasm32 optimizer workarounds

Date recorded: 2026-07-28

## Decision

Kandelo's Erlang/OTP 28.2 bottle keeps the normal `-O2` release build, with
two narrowly owned exceptions:

1. `patch-global-h.py` replaces aggregate initialization of OTP's
   pointer-bearing inline work stacks with equivalent explicit field
   assignments.
2. Five ERTS translation units build at `-O1`:
   `erl_unicode.c`, `erl_bif_chksum.c`, `erl_db_util.c`, `erl_db_hash.c`, and
   `erl_db.c`.

The evidence points to an LLVM 21 wasm32 optimizer/code-generation boundary,
not permission to weaken Kandelo's kernel, POSIX, or Erlang semantics.
Native OTP produces the expected results, while affected wasm32 `-O2` objects
have produced corrupted inline-to-heap work-stack state. The workaround
therefore changes optimization only; it does not add package-specific success
paths, bounds lies, or synthetic results.

The checked-in patcher is intentionally fail-closed. It installs all five
generated Makefile rules atomically, accepts its own exact complete block as
idempotent, and rejects partial markers, pre-existing target rules, or an
ambiguous upstream anchor. This prevents an OTP update from quietly leaving a
mixed and unaudited subset of the workaround in place.

The former registry experiment that replaced `db_is_fully_bound` with a fixed
256-entry traversal and returned success at its bound is deliberately not
carried forward. That patch hid a runtime memory failure by changing ETS
behavior and was not a valid platform or package contract.

## Workaround ownership

| Input | Current treatment | Reason covered by the smoke matrix |
|---|---|---|
| `global.h` | explicit inline-stack field initialization | pointer-bearing ESTACK/WSTACK setup |
| `erl_unicode.c` | `-O1` | deep mixed Unicode/iodata traversal |
| `erl_bif_chksum.c` | `-O1` | direct, incremental, and seeded checksums over nested iodata |
| `erl_db_util.c` | `-O1` | ETS match-spec binding traversal |
| `erl_db_hash.c` | `-O1` | ETS hash-table match traversal |
| `erl_db.c` | `-O1` | keeps the connected ETS implementation at one optimizer boundary |

The matrix also detects the same class in translation units that remain at
`-O2`: external term encode/decode, term comparison and maps, deep term
hashing, term copying between Erlang processes, term formatting, and
`iolist_to_binary`. Those cases are detection coverage, not a claim that every
file needs the workaround.

## Oracle and host evidence

The expected terms were regenerated on 2026-07-28 with native Erlang/OTP 28
(ERTS 16.4). The checksum values were independently cross-checked against
standard MD5, CRC-32, and Adler-32 implementations. Inputs deliberately exceed
OTP's 16-entry inline work-stack size.

`KandeloErlangWasm32Smoke` runs these cases in one BEAM boot:

1. external-term round trip;
2. deep mixed Unicode conversion;
3. MD5 direct and incremental APIs, CRC-32 direct and seeded APIs, and
   Adler-32 direct and seeded APIs over nested mixed iodata;
4. ETS match-spec traversal;
5. heterogeneous deep-term comparison and sorting;
6. deep `phash2`;
7. deep-term process-message copying;
8. deep term formatting;
9. deeply nested `iolist_to_binary`;
10. parsing forms and compiling/loading them with `compile:forms`;
11. writing a real source file in the guest VFS and compiling/loading it with
    `compile:file`.

The Formula adds its installed `compiler-*/ebin` explicitly before the matrix.
It uses the same program and strict result parser in Node.js and Chromium.
Every named case must occur exactly once, no `FAIL` line may occur, and exactly
one matching `matrix_done` sentinel must be present. This makes a missing
compiler closure, a truncated boot, a skipped case, duplicate output, or a
host-only pass fail publication.

At the time this note was recorded, all eleven oracle expressions passed on
native OTP. The tap's patcher/parser unit tests and structural Formula checks
also pass. An exact rebuilt wasm32 bottle still needs to run the full matrix in
both Node.js and Chromium before the workaround can be described as validated
for the published artifact.

## Re-audit and removal criteria

Re-audit this decision whenever OTP, LLVM, the Kandelo SDK, or the wasm32
compiler flags change. For an OTP source update, the five-rule patcher must
either match the new generated Makefile exactly or fail the build; do not
loosen its anchors merely to make the build continue.

Remove an exception one translation unit at a time:

1. build that unit at `-O2` without changing any semantic test;
2. rebuild the exact candidate bottle from the reviewed tap head;
3. run the complete one-boot matrix in both Node.js and Chromium;
4. retain the exact logs and artifact identity;
5. remove the rule and its rationale only after both hosts are green.

Do not infer that a newer LLVM or OTP fixed the issue from a successful native
build, a BEAM version string, or a shallow arithmetic probe. Conversely, a new
failure in this matrix is platform feedback: localize the responsible
translation unit and compiler behavior before adding another optimizer
exception.
