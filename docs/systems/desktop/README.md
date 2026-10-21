# desktop

The `system.toml` beside this file is the exact membership authority; this
README is narrative only.

## Responsibilities

- The Windows and macOS desktop entry and first-launch workspace chooser; Qt
  is imported only in `main`.
- Its standalone build (`scripts/build_desktop.py`, `pysidedeploy.spec`).

## Outside this boundary

- Starting the Linux service; the desktop never does.

## Collaborators

- Depends on `lifecycle-control`, `account-state`, and `application-serving`.
- Used by `lifecycle-control` (lazily, from the command line).
- Bundles `board-web`.
- Configuration writes: see
  [account-state invariants](../account-state/README.md#cross-system-invariants).
- Shared serving and board assets: see
  [application-serving invariants](../application-serving/README.md#cross-system-invariants).
