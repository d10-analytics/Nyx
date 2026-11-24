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
  categories; a checkouts fetch collects the configured checkout folders once
  on a 60-second budget, asking each remote as `nyx checkouts` does by default,
  and publishes the `checkouts_*` categories. A fetch names its kind, so a catalog worker's command line and
  categories are unchanged, and the manager never stores a kind. Both the
  service and the desktop application call `fetch_checkouts` through the
  checkout serving below.
- The Linux service and the desktop application serve checkout state through
  `ApplicationRuntime`: `GET` and `HEAD /api/checkouts` answer with its
  `get_checkouts()` envelope, `{"checkouts": <document>, "remote_checks": ...}`.
  The standalone catalog server offers no checkout route.
- Both hosts read and save the active workspace's checkout folder settings
  through `ApplicationRuntime`: `GET`/`HEAD /api/checkout-settings` answers
  exactly `{folders, github_lookup, revision}` from the captured configuration,
  and `PUT` takes exactly `{revision, folders, github_lookup}`, applies the
  collector's one folder rule to every folder the workspace's saved entry does
  not already hold, answers 400 `invalid_checkout_folders` with every problem's
  index and reason, and otherwise answers exactly `{folders, github_lookup,
  revision, outcome}` with the board settings save's outcomes and status
  mapping. A workspace with no saved entry reads `folders: []` and the default lookup
  (`state.DEFAULT_CHECKOUT_GITHUB_LOOKUP`, on); an empty folder list removes the
  entry and answers the same. Both routes share the board settings admission counter
  and operation lock, they answer 503 `settings_unavailable` while admission is
  closed, and the standalone catalog server answers 404.
- A served document is refreshed by a background run only after
  `REFRESH_INTERVAL` (180 s) has passed since the start of the last run. Every
  run collects the configured folders and checks their remotes network. Nothing
  runs while no request asks for checkout data. One serial
  run queue runs one collection at a time with at most one waiting, and a run
  that publishes replaces the served document only if it started at the current
  change count. `mark_checkouts_changed()` increments that count and starts
  nothing. Both settings saves call it after the replacement, so the next
  request waits for a collection of the new settings, outside that interval. A
  run that fails with no current document holds further requests for
  `min(REFRESH_INTERVAL, 60)` seconds from its start.
- Board settings and checkout settings are saved through one private
  compare-and-save core that runs, in order, the revision compare, the caller's
  content check, the replacement, and the commit-verification reconciliation
  with the caller's own holds-the-request test, all inside the settings
  operation lock. Each save supplies its result shaper, so `/api/settings` keeps
  its `{order, completed, revision, outcome}` body byte-identical while the
  checkout save answers its own keys on every outcome.
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
- The checkout folder rule a settings save applies: see
  [checkouts invariants](../checkouts/README.md#cross-system-invariants).

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
