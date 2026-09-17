# Running Nyx

[Return to Nyx](../README.md).

Nyx requires Linux and Python 3.12 or newer. The
[root guide](../README.md#try-the-sample) covers installation from a checkout.
Activate the virtual environment where you installed Nyx before using its commands.

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
location and stage visibility for the current Linux account. The browser runs
at **http://127.0.0.1:8765/**.

Run `nyx` again to reuse an already running, ready instance. It prints the same
URL rather than starting a second instance.

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
for one Linux account through its loopback interface, not shared or remote hosting.
