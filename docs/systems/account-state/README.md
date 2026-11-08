# account-state

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- The per-account `~/.nyx` tree, the configuration schema, and admission.
- Atomic replacement of the configuration file.
- `save_configuration_owned` and `validate_configuration_candidate` take
  `checkout_folders` and `github_lookup` for the active workspace, omitted by
  default so a save of any other setting carries the stored entry forward. A
  non-empty folder list requires a boolean switch and stores canonical absolute
  folders in sorted order with no duplicates; an empty list removes the active
  workspace's entry, and every other workspace's entry survives either save.

## Outside this boundary

- Lifecycle ownership (`lifecycle-control`).

## Collaborators

- Depends on `lifecycle-control` lazily, to delegate setup.
- Used by `catalog`, `checkouts`, `lifecycle-control`, `application-serving`,
  and `desktop`.

## Cross-system invariants

- Only `state` writes the configuration file, by atomic replacement. A caller
  without the lifecycle claims saves through `runtime.setup`; a caller holding
  them uses `save_configuration_owned`. Members: `lifecycle-control`,
  `application-serving`, `desktop`.
