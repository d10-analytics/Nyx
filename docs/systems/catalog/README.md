# catalog

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Scans a specification root into the canonical catalog document.
- Reads the workspace and never writes it.

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
