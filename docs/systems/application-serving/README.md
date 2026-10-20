# application-serving

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- `ApplicationRuntime`: shared HTTP, catalog admission, and worker lifecycle;
  the loopback-only server, the catalog worker, and the desktop worker entry.
- Board settings are read and saved through `save_configuration_owned` after
  the revision check; `/api/workspace` is read-only.

## Outside this boundary

- The specification scanning rules (`catalog`), writing the configuration file
  (`account-state`), and the Linux service lifecycle `setup`, `start`, `stop`
  (`lifecycle-control`).

## Collaborators

- Depends on `account-state`, `protocols`, `catalog`, and `lifecycle-control`
  (the claim primitive).
- Used by `lifecycle-control` and `desktop`. Serves `board-web`.
- Byte bound: see [catalog invariants](../catalog/README.md#cross-system-invariants);
  configuration writes: see
  [account-state invariants](../account-state/README.md#cross-system-invariants).

## Cross-system invariants

- The Linux service and the desktop both serve through `ApplicationRuntime`,
  each with its own `CatalogWorkerManager`; servers are built only in
  `nyx/server.py`. Members: `lifecycle-control`, `desktop`.
- A `board-web` file is served only if `_STATIC` lists it and ships in the
  wheel only if `pyproject.toml` package-data matches it; the desktop build
  bundles the whole directory. Members: `board-web`, `desktop`.
