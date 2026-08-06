# Prefix campaign target source

This directory stages the reviewed `/opt/kandelo/homebrew` cutover without
changing the tap's active Formulae, helper, bootstrap recipe, metadata, or
bottle selections.

`source/` mirrors the 48 files that differ between the protected base
recorded in `manifest.json` and the reviewed target tree. The manifest
binds every base preimage and target file by mode, byte length, Git blob
ID, and SHA-256. The caller authority additionally binds the canonical
manifest bytes, the complete `source/` Git tree, and the reconstructed
target tree.

The staged tree is data, not a second public tap. Validation requires all live
destinations to remain at their recorded base identities. A build or finalizer
may materialize the overlay only in a separate output directory:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/prefix-campaign-source.py \
  materialize \
  --out /absolute/path/to/new-empty-directory
```

Do not copy individual staged files into the live tap. The caller authority
must remain non-active while the campaign is prepared: `inert` before its
executor is fixed and `armed` after the protected workflow tree is installed.
Only an anonymously verified campaign may activate the authority, and only the
campaign finalizer may replace the active files together with all selected
Formula bottle blocks and generated catalog metadata in one atomic tap commit.

The publication workflow handles one `(Formula, architecture)` variant
at a time. A build selected for wasm32 uses the exact rootfs package
generation bound by the caller authority. A reused bottle does not need
a package generation: the reviewed Kandelo executor checks out the
campaign's exact historical tap commit, downloads the public bottle
again, verifies every recorded byte and dependency, and produces the
same immutable handoff contract as a new build. The tap controller does
not duplicate those reuse rules.

New wasm64 builds are intentionally unavailable in this campaign. The
earlier browser-input generation includes complete browser images rather
than the smaller package build runtime, and its wasm64 closure cannot be
produced for Formulae that do not support wasm64. Admission fails before
dispatch instead of selecting that impossible input. An already-built
wasm64 bottle may still be reused because byte verification does not
execute a Formula.

Each successful handoff is independently immutable and anonymously
readable. It can be used to prepare a closed VFS selection even while
unrelated campaign variants fail. Only the optional whole-catalog final
tap commit waits for the complete selected campaign.

## Publish campaign authority

A campaign release is accepted only when its asset inventory contains
exactly one file named `campaign.json`. The file name is part of the
authority contract, in addition to the bytes, SHA-256, release tag, and
target commit.

Only the protected workflow may write this release:
`.github/workflows/publish-prefix-campaign-release.yml`. After
independently deriving the campaign from the exact newly merged armed
commit `T_ARM`, record its SHA-256 as `C` and dispatch only that tuple:

```sh
gh workflow run publish-prefix-campaign-release.yml \
  --repo kandelo-dev/homebrew-tap-core \
  --ref main \
  -f expected_caller_sha="$T_ARM" \
  -f expected_campaign_sha256="$C"
```

The workflow reconstructs the campaign from independent exact inputs. It
then builds and validates the schema-1 immutable-release manifest. That
manifest declares `campaign.json` with its exact byte count and SHA-256,
makes it the only preferred asset, and accepts no existing asset sets.
It targets the exact source tap commit and uses the content-addressed
campaign tag.

Do not invoke `publish-immutable-github-release.sh` locally, even with a
personal token. A local invocation is not the protected Actions run that
owns the publication lock and exact execution authority. Do not supply
another workflow's run ID to imitate that owner.

Do not hand-roll this lifecycle with `gh release create`. In particular,
GitHub CLI's `path#label` syntax sets a display label; it does not rename
the uploaded asset. The pinned publisher owns tag creation, locking,
draft reconciliation, exact asset upload, protected-main rechecks,
publication, immutability checks, and anonymous readback. With release
immutability enabled, a published naming mistake cannot be corrected in
place.

Before activating the campaign, query the public release and require all
of these facts:

- `draft` and `prerelease` are both `false`;
- `immutable` is `true`;
- `target_commitish` is the campaign's exact source tap commit;
- the asset inventory is exactly `campaign.json`; and
- the asset byte count and `sha256:` digest match the derived file and
  content-addressed tag.

Then use Kandelo's `fetch-campaign-release` command without credentials.
It independently downloads the public bytes and writes the readback
receipt used by campaign operations. Do not activate an authority from a
maintainer-authenticated metadata check alone.

### Rejected immutable release from 2026-08-03

