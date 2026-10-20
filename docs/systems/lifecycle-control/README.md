# lifecycle-control

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- The per-user Linux service lifecycle: `setup`, `start`, `stop`.
- Authenticated local control and the native claim primitive.
- The `nyx` command line.

## Outside this boundary

- Serving HTTP and building servers (`application-serving`), scanning a
  catalog, and writing the configuration file, which `setup` persists
  through `account-state`.

## Collaborators

- Depends on `account-state`, `protocols`, `application-serving`,
  `checkouts`, and `desktop` (lazily, from the command line).
- Used by `desktop`, by `application-serving` (the worker takes the claim),
  and lazily by `account-state`.
- Configuration writes: see
  [account-state invariants](../account-state/README.md#cross-system-invariants).
- Shared serving: see
  [application-serving invariants](../application-serving/README.md#cross-system-invariants).
