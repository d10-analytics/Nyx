# protocols

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Typed validation of catalog and checkout documents.
- The canonical digest helper and the sort keys.
- The schema constants and field limits that `catalog` and `checkouts` import.
- Imports nothing internal.

## Outside this boundary

- Producing either document (`catalog`, `checkouts`).

## Collaborators

- Used by `catalog`, `checkouts`, `lifecycle-control`, and
  `application-serving`.