Release
`homebrew-prefix-campaign-sha256-9c8ba0ddd90f64bbbde0a182fee5154dc1ae6c74a967d5088b82a7f1dd4e5061`
is intentionally orphaned. Its bytes and digest are correct, but its sole
asset is named `campaign-7abe0a1.json` instead of `campaign.json`.
The controller rejects that inventory. No campaign authority may name
this release.

## Abandoned campaigns

An active campaign can be returned to its fail-closed `armed` state when its
frozen publisher cannot build the reviewed source. That transition clears the
campaign release, package generation, and source-tap commit together. It does
not delete public evidence or make an old handoff valid for a later campaign.
If the successor corrects the sealed target source, `archive-active` must also
receive all three new content identities and verify them against the checked-in
manifest and inert source tree before it writes the armed authority.

`aborted-campaigns/` retains the exact abandoned authority, dispatches, and
public handoffs. The bounded recovery path may rebind a prior reuse handoff
without rebuilding or reinspecting its bottle only when the ABI, bottle bytes,
Formula source, guest layout, validation contract, and dependency digests are
all unchanged. A workflow failure after complete publication does not make its
handoff verified archive evidence by itself. Recovery must retain the frozen
terminal ledger unchanged, identify the protected verifier correction, and
bind a separately durable credential-free readback supplement before the
archive may classify that exact release as publicly verified. Its successor
receipt must name both campaigns and the immutable predecessor handoff. A
fresh build or any changed input must use the normal successor task instead.

The successor scope for campaign `01cc2e9a...` partitions the canonical
41-task wasm32 shell graph into 38 exact predecessor handoffs and three fresh
builds: Git, Ruby, and Vim. Findutils and Less reuse their immutable `01cc`
handoffs; they are not rebuilt merely because the original workflows failed
after publishing them.

The successor to campaign `9705e20f...` keeps that same canonical 41-task
graph but partitions it into 39 exact C5 handoffs and fresh Git and Ruby
builds. Its terminal predecessor archive is sealed as
`de031d03eb2d9d598bc00f7bfe34538dc07fbbc27ef76f1ace22b83382a07b4e`,
and `9705-successor-scope.json` is sealed as
`258e85edff6610e4d478abb6d8b887561b39a80e3f20f6bd8ba3b3a017992f64`.
The selected Git source changes only the bounded mergetool guest deadline from
120000 to 180000 ms; its exact 749 descendant statuses and output assertions
remain unchanged. That authority stayed armed until protected Kandelo `main`
and its publicly verified rootfs generation provided the exact executable
pins.

The historical C7 successor to campaign `f692a88a...` started from active C6
tap commit `1d7d63673d70c7204fef83f9284f4367b30a8b8a` and keeps that same canonical
41-task graph and sealed target overlay. The graph remains sealed as
`40a651d2ebe3a3aaab4bf9b65d91cf34db9908cb764a518437ac850747c4b139`.
The C7 scope partitions it into 40 exact direct C6 handoffs and one fresh Ruby
build. Its terminal predecessor archive is sealed as
`3b1e288aadb23fa85db549cfc874aabc035756a18bace01b606ed0d1c54b9f07`,
and `f692-successor-scope.json` is sealed as
`227830740f1c179e6194b32d7383d358b321763d1bbb7ff2ec029a549a47c315`.
Git's complete public C6 handoff is among the reused inputs. Ruby rebuilds
under exact protected Kandelo commit
`c157026d1234c9a28dc630d02f963828525897a7` and public rootfs generation
`package-generation-rootfs-wasm32-abi-v42-sha256-f44d50ad73b5bdd6c6f396b47806babff3b3fdc6869ee9f1d2f88f9460581fb4`.
That executor corrects the LLVM failure boundary by sealing and admitting the
exact prefix runtime root `etc/clang`; it does not admit a broader `etc` tree.
The authority remained armed until the content-addressed C7 campaign was
published from the protected arm commit and anonymously verified; protected
commit `454e5d54456c8d870496bacc0ba9c2759c863ab1` then activated it.

