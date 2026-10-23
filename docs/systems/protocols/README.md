# protocols

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Typed validation of catalog and checkout documents.
- The canonical digest helper and the sort keys.
- The schema constants that `catalog` and `checkouts` import, and the field
  limits `catalog` imports.
- Imports nothing internal.

## Outside this boundary

- Producing either document (`catalog`, `checkouts`).

## Collaborators

- Used by `catalog`, `checkouts`, `lifecycle-control`, and
  `application-serving`.

## Cross-system invariants

- `derive_removable` is the single derivation of every Removable verdict and
  count of a checkout clone. `checkouts` calls it when it builds the document and
  `parse_checkouts` recomputes it from the document's own facts, so a document
  whose verdicts disagree with its facts is refused. No other module computes a
  blocker.
