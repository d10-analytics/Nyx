# spec-format

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Owns the reading of a specification anchor's header: the bounded header cut,
  the BOM-aware UTF-8 header text, the single-value metadata lines, and the
  package-owned relationship rows (`Package ID`, `Program Membership`,
  `Superseded By`, `Prerequisite`, `Completion Prerequisite`, `Claim`).
- Owns the lexical rules of a git branch name. Callers add their own bounds and
  normalisation before calling it.
- Depends only on the standard library and holds no state.

## Outside this boundary

- The catalog document, its protocol limits and its diagnostics (`catalog`).
- Which header values a caller accepts as valid.

## Collaborators

- Used by `catalog`.

## Cross-system invariants

- The header byte limit, the header cut, the six header labels and the metadata
  readers are defined only in `nyx/spec_header.py`; consumers import them and
  never define a second copy. `tests/test_spec_header.py` enforces this.