The historical C8 successor to campaign `8edea42a...` started from that active C7 commit with
the same canonical graph and sealed target overlay. The C8 scope again contains
40 exact predecessor handoffs and one fresh Ruby build, but its predecessor is
the terminal C7 campaign. The archived C7 evidence is sealed as
`76c26c5af78a97bdcb840884451ca007ab95a37645b7db7804008646b2ca4150`;
`8ede-successor-scope.json` is sealed as
`dce71abbeb512b74adb3469a1388ccbdcbbfda28c124fe46f6773d96b8e59841`.
Ruby rebuilds under exact protected Kandelo commit
`75885de70c80448f08600b31a9466608e369713c` and public rootfs generation
`package-generation-rootfs-wasm32-abi-v42-sha256-697af3ea327198ae4fcfb8100662e504cf58d32de4b2045423b821c6e905a0a5`.
Run `31017507098` failed before recipe execution because authenticated LLVM
22.1.8 expands to 2,624,809,107 regular-file bytes, above the former 2 GiB
native-keg aggregate. C8 raises only each authenticated native tool keg and its
exact target-Cellar proxy to 4 GiB. The 1 GiB per-file limit and the 2 GiB
true-target-dependency, recipe-source, and recipe-output limits remain
unchanged.

The historical C9 successor started from active C8 tap commit
`9bbdbd334e4f45bf780e4d139cda1dc865a21419` and terminal campaign
`a516aa5e61f4b7513c18c3e5b279a6a1f2d8b07e6a7348706238bc261a63ada4`.
That terminal ledger retains 40 publicly verified handoffs and failed
Ruby/wasm32 run `31043674986` with no handoff. Its canonical archive is
`aborted-campaigns/a516aa5e61f4b7513c18c3e5b279a6a1f2d8b07e6a7348706238bc261a63ada4.json`,
sealed as
`7d8a7a9d1ac4df5c5dda459990384a5fe296511217053edf2a8d13c16703a483`.
The C9 `successor/a516-successor-scope.json` is sealed as
`a721afcecf9cde3185dcb6d5791a80e35ae99169bdd1a82666d63775ac32e187`
and selects exactly those 40 C8 handoffs plus one fresh Ruby build against the
unchanged canonical graph.

Ruby failed before recipe execution because the generic target-Cellar seal
correctly rejected its copied LLVM proxy's launcher-registered `etc/clang`
link into the separately sealed native prefix. The C9 correction does not make
cross-prefix target links generally valid. It recognizes only immutable proxy
kegs recorded by the launcher's native-bridge transaction, revalidates their
exact source, target, ownership, modes, and opt link, and subjects retained
links to the component-aware sealed-native projection audit. An unregistered,
redirected, writable, or changed proxy remains a hard failure.

The C9 arm was finalized with these then-live, independently verified
identities:

- `45a45fed06ff053ee4dd2cc2bb6564a99d5ce106`; and
- `package-generation-rootfs-wasm32-abi-v42-sha256-e3701277b519832435260e183b83ca7e1e82b12f84de6c24605db03552719e40`.

They remained current through C11. Historical C6 through C11 identities remain
audit evidence and are not rewritten during C12 finalization.

The C10 successor starts from active C9 tap commit
`47c232b5332ff2acad25c301ef6ba5f3f1e883b1` and terminal campaign
`f3f4cb4cda613c5cb6bbc73ec1a6952d3454971bfa92a31c9a10f9526b7308c3`.
All 40 reused tasks have public immutable C9 handoffs. Ruby/wasm32 run
`31062254998` passed admission, planning, sealed source materialization, native
dependency preparation, and installation of its Libyaml and Zlib handoffs. It
then entered the isolated tap-recipe runner and stopped before compilation
because `Kandelo/recipes/ruby/build.sh` required the intentionally unavailable
publisher checkout variable `HOMEBREW_KANDELO_ROOT`. No Ruby bottle or handoff
was produced.

The terminal archive
`aborted-campaigns/f3f4cb4cda613c5cb6bbc73ec1a6952d3454971bfa92a31c9a10f9526b7308c3.json`
is sealed as `a451e756879e38dea3834ee873d445fbfff8777ecd6812a9876c0129dd65dce8`. The new
`successor/f3f4-successor-scope.json` is sealed as
`4cfbb756def4280f4a9b74d330ba1f4c34298308da88dd0f1b0730764a7ec8b1` and selects those exact 40 C9 handoffs plus
one fresh Ruby build against the unchanged canonical graph.

