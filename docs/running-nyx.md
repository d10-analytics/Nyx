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
.venv/bin/nyx --setup examples/sample-specifications
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
nyx --setup path/to/specifications
nyx
```

Replace `path/to/specifications` with your workspace directory. Setup saves its
absolute location for the current account on this host. The browser runs
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

Status reports the configured workspace and runtime state. It
does not start or reconfigure Nyx. Stopping an already stopped instance is safe.
Both commands are Linux service commands; the desktop application does not
provide them.

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
canonical inventory order. An absent saved stage name stays in the editor
as a retained literal, marked as unavailable, but it produces no board row. A new
eligible stage that is not yet saved follows the saved names and is appended in
canonical inventory order. If the settings service cannot load, the board remains
usable in canonical inventory order and shows **Reload board row order** so you
can retry. A conflict or reload-needed Save requires that reload, which replaces
the unsaved draft with the current saved order. Any other failed Save leaves the
prior saved order and unsaved draft in place so you can retry.

The same collapsed **Board settings** panel also lists **Counts as finished**
beside every literal stage name. Select the stages whose names define whole-item
completion, then choose **Save** to persist the row order and completion policy
together. **Cancel** drops both kinds of unsaved change; **Reset** clears the
current root's row order and completed-stage set. Saved names that are
temporarily absent remain available in the panel as literal settings,
marked as unavailable when appropriate. If settings cannot be loaded, choose
**Reload board settings** to retry. After a successful save, Nyx refreshes the
catalog only when its configuration revision matches the saved settings before
showing changed dependency results; a conflict or failed refresh keeps the last
valid board and tells you to reload or retry.

When board settings are saved elsewhere, such as from another tab, a page picks
up the change the next time it applies a board update. Without unsaved changes
it loads the saved settings. With an unsaved draft it keeps the draft, disables
**Save**, and shows "Board settings changed elsewhere. Reload board settings to
replace this unsaved draft." Choose **Reload board settings** to replace the
draft with the saved settings; Nyx never discards the draft on its own.

**Click empty space to deselect** is a separate checkbox in Board settings.
It starts unchecked, so clicking a selected card again clears the selection.
When checked, clicking an empty cell, row label, or other empty board space
also clears it; clicking another card selects that card. The choice applies
immediately and is saved in this browser when storage is available. It remains
available if saved board settings cannot load. **Save**, **Cancel**, and
**Reset** affect the saved row settings, not this checkbox.

Selecting a stage as finished also marks its rows as terminal on the board. The
**Hide terminal rows** checkbox in the top bar starts checked, so finished rows
are hidden until you reveal them, and the choice is stored in the browser like
**Hide empty rows and columns**. This grouping is presentation only: it does not
change setup, the catalog, or which dependencies count as satisfied. Every
discovered stage remains in the catalog; uncheck **Hide terminal rows** to reveal
finished rows. This is the only control that hides populated stage rows.

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
- **Cancel** is available only while changing the workspace, not at the
  first-launch chooser. It discards the replacement choice and
  returns to the current workspace without quitting; the current board keeps
  running and nothing is saved.
- The recovery button reads **Retry** or **Close**, and is unavailable when
  there is nothing to recover. **Retry** repeats a stopped workspace change or a
  runtime start.
- **Close** finishes a blocked close or a board-display failure: it retries the
  cleanup and exits once the work has stopped.
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
directory. On Linux the desktop application refuses to start and directs you to
use the `nyx` console entry instead.

## Read and refresh the board

Use **Search** to search card metadata, including work item titles, projects,
and program names. Cards show prerequisite and dependent links; selecting a card
highlights its connections to other displayed cards. To see a provider in a
finished row, uncheck **Hide terminal rows**.
Right-click a card to copy the full directory path of its work item. Nyx shows
a brief confirmation beside the card when the copy succeeds.

Nyx checks for changed information every ten seconds. **Apply update** loads the
waiting snapshot; **Refresh view** requests an immediate check when no update is
waiting. Changes are not applied automatically while you are reading a snapshot.

An explicit completion policy affects only whole-item `Completion Prerequisite`
relationships. Moving a work item into a selected stage can satisfy that
relationship; moving it out reopens it. Hiding finished rows does not change
dependency results. No stage is completed automatically because it is called
`Done`, `Cancelled`, or `Archive`, and a completed stage is a recorded workflow
assertion rather than evidence verification.

The **Hide empty rows and columns** checkbox starts checked. It compacts only
the currently displayed catalog, and its value is persisted in browser local
storage when available. If browser storage is blocked or full, Nyx keeps the
preference in memory for the current page and safely falls back to checked on a
new page. This preference never changes setup, the workspace, or the catalog.

The **Hide terminal rows** checkbox uses the same contract. It starts checked and
hides the rows for the stages the account marks as finished, and its value is
stored in the browser the same way. Unchecking it reveals those rows without
changing setup, the catalog, or the saved completion policy.

Search filters cards and their visible dependency rails without changing the
project and stage axes. Compact view does not override **Hide terminal rows**.
Each project column reserves at most eight arrow lanes. Cross-project
relationships, and same-project relationships that get no arrow lane, are
written on their cards as visible `needs:` or `blocks:` text, including for
cards that share a Package ID. Same-project relationships drawn as arrows are
also written on their cards as screen-reader text, which search does not
remove.
Incomplete or unavailable dimensions remain visible with an
`incomplete / unavailable` notice; Nyx does not compact them as if they were
empty. If a refresh returns malformed data, Nyx reports the refresh failure and
retains the last valid displayed board and browser-local preference.

Choose **Light**, **Dark**, or **System** from the theme menu to suit your display.
A theme change follows other open tabs. The compact view (**Hide empty rows and
columns**), **Hide terminal rows**, and **Click empty space to deselect** apply
per tab until reload: another open tab keeps its current choice until it reloads.

## Capture and review the fictional example

The checked-in images use only the complete fictional community-event fixture
listed in the [workspace guide](workspaces.md): four `spec.md` work items
(Queue/festival, Done/permit, Needs_Fixes/cleanup, and
Under_Development/event-site), and two `.gitkeep` empty dimensions. Create that
disposable workspace outside the repository, configure it with
`nyx --setup <fixture-root>`, and start Nyx through
its normal local server and scanner. Do not use a private workspace for a public
example.

Use an isolated temporary account home outside the repository for every fixture
setup, start, status, and stop command and the browser session. Stop the owner's
Nyx instance first, record the SHA-256 of the owner's `.nyx/config/config.json`,
and verify that `/api/workspace` reports the fixture root before saving Board
settings. After visual acceptance, stop the isolated
instance, remove its temporary home, and confirm the owner's configuration hash
is unchanged.

Capture the views from the running browser at a `1600 × 1200` viewport. Use a
fresh browser context so the compact preference starts checked. Before saving the
first image, open **Board settings**, use **Board row order** to move **Queue**
above **Needs Fixes**, check **Counts as finished** beside `Done`, and choose
**Save**. Keep that saved account-local order and completion policy, and keep
**Hide terminal rows** checked for all three images.

Before each capture, inspect the live `/api/catalog` response: the workspace
diagnostics in `discovery_diagnostics` must be empty. Both identity and program
coverage must be complete, and `programs` must be empty. Confirm exactly the four
work items remain in the catalog and every project and stage inventory
availability is complete, including the empty `Planning` stage
and `Community_Resources` project. Confirm the festival's completion prerequisite still targets the
permit, observes `Done`, and resolves as satisfied with reason
`completion_satisfied`. In the actual browser, verify there is no workspace or
refresh issues panel (`#board-issues` must be absent, not merely collapsed or
hidden). Keep **Board settings** closed in every image.

