# Running Nyx

[Return to Nyx](../README.md).

Linux runs Nyx as a background service driven by the installed console. Windows
and macOS run a self-contained desktop application that owns its runtime; that
application is a private internal feasibility build, not a public release, and
this guide makes no support promise beyond the hosts that were actually
exercised. The sections below cover both.

## Linux: install and try the sample

You need Python 3.12. From the repository root, these commands use the installed
console directly and require no virtual environment activation. Explore the
board after starting Nyx, before running the final stop command.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/nyx --setup examples/sample-specifications --show-all-stages
.venv/bin/nyx
.venv/bin/nyx --status
.venv/bin/nyx --stop
```

Throughout the Linux sections, bare `nyx` means `.venv/bin/nyx`. That relative
path works from the repository root. From another directory, use the absolute
path to the same installed console and quote paths containing spaces.

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
Both commands are Linux service commands; the desktop application does not
provide them.

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

## Choose board row order

When account settings are available, the toolbar includes a **Board row order**
editor. It lists the literal stage names known to the current workspace. Use
**Move up** and **Move down** to arrange them, then choose **Save**. Save writes
the order to this account's configuration and the board applies it without moving
any folders or changing work item state. The saved order is shared by this
account's browser and desktop views, survives browser reload and a supported Nyx
restart, and is kept separately for each workspace root.

**Cancel** discards the editor's unsaved moves and performs no write. **Reset**
clears the saved order for the current workspace only; the board then follows the
canonical inventory order. A hidden or absent saved stage name stays in the editor
as a retained literal, marked as unavailable, but it produces no board row. A new
eligible stage that is not yet saved follows the saved names and is appended in
canonical inventory order. If the settings service cannot load, the board remains
usable in canonical inventory order and reports the problem so you can retry; a
failed Save likewise leaves the prior saved order in place.

## Windows and macOS: the private desktop application

The desktop application is a self-contained build; it does not require users to
install Python or create a virtual environment. It is a private internal
feasibility build, not a public release, and there is no offline-install
guarantee. It has been exercised on a hosted Windows Server x64 image and on
Apple Silicon macOS; it is not a claim about every Windows or Intel Mac client.
Your operating system may ask you to trust or open the build the first time you
run it.

### Build the private artifact

From the repository root, install the pinned desktop and build extras and run
the build driver. Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install '.[desktop,build]'
.\.venv\Scripts\python.exe scripts\build_desktop.py
```

