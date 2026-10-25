# checkouts

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Reports the git state of the configured checkout folders.
- A read-only collector: networked by default through one `git ls-remote` per
  distinct remote, which transfers no objects, and, when the workspace turns
  GitHub lookup on, one optional `gh` request per GitHub repository for its pull
  requests; `--no-network` skips both.
- Keeps the last successful remote checks (the `ls-remote` answer and the pull
  requests) in the verdict cache file, and answers from them, labelled
  `recorded`, when a run is offline or a check fails; a clone's own newer
  remote-tracking ref takes precedence.
- Never writes or locks a repository.

## Outside this boundary

- Fetching, writing or locking a repository; the board, which never shows
  checkout state.

## Collaborators

- Depends on `catalog`, `protocols`, and `account-state`.
- Used by `lifecycle-control` (the `nyx` command line).
- Reuses the catalog byte bound; see
  [catalog invariants](../catalog/README.md#cross-system-invariants).
- Calls `derive_removable`; see
  [protocols invariants](../protocols/README.md#cross-system-invariants).
