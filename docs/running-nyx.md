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

`nyx --stop` has five seconds in total: that time covers waiting for any other
Nyx command to finish, sending the stop, and waiting for the running instance
to finish cleanup. If the time runs out once Nyx has started sending the stop,
it prints `nyx: Nyx shutdown timed out; ownership was retained and the outcome
is unknown; check with nyx --status` and exits 1. The instance kept ownership
of the runtime and may still finish stopping, so the outcome is unknown rather
than failed; `nyx --status` is the authority, and you can rerun `nyx --stop`
safely.

If the time runs out before Nyx starts sending the stop, the instance was never
asked to stop: it keeps running and keeps ownership of the runtime. The command
exits 1 with a message for the step it was on. Waiting for another Nyx command
prints `nyx: another Nyx operation is in progress`; running out just after that
wait prints `nyx: control deadline expired`; running out while preparing the
Nyx state directories prints `nyx: Nyx shutdown timed out; ownership was
retained`. Check with `nyx --status`, then rerun `nyx --stop` once the other
command has finished.

### Recover an unreadable runtime record

`nyx --status` reads only, so it is the signal before recovery: a runtime record
that is unreadable or malformed and has no owning instance reports
`Runtime: unknown` with the `runtime state unavailable` diagnostic. While no
instance owns the runtime, `nyx --stop` removes that record exactly as it
removes a valid stale record and prints the ordinary `stopped` result; there is
no separate recovery message. With no owning instance, starting with `nyx` still
refuses while the record cannot be read, and its message names the recovery step
and ends `; run 'nyx --stop' to recover`. Recovery requires that no instance owns
the runtime. For an unreadable record, a live owner still refuses both start and
stop and keeps the record in place. Removal is permanent, because Nyx cannot
distinguish a genuinely damaged record from a temporarily unreadable one.

## Inspect checkouts

```text
nyx checkouts
nyx checkouts --json
nyx checkouts --no-network
```

`nyx checkouts` lists the git checkouts found in the checkout folders configured
for the current workspace: one line per clone and one indented line per linked
worktree, in the same order as the document. `--json` prints the full document
instead, and `--no-network` skips every remote check (see below). It works on Linux, Windows and macOS from a Python installation of Nyx;
the packaged desktop application has no `nyx` command. It never starts, stops or
inspects the Nyx service, never launches the desktop application, and cannot be
combined with `--setup`, `--stop` or `--status`; `--no-network` is accepted only
with `checkouts`.

Before the checkouts, a few lines report problems with the setup itself.
`git missing` means no `git` was found on `PATH`, and `git too_old <version>`
means git is older than 2.36 (without a version when git's answer could not be
read); either way no checkouts are listed. `folder <path> <status>` names a
configured folder that is `missing`, `unreadable` or `not_directory`.
`diagnostic <code> <path>` reports something left out of the list:
`not_a_repository` for a directory that has a `.git` entry but is not the top of
its own repository, and `unrepresentable_path` for a checkout whose path cannot
be shown safely, such as one containing a control character, where the path is
the folder or clone it belongs under, or `null` for a configured folder itself.
`checkout_cache_unreadable` and `checkout_cache_write_failed` report the verdict
cache described below, with the cache file's path: the first means the file was
present but could not be used and was treated as empty, the second means it
could not be updated. Neither changes any verdict or any other part of the
document.

Each clone or worktree line starts with the checkout's path, quoted as a JSON
string, and then gives, separated by two spaces, its branch as `branch <name>`,
`detached <sha>`, or `unborn <name>` for a branch with no commits yet; the
`staged`, `unstaged`, `untracked` and `conflicted` counts; and any `operations`
in progress such as `merge`. A clone line shows its main worktree's branch and
counts, or `main <status>` when that worktree could not be read; a bare
repository's line has neither. A clone line with an `origin` remote then adds,
separated by two spaces and in this order, three more elements.