macOS (POSIX shell):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install '.[desktop,build]'
.venv/bin/python scripts/build_desktop.py
```

The driver stages `dist\desktop\Nyx\Nyx.exe` on Windows and
`dist/desktop/Nyx.app` on macOS, together with the private worker helper and the
board resources. Open the staged executable or application bundle directly; the
staged build resolves its own dependencies and does not run from the source
checkout.

### Open, change, recover, and quit

The first launch shows a workspace chooser. Select the workspace directory and
save it; Nyx starts its own runtime and displays the board in the application
window's embedded web view. There is no detached daemon behind the window and
no `nyx --setup`, `nyx --status`, or `nyx --stop` on these hosts; those options
report that the desktop application has no lifecycle commands.

- **Change workspace** stops the current work, saves the new workspace, and
  restarts. An incomplete stop keeps the previously saved workspace and shows a
  visible **Retry** rather than mixing the two workspaces.
- **Quit**, and closing the only window, request the same visible shutdown. The
  window exits only after its owned cleanup has finished.
- One application owns the account on this host. A separately launched second
  process reports that Nyx is already open and exits without changing state;
  normal operating-system activation of the running application still works.
- After an unexpected crash, reopening visibly blocks the runtime start and
  workspace changes until the former workers have terminated, then permits
  **Retry**. An incomplete recovery stays visibly blocked rather than reported
  as successful.

### Desktop limits

The application serves one local account through the fixed loopback address and
does not edit specifications. It has no automatic startup, auto-update, remote
or shared hosting, cross-host configuration synchronization, or agent
execution. Configuration and runtime state still live under `.nyx` in your home
directory.

## Read and refresh the board

Use **Find** to search visible card metadata, including work item titles, projects,
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

## Capture and review the fictional example

The checked-in images use only the complete fictional community-event fixture
listed in the [workspace guide](workspaces.md): four `spec.md` work items
(Queue/festival, Done/permit, Needs_Fixes/cleanup, and
Under_Development/event-site), two `.gitkeep` empty dimensions, and one
malformed `Reference/Programs/.../program.md` that naturally produces the
`invalid_package` workspace issue. Create that disposable workspace outside the
repository, start Nyx through its normal local server and scanner, and configure
the `Done` stage as hidden. Do not use a private workspace for a public example.

Capture the views from the running browser at a `1600 × 1200` viewport. Use a
fresh browser context so the compact preference starts checked. Before saving the
first image, use **Board row order** to move **Queue** above **Needs Fixes** and
choose **Save**; keep that saved account-local order for all three images:

1. Choose **Light**, select **Organize the neighborhood festival**, and save the
   minimal board view as `docs/images/sample-board.png`.
2. Choose **Dark** and keep **Hide empty rows and columns** checked. Focus the
   selected festival card with the keyboard and press **Enter** twice: the first
   press deselects it and the second reselects it. Then focus the
   **Dependencies** summary and press **Enter**. Save this compact view as
   `docs/images/sample-board-compact.png`. The hidden permit work item and its
   two outcomes should remain visible in the disclosure.
3. Uncheck **Hide empty rows and columns**. This redraws the details panel, so
   focus the **Dependencies** summary and press **Enter** to reopen it. Then
   focus the **Workspace issues** summary and press **Enter**. Save this
   expanded view as `docs/images/sample-board-expanded.png`.

The first image is light with the saved row order, issue summary, selected festival
card, and disclosures collapsed. The second is dark with compact view checked and
the Dependencies disclosure opened by keyboard, showing the hidden permit title
and both `venue-confirmed` and `permit-approved` outcomes. The third remains
dark, unchecks compact view, and opens both Workspace issues and Dependencies by
keyboard so the empty Planning row and Community_Resources column are visible.

Review each file directly beside the running browser in both themes. Confirm
that the selected card, hidden dependency context, multiple outcomes, empty
dimensions, workspace issue, disclosure states, labels, and controls match the
browser. Reject a capture if it contains a private path or value, was drawn by
hand, or no longer shows the named state. This procedure is intentionally a
repeatable browser session; it does not add a permanent screenshot framework.

## If something looks wrong

- **The board is empty:** check the workspace reported by `nyx --status` on
  Linux, or the workspace chosen in the desktop application, the
  [directory layout](workspaces.md), and whether the relevant stages are hidden.
  If the catalog contains admitted folders but no work items, uncheck **Hide
  empty rows and columns** to inspect confirmed-empty dimensions. An incomplete
  discovery is reported separately and must not be treated as confirmation that
  a directory is empty.
- **A card or dependency has a diagnostic:** inspect its `spec.md` header for a
  missing or duplicated ID, a malformed field, or an unavailable prerequisite.
  The [reference](specification-reference.md) explains the expected format.
- **An edit has not appeared:** request a refresh and apply any pending update.
  If the catalog cannot be refreshed, the browser reports the problem; do not
  treat the retained view as confirmation of the latest file contents.
- **Setup rejects a change:** on Linux, stop the running instance, repeat setup,
  then start it. In the desktop application, choose **Change workspace**, then
  **Retry** if the stop is incomplete.
- **The desktop application will not start:** an unexpected crash blocks the
  start until the former workers have stopped; use **Retry** once they do. If
  the operating system blocked the private build, allow it to open and try
  again.

Nyx reads specification files without modifying them. Setup and runtime commands
do write account-local configuration and runtime state. The service is intended
for one local account through its fixed loopback interface, not shared or remote hosting.
