# board-web

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- The browser board's HTML, JavaScript, styles, font and its licence, and
  logo.
- Reads the catalog, workspace and settings, and changes board settings only,
  through `application-serving`'s HTTP routes.
- Reads the checkouts document those routes serve, and shows each card's
  `Code Status:` line: the text `nyx checkouts --json` gives, searchable with
  the card's other text and rewritten in place on each scan, without **Apply
  update**. The text is Nyx's last successful checkout scan, and Nyx rescans
  about once a minute while a board page is open.
- Shows a line only while the served document was scanned under the configuration
  the board displays, so a Board settings save on the same page hides every line
  until the next scan, for up to about 75 seconds, and a configuration change
  saved elsewhere hides them until **Apply update** installs the new catalog;
  while no document can be served it shows no
  line and adds a `Code Status unavailable` issues-panel part, and a
  configuration mismatch adds no entry.

## Outside this boundary

- Any write other than the board-settings `PUT`; editing specification files.

## Collaborators

- Served by `application-serving` and bundled by `desktop`.
- Asset serving and packaging: see
  [application-serving invariants](../application-serving/README.md#cross-system-invariants).
