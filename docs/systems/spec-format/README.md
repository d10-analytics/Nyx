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
- Owns the parsing of specification text in `nyx/spec_body.py`: section bodies,
  Markdown tables, stable-ID definitions and references (ranged and
  transitive), task blocks (`parse_tasks`, `Task`, `task_block`), file-boundary
  rows and paths (`boundary_rows`, `boundary_paths`), source-path and
  authority-path tokens, `strip_blanks` and `within`, with the regular
  expressions they share.
- Owns the package validator in `nyx/spec_validation.py`: the required
  sections, the sanity, closure and critique-contract rules, the verification
  authorities, task and traceability rules, the branch-header check (strict
  `not applicable — <reason>` grammar, then `branch_name_is_lexical`) and the
  lifecycle-folder rules. Its public names are `validate_spec`,
  `validate_package`, `validate_package_attachments` and `package_lifecycle`.
- Every public validator function takes the specification root explicitly and
  has no default root. `package_lifecycle`, `validate_spec` and
  `validate_package` also take `non_package_folders`, the names of top-level
  folders of the root that are not product folders (empty by default), so a
  caller supplies its own.
- `validate_package` returns the specification and lifecycle-folder errors
  only; it does not read a retrospective or merge summary for content.
- The validator reads no environment variable, never writes, and imports only
  the standard library.
- Depends only on the standard library and holds no state.

## Outside this boundary

- The catalog document, its protocol limits and its diagnostics (`catalog`).
- Which header values the catalog admits or tolerates as valid.

## Collaborators

- Used by `catalog`.

## Cross-system invariants

- `spec_header.py` and `spec_body.py` import nothing from Nyx;
  `spec_validation.py` imports the other two and reads the metadata lines only
  through `metadata_value` and `metadata_values`.
- The header byte limit, the header cut, the six header labels and the metadata
  readers are defined only in `nyx/spec_header.py`; consumers import them and
  never define a second copy. `tests/test_spec_header.py` enforces this.