- `remote live`, `remote off` or `remote failed <reason>`. `live` means Nyx
  asked `origin` and got an answer; `off` means `--no-network` was given;
  `failed` means the check did not succeed, and the reason is one of
  `unreachable`, `not_found`, `authentication_failed`, `timed_out` or `failed`.
  The reason is only a category: no URL, user name, password or error text is
  ever printed.
- `branches <basis> local-only <n>`, followed by `unknown <m>` when a
  comparison could not be made. `<basis>` is `live` when the remote check was
  live and `cached` otherwise, including for a clone with no local branches.
  `local-only <n>` counts local branches with no same-name branch on `origin`.
- The default branch, in one of three forms. With a live check:
  `default <name> live <relation> cached <relation> tracking live <relation>
  fetched <time>`, comparing the local default branch with the remote's, then
  with this clone's remote-tracking copy, and that copy with the remote's. Without
  one: `default <name> cached <relation> fetched <time>`. A clone that has never
  fetched shows `never fetched` in place of `fetched <time>`. A clone whose
  remote has no default branch (an empty remote, or one with a detached `HEAD`)
  shows `no live default`, and one whose default is not known without asking the
  remote shows `no cached default`.

Whether each local branch is already merged into the remote's default branch
is a verdict, and appears only in the `--json` document: the plain lines above
are unchanged and show no verdicts. In each clone, `verdict_base` names the
default branch the verdicts are judged against, its commit, and its `basis`.
The branch is the remote's default branch, or, when the remote did not answer,
the name the clone's own remote-tracking copy points to, or the name every clone
of the remote agrees on. The `live` basis is the remote's current commit for
that name, taken from the clone itself or borrowed read-only from a sibling
clone that has it (named in `objects_from`). The `cached` basis, "base stale",
is the clone's own remote-tracking copy of that branch, which can be older than
the remote until you fetch; a branch shown as merged against a stale base was
merged into what the clone last saw, which the remote may since have moved
past. A clone with no `origin` remote or no default branch name has no base. So
does a clone that has a default branch name but neither the remote's current
commit for it (in the clone or borrowed from a sibling) nor its own
remote-tracking copy of that branch. Every verdict in a clone with no base is
`unknown` with the reason `no_base`. A clone git could not read or that has too
many branches, described below, has no branches, no worktrees and no base, so it
has no verdicts at all. In every other clone, every local branch except the base branch
itself, and every worktree on a detached `HEAD`, gets a verdict: `state` is
`merged`, `not_merged` or `unknown`.

A `merged` verdict names the first rung of this ladder that proved it, tried in
this order. `ancestor`: the branch's commit is already in the base's history, so
nothing on the branch is missing from the base. `patch_equivalent`: the branch
has no merge commits and every one of its own commits has a counterpart in the
base with the same change (the same patch identity, whitespace included), as
after a rebase or a cherry-pick. `squash_equivalent`: the branch's whole change
since it left the base has the same patch identity as one commit on the base, as
after a squash merge. Both patch rungs also require that merging the branch into
the base would change nothing, so a change that only looks the same, indented
differently or made at another place in a repeated block, never reads as merged.
Nyx runs that merge in a scratch repository that borrows the clone's objects and
changes nothing in the clone. A branch whose lines the base rewrote after
landing them, or one sharing no history with the base, fails that check and
reads as `not_merged`. `tree_equal`: the branch's file contents are identical to the content of
some commit on the base. `not_merged` has the rung `none`: every rung that could
run found nothing. An `unknown` verdict has no rung and one of six reasons.
`no_base` is described above. `incomplete_clone` means the clone is shallow or
partial, so only the `ancestor` rung runs and the others are skipped rather
than fetching missing files. `git_too_old` means git is older than 2.39: the two
patch rungs, `patch_equivalent` and `squash_equivalent`, need 2.39 for exact patch
identities, the merge check and telling two different changes to the same binary
file apart, so they are skipped, and
`ancestor` and `tree_equal` still run. `history_limit` means the branch or the
comparison was too large to check within the fixed bounds. `deadline` means one
git call reached its 10-second cap or the 60 seconds, less the half second kept
for the cache write, ran out, and `failed` means a git call failed. A shallow or
partial clone shows `incomplete_clone` in preference to `git_too_old`.

