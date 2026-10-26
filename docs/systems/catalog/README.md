# catalog

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Scans a specification root into the canonical catalog document.
- Reads the workspace and never writes it.
- Reports, from the same scan, the package heads `checkouts` reads (each
  package's `Target repo:`, optional `Branch:`, finished stage and
  superseded flag), without adding them to the catalog document.

## Outside this boundary

- HTTP serving and worker process control (`application-serving`).

## Collaborators

- Depends on `protocols` (schema constants, field limits, and text helpers) and
  `account-state` (completed-stage validation).
- Used by `checkouts` and `application-serving`.

## Cross-system invariants

- `MAX_OUTPUT_BYTES` is the single byte bound for a rendered catalog, a
  rendered checkout document, and the worker's stdout. `checkouts` and
  `application-serving` reuse it and never define their own.