1. Choose **Light**, select **Organize the neighborhood festival**, and save the
   minimal board view as `docs/images/sample-board.png`.
2. Choose **Dark** and keep **Hide empty rows and columns** checked. Focus the
   selected festival card with the keyboard and press **Enter** twice: the first
   press deselects it and the second reselects it. Save this compact view as
   `docs/images/sample-board-compact.png`.
3. Uncheck **Hide empty rows and columns** to reveal the empty `Planning` row
   and `Community_Resources` column. Save this
   expanded view as `docs/images/sample-board-expanded.png`.

The first image is light with the saved row order and selected festival card
showing its filepath. The second is dark with compact view checked and the same
selected card and filepath. Both omit the empty dimensions. The third remains
dark with the same selected card and filepath,
unchecks compact view, and shows the empty Planning row and Community_Resources
column. All three omit the finished Done row and permit card and have no issues
panel.

Review each file directly beside the running browser in both themes. Confirm
that the selected card, package path, empty dimensions, saved row order, labels,
controls, and absence of workspace or refresh issues match the browser.
Reject a capture if the catalog is incomplete, any workspace diagnostic or issues panel
is present, it contains a private path or value, was drawn by
hand, or no longer shows the named state. This procedure is intentionally a
repeatable browser session; it does not add a permanent screenshot framework.

## If something looks wrong

- **Nyx says port 8765 is already in use:** find the program using it with
  `ss -ltnp 'sport = :8765'` on Linux,
  `lsof -nP -iTCP:8765 -sTCP:LISTEN` on macOS, or
  `Get-NetTCPConnection -LocalPort 8765 -State Listen` in Windows PowerShell
  (`OwningProcess` is the process ID). Close that program, then run `nyx` again
  or choose **Retry**. Nyx never switches ports. On Windows, a conflict
  can sometimes appear only as `Nyx runtime could not start; retry`; an
  excluded or reserved port range can raise the same error with no owning
  program. List those ranges with
  `netsh interface ipv4 show excludedportrange protocol=tcp`.

- **The board is empty:** check the workspace reported by `nyx --status` on
  Linux, or the workspace chosen in the desktop application, the
  [directory layout](workspaces.md), and the **Hide terminal rows** setting.
  Uncheck it to reveal finished rows.
  If the catalog contains admitted folders but no work items, uncheck **Hide
  empty rows and columns** to inspect confirmed-empty dimensions. An incomplete
  discovery is reported separately and must not be treated as confirmation that
  a directory is empty.
- A dependency shows **unresolved target**, or a work item looks wrong: inspect
  the involved `spec.md` headers for a missing or duplicated `Package ID`, a
  malformed field, or a prerequisite naming a nonexistent item. The board does
  not list problems inside individual specifications. **Workspace issues**
  reports only directories, specification files, and program descriptors that
  could not be read or identified.
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