Verdicts that took git a long time to compute are remembered in
`~/.nyx/cache/checkouts.json`, so a later run that cannot decide a branch in
time can still answer from them. The cache is used only when `~/.nyx` already exists; Nyx never creates that
directory for it, and it is created inside `~/.nyx` as `cache/` when first
needed. It stores only verdicts decided in full by git 2.39 or later in a clone
that is neither shallow nor partial, and only for the rungs other than
`ancestor`, together with the `not_merged` answer. A remembered answer is used
only in place of an `unknown` verdict that this run could not decide for lack of
time, a failed call, a size bound or a clone limit. It never replaces a verdict the run decides itself, so a warm
run differs from a cold run only where the cold run says `unknown`: the
warm run may give the remembered verdict instead, whatever the reason
(`incomplete_clone`, `git_too_old`, `history_limit`, `deadline` or `failed`).
`history_limit` depends on the other branches in the clone, which is why a
remembered answer may stand in for it. A warm run therefore does the same git
work as a cold run, spending the time it has left re-checking. It runs the size checks, the merge bases and the shared
comparison for every remaining branch first, then each branch's own checks, its
`tree_equal` rung and the `not_merged` decision finish before the next branch
starts. Both steps take the branches in one order: those with no remembered
answer first, then those with the oldest remembered answer, then the newest. A
verdict the run decides always wins over a remembered one, and a remembered
answer stands in only where that re-check does not finish. Every branch whose
check the run finishes, `ancestor` aside, is remembered again with the time of
this run, so the oldest answers are re-checked first next time and a clone is
covered a few branches at a time over successive runs, but only within these
limits. A budget that ends inside the shared steps (the size checks, merge
bases and shared comparison) decides no new branch, although remembered answers
still stand in. Branches that can never be remembered, because one of their
steps fails or reaches the 10-second cap or because they are `history_limit`,
are worked first on every run. The 8,192 remembered answers are shared by all
clones, and any beyond them are dropped and recomputed. A run that re-checks
nothing leaves the file
untouched. Only a shallow or partial clone, where no check beyond `ancestor` can
run, looks up first. Under git older than 2.39, the lookup comes after the size
check and the `tree_equal` rung, so a remembered answer never replaces a
`tree_equal` verdict that the cold run decides. A repository's diff attributes
(`.gitattributes` in the tree and `info/attributes`) can make Nyx compare a
file by its blob identity instead of by hunks, which can only make matching
stricter. A `not_merged` they caused is remembered like any other, but it
stands in only for a later `unknown`, so once the attributes are gone a run
that decides the branch reports its own verdict. A file that is
missing, damaged, too large or from another version is ignored. Deleting it only
costs time, because the verdicts are recomputed.

The relation is `equal`, `ahead`, `behind`, `diverged`, `differs` (different,
but not counted, as in a shallow clone), `absent` (one of the two branches does
not exist) or `unknown`. `cached` only ever labels a basis; the time a clone
last fetched is always `fetched <time>`. A clone without an `origin` remote shows
`no origin remote` and no branch or default elements. A checkout git refused or
could not read shows its path and its status instead: `unsafe_ownership` when
git refuses it for ownership, `timed_out` when git did not finish in time,
`too_many_branches` when the clone has more than 100 local branches (the default
branch included), and otherwise `failed`. A `too_many_branches` clone is not
examined, so it shows no facts and no verdicts; it stays listed with the other
clones of its remote but lends them no objects and takes no part in choosing
their default branch name. The plain lines are otherwise unchanged.

