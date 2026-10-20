# protocols

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Typed validation of catalog and checkout documents.
- The canonical digest helper and the sort keys.
- Imports nothing internal, so every system may depend on it.
- Validates what other systems produce; it produces nothing itself.

## Outside this boundary

- Producing either document (`catalog`, `checkouts`).

## Collaborators

- Used by `catalog`, `checkouts`, `lifecycle-control`, and
  `application-serving`.
