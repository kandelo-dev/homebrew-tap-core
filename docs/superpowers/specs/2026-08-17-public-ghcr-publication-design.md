# Public GHCR Publication Design

## Goal

Every GitHub Container Registry (GHCR) package produced by ABI staging must be
created as a public package associated with `kandelo-dev/homebrew-tap-core`.
No publication lane may rely on a personal access token (PAT) for first
creation, because GitHub creates a new PAT-owned package as private.

## Existing failure

The shared OCI publisher already validates the desired result. Before writing
to an existing namespace it requires a public package associated with the tap.
After writing, it repeats that metadata check and anonymously reads the exact
manifest and blobs. A private package therefore fails publication immediately.

Workflow credential wiring is not shared, however. Candidate publication was
corrected to use the repository-scoped `GITHUB_TOKEN`, while history, product,
canonical, admission, reuse, verification, and maintenance workflows still
inject the dedicated package PAT. The first ABI history record exposed the
gap by creating a private package and then correctly failing anonymous
readback.

## Design

All protected staging publication steps will expose the same environment:

```yaml
HOMEBREW_GITHUB_PACKAGES_TOKEN: ${{ github.token }}
HOMEBREW_GITHUB_PACKAGES_USER: ${{ github.actor }}
```

Reusable workflows will no longer declare or receive the dedicated package
secret. Their publisher jobs already have `packages: write`, so the built-in
token remains bounded to the repository and job. Existing Python publication
code remains unchanged and continues to enforce public metadata, repository
association, immutable digest identity, and credential-free readback.

The workflow checker will enforce this as one repository-wide publication
contract. A mutation that restores any `secrets.HOMEBREW_GITHUB_PACKAGES_TOKEN`
or `vars.HOMEBREW_GITHUB_PACKAGES_USER` publication environment must fail the
fast Ruby contract suite.

## Rollout proof

The existing ABI 42 history package was manually made public to unblock ABI 43.
After merging this change, rerunning the history workflow must idempotently
publish/read the same digest with the repository token. The next reconciliation
wave must then create the first canonical ABI 43 package publicly and read it
without credentials. These hosted checks do not rebuild bottles.

## Non-goals

- Deleting legacy private receipt or reuse packages.
- Changing OCI schemas, repository names, or bottle bytes.
- Weakening immutable digest or anonymous-readback checks.
- Introducing an automatic package-deletion capability.
