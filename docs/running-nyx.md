# Running Nyx

[Return to Nyx](../README.md).

Nyx supports Linux, Windows, and macOS with Python 3.12. From the repository
root, install and try the sample with the commands for your host. These use the
installed console directly and require no virtual environment activation.
Explore the board after starting Nyx, before running the final stop command.

Linux or macOS (POSIX shell):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/nyx --setup examples/sample-specifications --show-all-stages
.venv/bin/nyx
.venv/bin/nyx --status
.venv/bin/nyx --stop
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\nyx.exe --setup .\examples\sample-specifications --show-all-stages
.\.venv\Scripts\nyx.exe
.\.venv\Scripts\nyx.exe --status
.\.venv\Scripts\nyx.exe --stop
```

Throughout this guide and the linked guides, bare `nyx` means `.venv/bin/nyx`
on Linux or macOS, or `.\.venv\Scripts\nyx.exe` in Windows PowerShell. These
relative paths work from the repository root. From another directory, use the
absolute path to that same installed console; quote paths containing spaces
and use PowerShell's `&` before a quoted executable path.

Nyx reads and displays specifications; it does not edit them or move them
between directories. Setup and runtime commands write account-local configuration
and runtime state. The board's compact preference is separate and belongs only
to the browser.

## Set up a workspace

```bash
nyx --setup path/to/specifications --show-all-stages
nyx
```

Replace `path/to/specifications` with your workspace directory. Setup saves its
absolute location and stage visibility for the current account on this host. The browser runs
at **http://127.0.0.1:8765/**.

Run `nyx` again to reuse an already running, ready instance. It prints the same
URL rather than starting a second instance.

## Upgrade or use another host

Configuration and runtime state live in `.nyx` inside your home directory.
This is intentionally a fresh state root: legacy Linux state is neither read nor
migrated. Nyx does not copy, remove, or fall back to those legacy locations.
Stop Nyx with your existing installation before upgrading, then rerun setup
with the new installation.

Saved workspace paths are absolute and local to each host. Rerun setup on every
host using its local workspace location, even when the specification files are
copied or synchronized between hosts.

## Check status and stop

```bash
nyx --status
nyx --stop
```

Status reports the configured workspace, hidden stages, and runtime state. It
does not start or reconfigure Nyx. Stopping an already stopped instance is safe.

## Choose visible stages

Stop Nyx before changing its workspace or stage policy:

```bash
nyx --stop
nyx --setup path/to/specifications --hide-stage Done --hide-stage Archive
nyx
```

Repeat `--hide-stage` for each stage to hide. Use the directory names listed in
the [workspace guide](workspaces.md), with the same capitalization. Any admitted
literal stage directory can be hidden by its exact name, including a custom
stage such as `Testing`.

To show every stage:

```bash
nyx --stop
nyx --setup path/to/specifications --show-all-stages
nyx
```

The visibility flags are setup options; they cannot be used alone or combined
with each other. An active instance rejects changes to its workspace or policy.
Use an explicit visibility option when setting up so the intended board is clear.

## Read and refresh the board

Use **Find** to search visible card metadata, including package titles, projects,
and program names. Select a card to inspect its recorded values, prerequisites,
and diagnostics. A hidden prerequisite can still appear in those details.

Nyx checks for changed information every ten seconds. **Apply update** loads the
waiting snapshot; **Refresh view** requests an immediate check when no update is
waiting. Changes are not applied automatically while you are reading a snapshot.

The **Hide empty rows and columns** checkbox starts checked. It compacts only
the currently displayed catalog, and its value is persisted in browser local
storage when available. If browser storage is blocked or full, Nyx keeps the
preference in memory for the current page and safely falls back to checked on a
new page. This preference never changes setup, the workspace, or the catalog.

Search filters cards and their visible dependency rails without changing the
project and stage axes. A hidden configured stage stays absent even when compact
view is unchecked. Incomplete or unavailable dimensions remain visible with an
`incomplete / unavailable` notice; Nyx does not compact them as if they were
empty. If a refresh returns malformed data, Nyx reports the refresh failure and
retains the last valid displayed board and browser-local preference.

Choose **Light**, **Dark**, or **System** from the theme menu to suit your display.

## If something looks wrong

- **The board is empty:** check the workspace reported by `nyx --status`, the
  [directory layout](workspaces.md), and whether the relevant stages are hidden.
  If the catalog contains admitted folders but no packages, uncheck **Hide
  empty rows and columns** to inspect confirmed-empty dimensions. An incomplete
  discovery is reported separately and must not be treated as confirmation that
  a directory is empty.
- **A card or dependency has a diagnostic:** inspect its `spec.md` header for a
  missing or duplicated ID, a malformed field, or an unavailable prerequisite.
  The [reference](specification-reference.md) explains the expected format.
- **An edit has not appeared:** request a refresh and apply any pending update.
  If the catalog cannot be refreshed, the browser reports the problem; do not
  treat the retained view as confirmation of the latest file contents.
- **Setup rejects a change:** stop the running instance, repeat setup, then start it.

Nyx reads specification files without modifying them. Setup and runtime commands
do write account-local configuration and runtime state. The service is intended
for one local account through its fixed loopback interface, not shared or remote hosting.