By default `nyx checkouts` is networked: it asks each distinct `origin` remote
once, so clones that share a remote share one answer, and compares against what
the remote has now. It stops waiting after 60 seconds in total; a remote it
could not ask or finish asking falls back to cached values and shows
`remote failed <reason>`. When a clone lacks a commit that it needs for a count,
the count may be borrowed read-only from a sibling clone of the same remote
that has the commit; the JSON document names that sibling. Nyx's own SSH runs
in batch mode, so it never stops to ask for a password or passphrase, unless you
configured an SSH command yourself. An SSH command or credential helper that you
configured runs exactly as configured, so whether it asks for input depends on
that configuration. When a remote check runs out of time, Nyx stops git itself
but not the programs git started to reach the remote, such as your SSH command,
a credential helper or git's HTTP helper. Such a program can keep running after
`nyx checkouts` has printed its result and exited, and a prompt it opened can
stay on the terminal until that program ends or you stop it. `--no-network`
makes no network connection at all: `remote off`, and every value comes from
the remote-tracking branches the clone already has, so it can be older than the
remote until you fetch.

The command is read-only: it changes no repository, takes no git lock, writes
no alternates file and never fetches, so asking a remote leaves every clone as
it was. Its only write is the verdict cache file, which changes no repository. It exits 0 whenever it can print a document, including when git is
missing, a folder is unavailable or a remote check failed; it exits 1 with one
line only when the configuration is unavailable or the document exceeds 2 MiB.
With no folders configured it says `no checkout folders are configured` and
exits 0.

