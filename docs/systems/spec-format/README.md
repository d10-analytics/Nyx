# spec-format

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- Owns the reading of a specification anchor's header: the bounded header cut,
  the BOM-aware UTF-8 header text, the single-value metadata lines, and the
  package-owned relationship rows (`Package ID`, `Program Membership`,
  `Superseded By`, `Completion Prerequisite`). The retired `Prerequisite` and
  `Claim` labels are not header labels: a header line carrying one adds no
  relationship row. The catalog also keeps such a line out of the reported
  fields, so it is not searchable text.
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
  `not applicable — <reason>` grammar, then `branch_name_is_lexical`), the
  retired-label rule that rejects the retired relationship rows in a live
  package, and the lifecycle-folder rules. Its public names are `validate_spec`,
  `validate_package`, `validate_package_attachments` and `package_lifecycle`.
- `package_lifecycle`, `validate_spec` and `validate_package`, the functions
  that determine a package's stage folder, take the specification root
  (`spec_root`) explicitly and have no default root. They also take
  `non_package_folders`, the names of top-level folders of the root that are
  not product folders (empty by default), so a caller supplies its own.
  `validate_package_attachments` takes the package folder, the already-read
  specification lines and the lifecycle name, and no root.
- `validate_package` returns the specification and lifecycle-folder errors
  only. It does not validate the content of a retrospective or a merge
  summary. Of `retrospective.md` it reads only the `Status:` line: a Done
  package needs `Status: READY`.
- The validator reads no environment variable and never writes. It imports
  only the standard library and the two sibling parsers of this system.
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
- The header byte limit, the header cut, the four header labels, the retired
  relationship labels and the metadata readers are defined only in
  `nyx/spec_header.py`; consumers import them and never define a second copy.
  `tests/test_spec_header.py` enforces this.
