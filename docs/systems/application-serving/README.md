# application-serving

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- `ApplicationRuntime`: shared HTTP, catalog admission, and worker lifecycle;
  the loopback-only server, the catalog it fetches through its
  `CatalogWorkerManager`, and the desktop worker entry, which runs either
  worker kind.
- `CatalogWorkerManager` serves two worker kinds through one reservation,
  spawn, reader, collect and finalizer path. A catalog fetch scans the
  specification root on a 5-second budget and publishes the `producer_*`
  categories; a checkouts fetch collects the configured checkout folders once,
  locally on 30 seconds or networked on 60, and publishes the `checkouts_*`
  categories. A fetch names its kind, so a catalog worker's command line and
  categories are unchanged, and the manager never stores a kind. Neither the
  service nor the desktop application calls `fetch_checkouts`; both fetch only
  the catalog.
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
- A checkouts worker stops the programs it started, and the programs those
  started, when it ends. On Windows the worker owns a kill-on-close job object,
  joined before its first program, which ends them however the worker ends. On
  POSIX it leads its own session, so one group holds its programs: the manager
  sends that group `SIGTERM` on a timeout and on cancellation while the worker
  is unreaped, the worker sends the same group `SIGTERM` after its document is
  out, and a worker that loses its owner sends the group `SIGKILL`. The POSIX
  stop has limits: a program that ignores `SIGTERM` survives a timeout, a
  cancellation and the end sweep; a program that moves to a session or process
  group of its own is outside the group; the manager signals no group once it
  has reaped the worker; and a worker the operating system ends outright, such
  as by another process's `SIGKILL`, the out-of-memory killer or a native crash,
  runs no sweep, so its programs run until they exit on their own. A worker
  that does not lead its own session — one started by hand or by a test —
  signals no group and keeps the plain exit status, so it never stops its
  starter. A catalog worker starts no program and keeps its unchanged spawn and
  its process-only termination. Members: `checkouts`.
- A `board-web` file is served only if `_STATIC` lists it and ships in the
  wheel only if `pyproject.toml` package-data matches it; the desktop build
  bundles the whole directory. Members: `board-web`, `desktop`.
