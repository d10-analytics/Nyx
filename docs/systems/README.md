# Nyx systems

Each child folder of this directory is one system: a reviewable boundary of
the code under `nyx/` and `scripts/`, plus the desktop build configuration.

## Defining a system

A system is a folder `docs/systems/<name>/` containing two files:

- `system.toml`, with `schema_version = 1`, `name` equal to the folder name,
  and `files`, a list of repository-relative paths to individual files. Globs
  are not allowed.
- `README.md`, a narrative description of what the system covers.

`system.toml` is the exact membership authority. The README is narrative only
and does not define membership. A file belongs to at most one system, and
every file under `nyx/` and `scripts/` belongs to exactly one.

```toml
schema_version = 1
name = "checkouts"
files = ["nyx/checkouts.py"]
```

## Systems

- `catalog`: scans a specification root into the canonical catalog document.
- `protocols`: the typed catalog and checkout document protocols.
- `checkouts`: read-only git state of the configured checkout folders.
- `account-state`: the per-account `~/.nyx` configuration and its admission.
- `lifecycle-control`: the Linux service lifecycle, its ownership claims, and
  the `nyx` command line.
- `application-serving`: the loopback HTTP server, catalog admission, and the
  catalog worker shared by the service and the desktop application.
- `desktop`: the Windows and macOS desktop application and its build.
- `board-web`: the browser board's HTML, JavaScript, styles, and assets.

## Reading path

Before design, implementation or review, read in this order:

1. [AGENTS.md](../../AGENTS.md), the operating contract for any agent.
2. The module docstrings of the files you change.
3. The README of each system whose `system.toml` lists those files, including
   its `## Cross-system invariants` section where present.
4. [Repository-wide rules](#repository-wide-rules) below.
5. For user-visible behavior, the user documents listed in the
   [README](../../README.md#documentation).

## Where documentation goes

- A rule about one module (how to call it, what it must never do) goes in that
  module's docstring.
- A rule about one system goes in that system's README.
- An invariant whose members span systems goes in the owner system's README
  under `## Cross-system invariants`. The owner is the system holding the
  shared value or the single authority, otherwise the producer side. Every
  other member system gets a one-line pointer under Collaborators.
- A rule with no owner system goes in
  [Repository-wide rules](#repository-wide-rules).
- User-visible behavior goes in the user documents.
- Do not restate what code or tests show, file inventories, counts the code
  determines, or development history. Name a collaborator only for a direct
  import or a documented runtime handoff that the code performs today.

## Repository-wide rules

These rules have no owner system.

- **Published documents are executable.** `tests/test_documented_install.py`
  runs the commands in `README.md` and `docs/running-nyx.md` and pins required
  wording in the user documents; change them together.
- **Specification format.** A change to the specification format the catalog
  reads updates `docs/specification-reference.md`;
  `tests/test_documented_install.py` pins its header limits to `nyx/catalog.py`.
- **CI contract.** `tests/test_workflow.py` holds the commands, extras,
  environment, and time limits of `.github/workflows/tests.yml` as a closed
  contract; change the workflow and that test together.

## Changing membership

When you add, move, split, or delete a file under `nyx/` or `scripts/`, update
the owning `system.toml` and that system's README in the same change. A new
system needs its own folder with both files. `tests/test_system_definitions.py`
enforces membership, names, and README presence.
