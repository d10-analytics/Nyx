# board-web

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- The browser board's HTML, JavaScript, styles, font and its licence, and
  logo.
- Reads the catalog, workspace and settings, and changes board settings only,
  through `application-serving`'s HTTP routes.

## Outside this boundary

- Any write other than the board-settings `PUT`; editing specification files.

## Collaborators

- Served by `application-serving` and bundled by `desktop`.
- Asset serving and packaging: see
  [application-serving invariants](../application-serving/README.md#cross-system-invariants).
