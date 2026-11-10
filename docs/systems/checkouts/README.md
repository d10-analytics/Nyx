# checkouts

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Reports the git state of the configured checkout folders.
- On macOS, when git resolves to the system `/usr/bin/git` Command Line Tools
  shim, checks the developer tools before the first git child: the pure
  `developer_tools_probe(platform, git_executable)` decides whether a check is
  needed, its caller passes the normalized executable, and the developer
  directory the check reports must hold `usr/bin/git`. Without that, git reads
  `missing`, no version and no repository, and no git runs at all, so the
  installer dialog cannot appear on every refresh; a git resolved anywhere else
  needs no check.
- `folder_status(path)` is the one folder rule: `ok`, `missing`, `unreadable` or
  `not_directory`, from one `stat` and, for a directory that opens, one
  `scandir` handle opened and closed without reading a name. Discovery uses it
  for every configured folder and the settings save uses it for every newly
  submitted folder, so a saved folder and a listed one cannot disagree.
- Suggests the parent folders whose configuration would reveal a package target
  the configured folders do not: `folder_suggestions` in the document, one
  `{path, package_count}` entry per folder, decided by a pure rule over the
  facts the collector prepares for each package's usable target. A target
  discovery already admits, one its child admission test would reject, the
  filesystem root, and a parent that is already configured contribute nothing,
  so the rule reads no file, runs no git, and its list cannot change with git's
  availability.
- A read-only collector: networked by default through one `git ls-remote` per
  distinct remote, which transfers no objects, and, when the workspace turns
  GitHub lookup on, one optional `gh` request per GitHub repository for its pull
  requests; `--no-network` skips both.
- Keeps the last successful remote checks (the `ls-remote` answer and the pull
  requests) in the verdict cache file, and answers from them, labelled
  `recorded`, when a run is offline or a check fails; a clone's own newer
  remote-tracking ref takes precedence.
- Decides the remote copy of a package branch from the live answer, else the
  saved answer, else, with neither, the remote-tracking ref that moved last among
  the readable clones that hold it (a ref whose move time cannot be read ranks
  below the others; fetch times play no part). When no readable clone holds it
  the copy is `unknown`, never `absent`: only a live or saved answer says
  `absent`.
- Decides the remote copy's verdict, `remote_verdict` in the document, for a
  present copy whose commit no local copy of the branch has: a merged,
  same-repository pull request into the default branch whose head is that commit
  proves it merged, and otherwise the same verdict ladder runs in a clone that
  holds the commit, judged against that clone's base. So a branch that exists only
  on the remote gets a verdict. A copy whose commit no clone holds stays
  `unknown`, and two reasons belong to this verdict alone: `objects_missing` when
  no clone holds the commit, `ambiguous_tip` when it lies on the default branch's
  first-parent history, where a fast-forward merge and a branch pushed with no
  commit of its own look alike. The verdict is null exactly when there is nothing
  to decide: no `origin` remote, a copy that is not `present`, or a copy at the tip
  of a local copy of the branch.
- Concludes that every package naming a branch is finished only from a complete
  package scan; on an incomplete one a package that may share the branch is
  unseen, so no package reads `awaiting_merge`.
- Matches each specification package's `Target repo:` to the checkout at that
  path: a clone, a linked worktree (resolved to its clone), or a discovered
  checkout whose clone could not be read. Paths compare by `os.stat` identity
  (device and inode, which a symbolic link or a different letter case on a
  case-insensitive volume does not change); a filesystem that reports no inode
  compares the real path instead, with the host's case normalization. A target
  that matches nothing is classified from the filesystem
  alone as missing, not a git checkout, or outside the checkout folders, so
  git never runs in a directory discovery did not admit; with git unavailable a
  usable target is unchecked. A path inside a clone that is not its top level
  is not a git checkout. A path that cannot be examined at all (for example,
  access denied) is reported as not a git checkout, because no `.git` entry can
  be shown. A discovered checkout whose clone's path, as git lists
  it, the document cannot carry (`unrepresentable_path`) has no clone record, so
  a target at the checkout is reported as outside the checkout folders; this is
  a known limit.
- Counts each unfinished package's drift, from its declared `Baseline:` commit
  to the target clone's verdict base, with one read-only `rev-list --left-right
  --count` per distinct clone and Baseline, using the verdict settings' pinned
  git options; a sibling clone of the same remote may lend the Baseline commit
  read-only. Drift is recomputed on every run and never saved. A package target
  that is missing or not a git checkout also records `within_folders`, true when
  its path lies in or under a configured checkout folder.
- Calls `derive_attention` after the join and stores the clone availability,
  each repository's free clones and the attention list it returns.
- `nyx/checkout_join.py` holds the matching rules over plain facts and makes no
  git call and reads no file; `nyx/checkouts.py` owns every git call and
  filesystem read and builds the path identities it is given.
- Derives a collection's inputs once, through `collection_inputs`: the
  configured record's folders and GitHub switch, and the package scan of its
  specification root; a not-configured account collects no folder and scans no
  package, and an unavailable record yields none. The `nyx` command line and
  the checkouts worker both call it, so no path disagrees about what a
  collection reads.
- Never writes or locks a repository.

## Outside this boundary

- Fetching, writing or locking a repository. The board's Checkouts view renders
  the served document as it stands (the status line, the attention list, the
  groups and, for folder problems, git availability and diagnostics, a setup
  panel) and edits nothing: it shows the folders and cannot change them. It
  takes every fact and attention item from the document; the only things it
  decides itself are presentation: which repositories the quiet row collects,
  which pull-request lookup state the status line names, where each attention
  item sends focus, and which of the words it shows the search matches. The board renders the served
  document's Code Status as a card line and never derives, rescans or ages it:
  the line holds the text `nyx checkouts --json` gives from the last successful
  scan, refreshed about once a minute by `application-serving` and updated in
  place on its own without **Apply update**, searchable with the card's other
  text, and hidden while the served document's `configuration_revision` differs
  from the displayed catalog's, which adds no entry and lasts for up to about
  75 seconds after a Board settings save on the same page, or, after a
  configuration change saved elsewhere, until **Apply update** installs the new
  catalog; while no document can be served the
  issues panel says `Code Status unavailable`.

## Collaborators

- Depends on `catalog`, `protocols`, and `account-state`.
- Used by `lifecycle-control` (the `nyx` command line) and
  `application-serving` (the checkouts worker kind).
- The collector's deadline is derived from the worker's own budget; see
  [application-serving invariants](../application-serving/README.md#cross-system-invariants).
- Reuses the catalog byte bound; see
  [catalog invariants](../catalog/README.md#cross-system-invariants).
- Calls `derive_removable`; see
  [protocols invariants](../protocols/README.md#cross-system-invariants).
- Calls `code_status_text`; see
  [protocols invariants](../protocols/README.md#cross-system-invariants).

## Cross-system invariants

- `folder_status` is the single folder rule: `checkouts` reads every configured
  folder with it and an `application-serving` checkout settings save reads every
  newly submitted folder with it, so a folder the save accepts and a folder the
  document lists can never disagree. Members: `application-serving`.
