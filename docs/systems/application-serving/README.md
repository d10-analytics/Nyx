# application-serving

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- `ApplicationRuntime`: shared HTTP, catalog admission, and worker lifecycle;
  the loopback-only server, the catalog and checkouts workers, and the desktop
  worker entry.
- `CatalogWorkerManager` serves two worker kinds through one reservation,
  spawn, reader, collect and finalizer path. A catalog fetch scans the
  specification root on a 5-second budget and publishes the `producer_*`
  categories; a checkouts fetch collects the configured checkout folders once,
  locally on 30 seconds or networked on 60, and publishes the `checkouts_*`
  categories. A fetch names its kind, so a catalog worker's command line and
  categories are unchanged, and the manager never stores a kind.
- Board settings are read from the held configuration, saved through
  `save_configuration_owned` after the revision check; `/api/workspace` is read-only.

## Outside this boundary

- The specification scanning rules (`catalog`), the checkout collection rules
  (`checkouts`), writing the configuration file (`account-state`), and the
  Linux service lifecycle `setup`, `start`, `stop` (`lifecycle-control`).

## Collaborators

- Depends on `account-state`, `protocols`, `catalog`, `checkouts`, and
  `lifecycle-control` (the claim primitive).
- Used by `lifecycle-control` and `desktop`. Serves `board-web`.
- Byte bound: see [catalog invariants](../catalog/README.md#cross-system-invariants);
  configuration writes: see
  [account-state invariants](../account-state/README.md#cross-system-invariants).

## Cross-system invariants

- The Linux service and the desktop both serve through `ApplicationRuntime`,
  each with its own `CatalogWorkerManager`; servers are built only in
  `nyx/server.py`. Members: `lifecycle-control`, `desktop`.
- Every Python child Nyx starts from a source run takes its environment from
  `worker.source_child_environment()`, so it imports `nyx` from its starter's
  package directory, and a new spawn site calls the same function. Members:
  `lifecycle-control`, `desktop`.
- A checkouts worker hands its collector a deadline 1.5 s below its own
  budget, so a collection that cannot finish inside its calls returns a
  document whose affected values read `timed_out` or `deadline` rather than a
  manager timeout. Members: `checkouts`.
- A checkouts worker ends with every program it started, and with the programs
  those started. On POSIX it leads its own session, so one group holds exactly
  its programs; the manager signals that group on a timeout and on
  cancellation, the worker sweeps the same group after its document is out, and
  a worker that loses its owner force-kills it. A worker that does not lead its
  own session — one started by hand or by a test — signals no group and keeps
  the plain exit status, so it never stops its starter. On Windows the worker
  owns a kill-on-close job object instead, which ends its programs however the
  worker ends. A catalog worker starts no program and keeps its unchanged spawn
  and its process-only termination. Members: `checkouts`.
- A `board-web` file is served only if `_STATIC` lists it and ships in the
  wheel only if `pyproject.toml` package-data matches it; the desktop build
  bundles the whole directory. Members: `board-web`, `desktop`.