That correction changed only the sealed tap target source. It staged the Ruby
recipe explicitly, consumed the authenticated `WASM_POSIX_LOCAL_ROOT_SPILL`
and `WASM_POSIX_FORK_INSTRUMENT` paths, and copied the authenticated Ruby source
into a writable work directory before patching it. The corrected target is
sealed by manifest `48f2f519beba22237d857b7b6860d5eccb57d5cb8abad2d7733f10b424fb34bf`, source tree
`7917903175fb2f75714ec2bc6fa0ab603efb6975`, and target tree
`af6215547bcd9fb2703e5f358721f7283b97eaee`. C10 was armed at
`c4039570825e9a0bd5932f84f933056368ccdf0a`, activated at
`5fec71d3e3de0f0fc8a0b543bee0c4afbe4bb810`, and published campaign
`ac950955718d406fa3ee31a7396c22c13ede154f948673f28171ca49592c2f34`.

C10 verified all 40 reused tasks. Ruby/wasm32 run `31069244063` installed its
declared authenticated `gpatch` dependency and entered the isolated recipe
runner, but stopped before patching or compilation because the Linux `gpatch`
keg exposes `bin/patch`, not the macOS-prefixed executable `gpatch`. It
published no Ruby bottle or handoff.

The C11 successor starts from active C10 tap commit
`5fec71d3e3de0f0fc8a0b543bee0c4afbe4bb810`. Its terminal predecessor archive
is `aborted-campaigns/ac950955718d406fa3ee31a7396c22c13ede154f948673f28171ca49592c2f34.json`,
sealed as `f861ae7e8b4f2669ec1851a943c1ac6ad92c780e20e2e38fac5785cd84109b15`.
The new `successor/ac95-successor-scope.json` is sealed as
`a5073d0351dd3d802b87bb0ff48052dc741c12e547e0184963549846cf81aba5`
and selects all 40 exact C10 handoffs plus one fresh Ruby build against the
unchanged canonical graph.

The C11 tap-only correction passes exact declared-keg paths for patch, make,
Perl, and Python into the recipe and invokes those paths directly, preventing
ambient host tools from satisfying declared dependencies. It also removes the
unused Formula-level Rust build dependency; local-root-spill remains an exact
separately sealed campaign input. The corrected target is sealed by manifest
`3359e8d45d6c04de2d3cac146c225a3bc54beb176b4018d082b337c7a49c298e`, source tree
`17bcb5910fd3d403d861b695f9ee945f1ce14d30`, and target tree
`f235ec029446883f067db5ea5d7e179710167dc6`. C11 used exact Kandelo executor
`45a45fed06ff053ee4dd2cc2bb6564a99d5ce106` and exact rootfs generation
`package-generation-rootfs-wasm32-abi-v42-sha256-e3701277b519832435260e183b83ca7e1e82b12f84de6c24605db03552719e40`.
It was armed at `be405601ca9cbc8cff9aa3ce023e0490040cd035`, activated at
`f4daa689d89b2de2a4359bf358854a7db130ca97`, and published campaign
`b0476cd05b16a835bd42292bcd34bffdada50f6d06bb1129bc106a9f86763896`.

C11 verified all 40 reuse tasks. Ruby/wasm32 run `31075257926` passed
campaign admission and planning, then failed in the signed native API contract
before native dependency installation, Formula recipe execution, bottle
publication, or handoff publication because the signed Homebrew API selected
a newer `python@3.13` than the checked-in compatibility lock.

The C12 successor starts from active C11 tap commit
`f4daa689d89b2de2a4359bf358854a7db130ca97`. Its terminal predecessor archive
is
`aborted-campaigns/b0476cd05b16a835bd42292bcd34bffdada50f6d06bb1129bc106a9f86763896.json`,
sealed as `0c31f4b6a4eb24f1bc193a1b807d9352e81a76a3995453020c5bd16847573f32`.
The new `successor/b047-successor-scope.json` is sealed as
`84a43358c03dd6700b2edf6c337f7d22523af69207a07eb9babc99452c7a0d88`
and selects all 40 exact C11 handoffs plus one fresh Ruby build against the
unchanged canonical graph.

C12 advances executable authority to exact protected Kandelo commit
`af80a443a6b4820e3b04845a64ab5cb8854638cd` and exact independently admitted
rootfs generation
`package-generation-rootfs-wasm32-abi-v42-sha256-7ed33d5d51b7362c2ac04c0aca812a49c859bde25a2930d0e876f1c1e1aafcc9`.
The target source remains unchanged: manifest
`3359e8d45d6c04de2d3cac146c225a3bc54beb176b4018d082b337c7a49c298e`,
source tree `17bcb5910fd3d403d861b695f9ee945f1ce14d30`, and target tree
`f235ec029446883f067db5ea5d7e179710167dc6`.
