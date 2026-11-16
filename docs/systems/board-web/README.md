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
- Shows each card's title, folder name and tags, and draws relationships as
  arrows only: a card prints only a relationship that can never be an arrow (an
  unresolved prerequisite, or any relationship of a card that shares its
  Package ID), and carries the rest as screen-reader text. Each column plans at most eight arrow
  lanes; a relationship left without one is drawn while either of its cards is
  selected, and the cards at the other end of the selected card's relationships
  are marked.
- Installs a changed catalog itself at the next ten-second check, or at once
  when the page becomes visible or the window regains focus, with no button and
  no pending state. It keeps the reader's place across an install: the selected
  card and its panel, the position of the focused or selected card when it keeps
  its row and column, the window scroll, the search and the Board settings draft,
  and it marks the new or changed cards for a few seconds. Keyboard focus on a
  card that still exists stays with it; focus inside the work item panel is kept
  by the panel's own rewrite on every cause (an install, a checkout scan, a
  selection), not by the board redraw: a focused relationship link or **All
  fields** summary stays focused, else focus returns to the panel title.
- Shows the work item panel beside the board, filled by a left or right click
  on a card (a right click also copies the directory path and never clears the
  selection): under the title the package path on one line, then the default
  fields (Status, Program, and Target Folder only when it differs from the
  project, each only when declared), the relationships as links that select the
  card they name, and, from the served checkout document, a Code section of
  Code Status, the target repo only when it is not matched to a clone or
  worktree, the branch, a prerequisites row with the pill and its
  per-prerequisite lines only when the pill is not green, the attention items
  that name the work item, the baseline line with its words, and where it
  applies the other checkouts. Everything else sits in one closed **All fields
  (N)** disclosure after the Code section: every reported field, the Package ID
  and the other Code rows (remote copy, verdict, pull requests, Baseline drift,
  the prerequisite lines under a green pill and the baseline line's legend),
  not drawn when it would hold no row. Its open state is a page variable, read
  from the live element when the panel is rewritten and kept across rewrites
  and selections for the life of the page; it is not stored. The Code section
  and its rows are absent whenever the card's Code Status tag is hidden. The
  panel is written only when its markup changes, opening or closing the
  disclosure never rewrites it, and whether the panel is open is a
  browser-local preference.
- Reads the checkouts document those routes serve, and shows each card's
  Code Status tag, its prerequisites pill (the five wordings the document carries,
  green, red or grey by state alone, and the check mark of the baseline line only
  while the pill is green or absent), one tag per attention item naming the card (all but an
  awaiting merge, which the Code Status states, and a moved base, which the baseline
  line draws) and a baseline line of three points, the Baseline, the checkout
  and main: the text `nyx checkouts --json` gives and the line's words, searchable
  with the card's other text and rewritten in place on each scan. The text is Nyx's last successful checkout scan, and Nyx rescans
  about once a minute while a board page is open.
- Shows a line only while the served document was scanned under the configuration
  the board displays, so a Board settings save on the same page hides every line
  until the next scan, for up to about 75 seconds, and a configuration change
  saved elsewhere hides them until the next check installs the new catalog;
  a checkout folder save on the same page hides them only until its own
  checkouts read answers with a scan of the new folders, and the page installs
  the new catalog itself (with a checkouts read already in flight at the save,
  it sends no read of its own and the lines follow at the next tick); while no document can be served it shows no
  line and adds a `Code Status unavailable` issues-panel part, and a
  configuration mismatch adds no entry.

- Shows the Repositories view beside the board, chosen with a header switch whose
  choice and **Hide quiet repositories** are browser-local preferences. The view
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
  none do. A finished work item with no Code Status is not shown: it adds no
  **Work item branches** row, Package or Details entry, or search text, and
  never keeps a repository out of the quiet row.
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