Checkout folders are set per workspace, in the account's configuration record;
see [Your specification workspace](workspaces.md#checkout-folders) for the
exact edit. A settings editor arrives with the Checkouts view; until then the
record is edited by hand.

## Choose board row order

When account settings are available, the toolbar includes a collapsed **Board settings**
panel with a row-order editor. It lists the literal stage names known to the current workspace. Use
**Move up** and **Move down** to arrange them, then choose **Save**. Save writes
the order to this account's configuration and the board applies it without moving
any folders or changing work item state. The saved order is shared by this
account's browser and desktop views, survives browser reload and a supported Nyx
restart, and is kept separately for each workspace root.

**Cancel** discards the unsaved moves and unsaved completion choices and performs
no write. **Reset** clears the current root's row order and completed-stage set;
the board then follows the canonical inventory order. An absent saved stage name stays in the editor
as a retained literal, marked as unavailable, but it produces no board row. A new
eligible stage that is not yet saved follows the saved names and is appended in
canonical inventory order. If the settings service cannot load, the board remains
usable in canonical inventory order and shows **Reload board settings** so you
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
the build driver.

On Windows the build needs Visual Studio 2022 or later, or its Build Tools, with the MSVC
C++ x64 build tools (the **Desktop development with C++** workload). The block
loads that toolchain's environment with `vswhere` and `vcvars64.bat` before the
build: Nuitka compiles with it, and the deployment tool uses its `dumpbin` to
discover the Qt modules the application depends on. If the toolchain or
`dumpbin` is missing, the block stops with an error instead of building.
Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install '.[desktop,build]'
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw 'vswhere.exe was not found; install Visual Studio 2022 or later, or its Build Tools' }
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'no Visual Studio installation has the MSVC C++ x64 build tools' }
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
cmd /c "`"$vcvars`" && set" | ForEach-Object {
  if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process') }
}
if ($LASTEXITCODE -ne 0) { throw "vcvars64.bat failed with exit code $LASTEXITCODE" }
if (-not (Get-Command dumpbin.exe -ErrorAction SilentlyContinue)) { throw 'dumpbin.exe is not on PATH after loading the build tools' }
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
- If the board cannot be displayed, the window stays open with the message
  "Nyx board could not be displayed; choose Close", the recovery button reads
  **Close**, and the board's work keeps running until you choose. If the board
  then loads successfully again, the window leaves the failure screen and shows
  its usual controls.
- **Close** finishes a blocked close or a board-display failure: it stops the
  work and closes the window once the work has stopped. If cleanup is still
  blocked, the window stays open, reports that Nyx workers are still stopping,
  and keeps **Close** available to retry.
- **Quit**, and closing the only window, request the same visible shutdown. The
  window exits only after its owned cleanup has finished. After a board-display
  failure they stop the board's work the same way **Close** does.
- **Close**, **Quit**, closing the window, **Save workspace**, and **Retry**
  return at once and keep the window responsive while Nyx's work stops or
  starts. The window shows what it is waiting for ("Closing Nyx; waiting for
  its work to stop", "Changing workspace; restarting Nyx work", or "Retrying;
  waiting for Nyx work") and disables the other actions until that finishes.
  The window closes once cleanup finishes; if cleanup is incomplete, it stays
  open, reports that Nyx workers are still stopping, and keeps **Close**
  available to retry.
- **Quit** stays available while Nyx is working. During a workspace change it
  takes effect as soon as it can: before the new workspace is saved, the change
  is abandoned and the previous workspace stays saved; after it is saved, the
  new workspace stays saved and its work is stopped before the window closes.
- One application owns the account on this host. A separately launched second
  process reports that Nyx is already open and exits without changing state;
  normal operating-system activation of the running application still works.
  The packaged application shows that message, or a startup failure such as
  `Nyx is unavailable`, in a Nyx message box and exits when the box is
  dismissed. A source run prints the message to standard error instead.
- After an unexpected crash, reopening visibly blocks the runtime start and
  workspace changes until the former workers have terminated, then permits
  **Retry**. An incomplete recovery stays visibly blocked rather than reported
  as successful.

### Desktop limits

The application serves the board on the fixed loopback address without caller
authentication, so other local accounts on the same host can read the catalog and
workspace path and change board settings. It does not edit specifications. It has no automatic startup, auto-update, remote
or shared hosting, cross-host configuration synchronization, or agent
execution. Configuration and runtime state still live under `.nyx` in your home
directory. On Linux the desktop application refuses to start and directs you to
use the `nyx` console entry instead.

## Maintainer: record cloud-placeholder behavior

Hosted CI cannot provide OneDrive or Dropbox placeholders, so a maintainer runs
this procedure once on a Windows host with both providers enabled and keeps the
filled record as the evidence. It changes no Nyx setting and no specification
content; use a disposable test folder inside each provider's sync folder.

Nyx treats an entry as a link when `os.lstat` reports a symbolic link or the
`FILE_ATTRIBUTE_REPARSE_POINT` bit (`0x400`) in `st_file_attributes`. A cloud
placeholder can carry that bit, so the record decides whether Nyx hides or
refuses placeholder content.

**Setup (once per provider).** Create `<sync folder>\nyx-probe\specs` holding
one repository folder, one stage folder inside it, and one package folder
inside that containing a file named exactly `spec.md`
(`specs\<repository>\<stage>\<package>\spec.md`; a card exists only for such
a file). Also create an empty `<sync folder>\nyx-probe\empty` folder. Wait until the
provider reports the files as synced.

**States (repeat for each provider, OneDrive then Dropbox).** Set the state on
the `specs` folder, the repository folder and `spec.md`, then record the table before
each Nyx observation:

| State | How to reach it |
| --- | --- |
| Online-only | OneDrive: **Free up space**. Dropbox: **Make online-only** (Smart Sync). |
| Pinned | OneDrive: **Always keep on this device**. Dropbox: **Make available offline**. |
| Hydrated | Open the file so the content is downloaded, without pinning (OneDrive **Free up space** and Dropbox **Make online-only** both undone). |

**Fields to record (Python, for each of the workspace root `specs`, the
repository folder and `spec.md` itself, per provider and state).** Run `os.lstat(path)` and record
exactly these values:

| Field | Record |
| --- | --- |
| `st_file_attributes` | Value in hexadecimal, plus whether bit `0x400` (reparse point) is set |
| `st_reparse_tag` | Value in hexadecimal, or `absent` if the attribute does not exist |
| `stat.S_ISDIR` / `stat.S_ISREG` | `True` or `False` for the root and entry |
| `stat.S_ISLNK` | `True` or `False` |
| Other attribute bits | Whether `0x1000` (offline), `0x400000` (recall on data access) and `0x40000` (recall on open) are set |

**Nyx observations (same provider and state).** Save the root as the
workspace and open the board, then record for each row below what Nyx shows.
The code expects the following when the reparse bit is set. Save accepts the
root, but the catalog scan refuses it, so the board shows `producer_failed`
instead of cards. A reparse repository or stage folder is skipped silently if
its name ends in `.md`; any other one is reported as an incomplete-discovery
issue. A reparse `spec.md` still produces a card, carrying a nonregular-anchor
diagnostic. The record states whether each state matches those expectations.

| Observation | Record |
| --- | --- |
| Save with `specs` as the workspace root | Accepted or refused, and any message shown (expected: accepted, even for a reparse root) |
| Board after Save | Cards listed, or the `producer_failed` state (expected for a reparse root) |
| Cards listed | Card count against the number of specifications that exist |
| Workspace or refresh issues panel | Present or absent, and the issue codes and paths shown |
| Repository folder entry | Listed, skipped silently, or reported as an issue |
| `spec.md` entry | Card present or absent, and the diagnostic shown (expected for a reparse file: card with a nonregular anchor) |
| Reading a card's specification header | Content shown, or the unreadable or nonregular anchor message |
| Open a card, then refresh | Whether either action downloads the file (provider status changes to hydrated) |
| Empty `empty` folder as a control | Accepted with zero cards and no issue panel |

Close the record with one line per provider and state naming the observed
behavior as `matches the code expectation` or `differs`, quoting the differing
values. A difference is a finding to file, not a documentation change.

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
first image, open **Board settings**, use the row-order editor to move **Queue**
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
- A dependency shows **unresolved target**, or a work item looks wrong: check
  **Workspace issues** first for an unreadable directory or specification file
  that made identity coverage incomplete. If it is empty, check that the
  prerequisite UUID and the target's `Package ID` are lowercase version 4 and
  unique, because an invalid or duplicate ID produces no Workspace-issues entry,
  then inspect the involved `spec.md` headers for a malformed field or a
  prerequisite naming a nonexistent item. Nyx keeps its conservative
  all-or-nothing resolution, so one unreadable entry makes every resolved
  relationship show **unresolved target**. Descriptor problems affect programs
  only and do not make identity coverage incomplete. The board does not list
  problems inside individual specifications; **Workspace issues** reports only
  directories, specification files, and program descriptors that could not be
  read or identified.
  The [reference](specification-reference.md) explains the expected format.
- **An edit has not appeared:** request a refresh and apply any pending update.
  If the catalog cannot be refreshed, the browser reports the problem; do not
  treat the retained view as confirmation of the latest file contents.
- **A refresh reports `producer_output_too_large`:** Nyx did not produce the
  catalog. Usually the workspace's catalog is larger than the 2 MiB catalog
  limit; reduce the number of work items and folders in the workspace, then
  refresh. The browser keeps any board it already shows. The same report also
  appears when the catalog worker writes too much diagnostic output or, on
  Windows, aborts, so if a small workspace keeps reporting it, the cause is a
  worker failure rather than the workspace's size.
- **`nyx --status` prints `Configuration: unavailable`:** Nyx cannot read the
  account configuration, and `nyx --setup` does not repair an existing
  unreadable record. Run `nyx --stop`, then either restore
  `~/.nyx/config/config.json` from a backup or move it aside and run
  `nyx --setup <workspace>`, which discards the saved board settings and
  checkout folders.
- **Setup rejects a change:** on Linux, stop the running instance, repeat setup,
  then start it. In the desktop application, choose **Change workspace**, then
  **Retry** if the stop is incomplete.
- **The desktop application will not start:** an unexpected crash blocks the
  start until the former workers have stopped; use **Retry** once they do. If
  the operating system blocked the private build, allow it to open and try
  again.

Nyx reads specification files without modifying them. Setup and runtime commands
do write account-local configuration and runtime state. The service is intended
for use on a fixed loopback interface without caller authentication, so other local
accounts on the same host can read the catalog and workspace path and change
board settings; shared or remote hosting is not supported.
