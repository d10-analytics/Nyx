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
- Matches each specification package's `Target repo:` to the checkout at that
  path: a clone, a linked worktree (resolved to its clone), or a discovered
  checkout whose clone could not be read. Paths compare by `os.stat` identity
  (device and inode, which follows symbolic links and letter case), never as
  strings. A target that matches nothing is classified from the filesystem
  alone as missing, not a git checkout, or outside the checkout folders, so
  git never runs in a directory discovery did not admit; with git unavailable a
  usable target is unchecked. A path inside a clone that is not its top level
  is not a git checkout. A path that cannot be examined at all (for example,
  access denied) is reported as not a git checkout, because no `.git` entry can
  be shown. A discovered checkout whose clone's path, as git lists
  it, the document cannot carry (`unrepresentable_path`) has no clone record, so
  a target at the checkout is reported as outside the checkout folders; this is
  a known limit.
- `nyx/checkout_join.py` holds the matching rules over plain facts and makes no
  git call and reads no file; `nyx/checkouts.py` owns every git call and
  filesystem read and builds the path identities it is given.
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
