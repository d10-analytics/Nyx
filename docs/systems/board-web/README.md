# board-web

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- The browser board's HTML, JavaScript, styles, font and its licence, and
  logo.
- Reads the catalog, workspace and settings, and changes board settings and
  the active workspace's checkout folders and GitHub lookup, through
  `application-serving`'s HTTP routes: the board-settings `PUT` and the
  checkout settings `PUT`.
- Reads the checkouts document those routes serve, and shows each card's
  `Code Status:` line: the text `nyx checkouts --json` gives, searchable with
  the card's other text and rewritten in place on each scan, without **Apply
  update**. The text is Nyx's last successful checkout scan, and Nyx rescans
  about once a minute while a board page is open.
- Shows a line only while the served document was scanned under the configuration
  the board displays, so a Board settings save on the same page hides every line
  until the next scan, for up to about 75 seconds, and a configuration change
  saved elsewhere hides them until **Apply update** installs the new catalog;
  a checkout folder save on the same page hides them only until its own
  checkouts read answers with a scan of the new folders, and the page installs
  the new catalog itself (with a checkouts read already in flight at the save,
  it sends no read of its own and the lines follow at the next tick); while no document can be served it shows no
  line and adds a `Code Status unavailable` issues-panel part, and a
  configuration mismatch adds no entry.

- Shows the Checkouts view beside the board, chosen with a header switch whose
  choice and **Hide quiet checkouts** are browser-local preferences. The view
  renders the served checkouts document: a status line with **Check remotes
  now** (a request for a remote check through `application-serving`) and the
  **Checkout folders** editor, whose save is the view's other write (the
  checkout settings `PUT`), a **Needs attention** list whose items focus the
  branch, clone, worktree or **Work item branches** row they name (a branch item
  opens the clone's **Details** first) or select a work item's card, one group
  per repository with openable clone **Details**, and a quiet row. The shared
  search keeps a repository when its names, clone and worktree paths, branch
  names, work item paths and Code Status text, the availability, HEAD and tree
  words of its clone rows, the HEAD words of its linked worktrees, or the status
  words of an unreadable clone or worktree match; no other column is searched,
  and the quiet row is searched by the repositories it holds with the same text:
  it stays while one matches, listing and counting only those, and is hidden when
  none do.
  Every enumerated code the view shows and the sentence built around it come
  from one words block, so an unknown code renders raw; served failure
  categories are shown as their raw `checkouts_*` code. The view itself decides
  which repositories are quiet, which pull-request lookup state the status line
  names (the most serious one any repository reached), and composes counts with
  their plural nouns, the cleanup count sentence and fetch ages (`min`, `h`,
  `d`) outside the words block. The view is drawn only while it is shown, and once on
  switching to it with the latest outcome, so a scan that settles while the board
  is shown does not touch the board; a refresh replaces only the blocks that
  changed, so what the reader opened and focused stays. A panel
  replaces the groups when the server serves no checkout state, a collection
  failed, or no folders are configured. The **Checkout folders** editor, shown
  while the view has a scan or a failed collection, loads and saves the
  checkout settings (folders, GitHub lookup and revision) with the server's
  answer for each outcome, offers the document's `folder_suggestions`, shows the
  folders the server refused beside them and shares one configuration revision
  with the Board settings editor. A successful save installs a fresh catalog and
  asks for a checkouts read at once, unless one is already in flight, when the
  next tick shows the new folders; the view changes no repository.

## Outside this boundary

- Any write other than the board-settings `PUT`, the checkout settings `PUT` and
  the remote-check request; editing specification files.

## Collaborators

- Served by `application-serving` and bundled by `desktop`.
- Asset serving and packaging: see
  [application-serving invariants](../application-serving/README.md#cross-system-invariants).
