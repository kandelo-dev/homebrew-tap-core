# Deferred campaign/trust implementation

This directory preserves campaign-wide publication and trust machinery that is
not part of the active Homebrew shipping lane. Files below this directory are
historical implementation material, not executable GitHub Actions workflows or
current release contracts.

The archive includes the obsolete campaign and contract-check workflows, the
consumed first-package namespace canary, publisher-trust rotation/checking, and
the campaign controller/source/authority-transition tools. It also contains the
old mirror finalizer that edited the archived checker. Active caller changes
rely on ordinary code review while provenance enforcement is deferred; this
lane does not claim a replacement trust-rotation contract.

The active lane treats each bottle as an independent published object.
`.github/workflows/publish-bottles.yml` builds and publishes those objects. A
canonical checked-in selection chooses exact bottle bytes for one VFS image,
and `.github/workflows/selection-checks.yml` validates that data using a pinned
Kandelo parser. VFS composition and Node/browser acceptance run from Kandelo.

Do not restore these files to active workflow paths without a new design and an
explicit decision to adopt campaign-wide publication semantics.
