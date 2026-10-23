# checkouts

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Reports the git state of the configured checkout folders.
- A read-only collector: networked by default through one `git ls-remote` per
  distinct remote, which transfers no objects; `--no-network` skips that check.
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
