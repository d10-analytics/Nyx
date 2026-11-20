# catalog

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Scans a specification root into the canonical catalog document.
- Reads the workspace and never writes it.
- Reports, from the same scan, the package heads `checkouts` reads. Each head
  carries the package's `Target repo:`, optional `Branch:` and optional
  `Baseline:`, plus its finished stage and superseded flag. The Baseline is a
  declared, invalid or absent full commit ID. The heads stay out of the catalog
  document.
- Exposes `scan_packages`, the one scan the catalog renders from, as frozen
  records: each listed package's path, raw stage folder name, read diagnostic,
  relationship (including `completion` rows), candidate IDs and package ID,
  plus whether identity coverage is complete. A record holds no file contents:
  the catalog reads only a bounded prefix of each specification, so a consumer
  that needs the text reads the file at the record's path. It adds no directory
  walk and changes neither the catalog document nor the package heads.

## Outside this boundary

- HTTP serving and worker process control (`application-serving`).

## Collaborators

- Depends on `spec-format` (the header cut, header reader and parser, metadata
  readers, and the branch-name lexical rule), `protocols` (schema constants, field limits, and text helpers) and
  `account-state` (completed-stage validation).
- Used by `checkouts` and `application-serving`.

## Cross-system invariants

- `MAX_OUTPUT_BYTES` is the single byte bound for a rendered catalog, a
  rendered checkout document, and the worker's stdout. `checkouts` and
  `application-serving` reuse it and never define their own.
