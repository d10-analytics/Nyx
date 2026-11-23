# Your specification workspace

[Return to Nyx](../README.md).

A workspace is a directory of development specifications, grouped by project
and stage. It can live alongside your repositories or in a separate location.
Nyx discovers the literal direct-child project and stage directory names; it
does not require a fixed lifecycle vocabulary.
You can keep it in version control if that fits your workflow.

Setup resolves a supplied workspace path to its literal, readable directory
before saving it. A catalog scan invoked directly with a symlink or Windows
reparse point as its root rejects that root. The same literal-directory rule
applies to discovered projects, stages, grouping directories, work item
directories, and program directories; linked or reparse entries are skipped
with bounded discovery information. At the workspace, project, stage, and
grouping levels, a linked or reparse entry whose name ends in `.md` (matched
exactly, so `notes.MD` does not count) is instead treated as a document, like
a regular file: it is not followed and does not make discovery incomplete.
Names that Nyx already rejects as unsafe keep their diagnostic. Grouping and
work item folder names receive the same unsafe-name screening as project and
stage names, and an entry whose path inside the workspace is too long to
report safely (folder, file, or link, including an `.md` document link) is
skipped with a diagnostic naming its parent folder and makes discovery
incomplete. Because Nyx never follows links, a linked folder named like
`notes.md` is not scanned: its work items do not appear, and discovery still
reports complete. A link named `spec.md` inside a stage, grouping, or work
item directory, or `program.md` inside a program directory, is treated as that
directory's anchor rather than as a document: Nyx reports a nonregular anchor
and does not read through the link. This is a trusted-local workspace
contract, not containment against a hostile process changing a pathname while
a scan is in progress.

## A small community-event example

Nyx is useful for planning work outside software too. For example, a fictional
neighborhood festival can use this workspace layout:

```text
community-planning/
├── Community_Event/
│   ├── Done/permit/spec.md
│   ├── Needs_Fixes/cleanup/spec.md
│   ├── Planning/.gitkeep
│   ├── Queue/festival/spec.md
│   └── Under_Development/event-site/spec.md
└── Community_Resources/.gitkeep
```

The event plan can use the same metadata fields as a software specification:

```markdown
# Organize the neighborhood festival
Package ID: 123e4567-e89b-42d3-a456-426614174100
Status: planning
Completion Prerequisite: 123e4567-e89b-42d3-a456-426614174102

## Purpose

Coordinate volunteers, food, music, and a safe public event.
```

The completion row is the event plan's whole-item requirement. It names the whole
permit work item rather than a part of it, and that item sits in the `Done` stage
folder. A prerequisite is outstanding until the board reads its target as served
merged on `main`. This fictional workspace names no repository, so the event
plan's prerequisite stays outstanding. Marking `Done` as finished makes the
permit a finished row that **Hide terminal rows** hides, and it changes no
prerequisite.

The complete disposable fixture used by the checked-in captures adds these
fictional files:

```markdown
# Confirm the community festival permit
Package ID: 123e4567-e89b-42d3-a456-426614174102
Status: ready
```

```markdown
# Publish the event website
Package ID: 123e4567-e89b-42d3-a456-426614174101
Status: in-progress
```

```markdown
# Confirm the cleanup plan
Package ID: 123e4567-e89b-42d3-a456-426614174103
Status: needs-fixes
```

The empty `Planning` stage and `Community_Resources` project each contain only
`.gitkeep`. These four work items and two empty dimensions are the entire
disposable fixture. The normal scanner must retain all four work items, resolve
the permit item, and report zero workspace diagnostics with complete identity and program
coverage and complete project and stage inventory. Configure the fixture
with `nyx --setup <fixture-root>` and start the normal local server and scanner.
In **Settings**, check **Counts as finished** beside `Done` and choose
**Save**; keep **Hide terminal rows** checked for every capture, as described in
the [capture procedure](running-nyx.md#capture-and-review-the-fictional-example).
Before each capture, verify the live catalog is complete and has no workspace diagnostics
and the browser has no workspace or refresh issues panel. The expanded view
reveals the empty `Planning` row and `Community_Resources` column.
The interface calls each
specification a **work item** and displays its directory stage as a board row.
The `Package ID` and `Completion Prerequisite` spellings remain the file
contract. Use fresh UUIDs in a real workspace.

## Create your first specification

For a fictional trail-planning app, start with this layout:

```text
specifications/
└── Trail_Web/
    └── Under_Development/
        └── route-preview/
            └── spec.md
```

Each work item has its own directory containing `spec.md`. You can add grouping
directories between a stage and its work item directories as the project grows.
Nyx reads only that `spec.md`; other files and folders inside the work item
directory are ignored.

Every UUID field (`Package ID`, `Program ID`, `Program Membership`,
`Completion Prerequisite`, and `Superseded By`) takes a
lowercase version 4 UUID only; an uppercase or otherwise invalid value is not
accepted. An invalid `Package ID` makes the item lose its identity, so
dependents that name it show **unresolved target** and **Workspace issues**
lists nothing. An invalid `Completion Prerequisite` shows **unresolved target**.
An invalid `Program Membership` or `Superseded By` leaves
the item's identity intact and leaves only that relationship unresolved. An
invalid `Program ID` invalidates its program descriptor without affecting any
item's identity.

Generate a unique lowercase `Package ID` value. On Linux:

```bash
python3 -c 'import uuid; print(uuid.uuid4())'
```

On macOS, which prints uppercase by default:

```bash
uuidgen | tr '[:upper:]' '[:lower:]'
```

In Windows PowerShell:

```powershell
[guid]::NewGuid().ToString()
```

Write the following structure in `spec.md`, replacing the example ID with the
one you generated:

```markdown
# Plan the route preview
Package ID: 11111111-1111-4111-8111-111111111111

## Purpose

Help walkers compare distance and elevation before choosing a route.

## Intended behavior

Show a route summary with distance, elevation gain, and a link to full details.
```

The first heading supplies the card title. Keep metadata above the first
second-level heading (`##`); Nyx reads its header fields from that portion of
the file. Below it, write the context, requirements, decisions, and review notes
that your work needs. The board displays selected metadata, not the full document.

From the directory containing `specifications`, configure Nyx:

```bash
nyx --stop
nyx --setup specifications
nyx
```

Open **http://127.0.0.1:8765/** to find your new card under **Trail_Web** and
**Under Development**.

## Move work through development

Move the work item directory to another stage when your workflow calls for it.
For example, move `route-preview` from `Under_Development` to `Queue` when it
is ready to be scheduled. Keep its `Package ID` unchanged so dependency references
continue to identify the same work.

These familiar stage directory names are common choices, not a required set:

| Directory | Board label | Typical use |
| --- | --- | --- |
| `Under_Development` | Under Development | Develop the idea and specification. |
| `Queue` | Queue | Hold work ready to be picked up. |
| `In_Progress` | In Progress | Track implementation underway. |
| `Needs_Fixes` | Needs Fixes | Collect work requiring corrections. |
| `Awaiting_Retrospective` | Awaiting Retrospective | Review completed work and lessons. |
| `Done` | Done | Keep completed specifications. |
| `Archive` | Archive | Retain work outside the active workflow. |

These descriptions are suggested uses; Nyx displays directory placement rather
than deciding when work can move. A safe direct child such as `Testing` or
`Ready_For_Review` is also a stage, and its spelling is preserved on the board.
You do not need to create every stage in advance. Direct files such as a tracked
`.gitkeep` preserve an empty directory in version control but are not work items.
After moving files, the board shows the change at its next check, within ten
seconds.

### Mark finished work items

Finished work is an explicit, literal setting for this workspace. Open the
collapsed **Settings** panel, select **Counts as finished** beside each
stage whose literal name should count, and choose **Save**. There is no implicit
`Done` or `Archive` rule: row order, visual position, and cancellation never infer
a finished row. A cancelled or archived item therefore counts as finished only
when you deliberately select that exact literal stage name.

The setting decides finished rows, **Hide terminal rows** and the Code Status that
`nyx checkouts` reports, and it decides no prerequisite. A prerequisite names one
whole work item, and `Completion Prerequisite: UUID` is the one prerequisite row
that declares it.

Declare a prerequisite in the dependent `spec.md` with exactly one header:

```text
Completion Prerequisite: 55555555-5555-4555-8555-555555555555
```

A prerequisite is outstanding until the board reads its target as served merged
on `main`. Hiding that target's finished row changes no prerequisite. The row
records a workflow dependency, and it does not verify evidence or approve work.

The same selection also decides which packages `nyx checkouts` treats as
finished when it reads a package's Code Status (see
[Inspect checkouts](running-nyx.md#inspect-checkouts)); with no stage selected, it
treats no package as finished.

The same selection groups finished rows on the board. The **Hide terminal rows**
checkbox in the top bar starts checked, so rows for the selected stages are hidden
until you reveal them, and the choice is stored in the browser. This is
presentation only: it does not change the policy, the catalog, or dependency
results. Every discovered stage remains in the catalog. Uncheck **Hide terminal
rows** to reveal finished rows; this is the only control that hides populated
stage rows.

The selected stage names are saved per canonical workspace root and shared by
all projects in that root. Switching roots keeps each root's own setting, and a
root with no saved names makes no row finished.
Names that are temporarily absent remain retained in the **Settings** panel
so they can be used again when available. **Cancel** leaves the
saved policy unchanged; **Reset** removes the current root's completion policy.

Saved stage order and completion names stay keyed to the workspace's original
path. If you move or relink the workspace folder, Nyx does not carry them to the
new path and does not delete them; the new path starts with no saved settings.
Moving the folder back to its original path restores its order and completion
policy.

At the project level, names beginning with `.`, plus `Reference`, are reserved
and are not project columns. At the stage level, names beginning with `.`, plus
`Reference` and `.pipeline`, are reserved and are not stage rows. Nyx ignores
non-directory stage candidates and never follows symlinks. A safely identified
but unreadable project or stage remains an incomplete dimension with a bounded
diagnostic; it is not reported as an empty directory.
Names are compared literally. Case-distinct siblings are representable only on
host filesystems that support them; Nyx does not normalize case or claim that
every host can store both spellings.

## Checkout folders

Nyx records, per workspace, the folders whose git checkouts `nyx checkouts`
lists. Set them in the browser: open the **Settings** panel and its **Checkout
folders** section. Add or remove folders and the GitHub lookup, and choose
**Save checkout folders**. The section offers the parent folders that would bring
a package's `Target repo:` checkout into view and shows what the server refused
beside each folder; see [Change the checkout
folders](running-nyx.md#change-the-checkout-folders) and [Use the Repositories
view](running-nyx.md#use-the-repositories-view). It saves the account's
configuration record for the active workspace.

### Without the board

With no board running, such as for `nyx checkouts` alone, set the folders by
editing the account's
configuration record by hand: `~/.nyx/config/config.json` on Linux and macOS,
`%USERPROFILE%\.nyx\config\config.json` on Windows. A record with checkout
folders uses schema 4:

```json
{
  "schema_version": 4,
  "specification_root": "/home/me/specs",
  "checkout_settings": {
    "/home/me/specs": {
      "folders": ["/home/me/code", "/home/me/more-code"],
      "github_lookup": true
    }
  }
}
```

- The key under `checkout_settings` is the record's own `specification_root`
  value, copied exactly. On Linux `nyx --status` prints the same path after
  `Workspace:`; desktop hosts refuse `--status`, so copy the value from the
  record itself.
- `github_lookup` is the workspace's switch for the GitHub pull-request lookup
  of `nyx checkouts`. Write `true` or `false` (no request is made), without
  quotes; both keys of an entry are required, so an entry always states its own
  value. With `true`, a networked run asks GitHub about the pull requests of each
  GitHub repository among the checkouts, through an authenticated `gh` on `PATH`;
  see [Inspect checkouts](running-nyx.md#inspect-checkouts). A workspace with no
  entry has the lookup on. The **Checkout folders** section of **Settings**
  changes it with the folders; saving no folders removes the entry, so the
  lookup returns to on.
- Each entry in `folders` must be an absolute path, and the list must be sorted
  by character code, with no duplicates and at least one entry. Write each path
  already normalized in the host's own separators, with no trailing separator
  and no `.` or `..` parts: `/home/me/code`, not `/home/me/code/`, and on
  Windows `C:\\Users\\me\\code`, not `C:/Users/me/code`. Character-code order
  puts capital letters before lowercase ones and `-` before `/`. To stop
  listing checkouts for a workspace, remove its entry rather than leaving an
  empty list.
- On Windows, write each backslash as `\\` in JSON, for example
  `"C:\\Users\\me\\code"`.
- Stop Nyx first (`nyx --stop` on Linux, or quit the desktop application),
  because a running board keeps the settings it captured in memory and would
  overwrite or ignore the edit.
- On Windows and macOS, rebuild the desktop application at this version before
  editing: an older application refuses a schema 4 record.
- If an edit is malformed, the configuration becomes unavailable and
  `nyx checkouts` reports `nyx: Nyx configuration is unavailable`. Undoing the
  edit restores it.

Other keys the record already holds, such as `stage_orders`, stay as they are.

## Move files yourself

Nyx does not provide lifecycle buttons. Move a work item directory with your
editor or existing file tools while Nyx is running. The board installs the new
inventory, cards, and diagnostics together at its next check, within ten seconds,
or at once when you return to the window. A failed or malformed refresh leaves the
displayed snapshot in place.

The browser's **Hide empty rows and columns** checkbox compacts confirmed-empty
dimensions from the displayed snapshot; unchecking it restores those rows and
columns without changing the workspace or configuration. It does not override
**Hide terminal rows**: uncheck that control to show finished rows and their
cards and dependency connections.

The **Settings** panel's row-order editor is another presentation control. It saves a
root-keyed order map in the current account's configuration, so browser and
desktop views for that account share the order and a supported restart restores
it. Switching between workspace roots does not carry one root's order into
another. **Move up** and **Move down** change the draft; **Save** persists it,
**Cancel** performs no write, and **Reset** clears the current root's saved row
order and completed-stage set. Unlisted eligible stages are appended in canonical inventory order until
you save them. Saved names remain literal even when their stage is absent;
they retain their place for a later return but do not produce a row while
absent. If loading account settings fails, no saved order is available,
so the board uses canonical inventory order and reports the problem for retry.
Choose **Reload board settings** to retry. A conflict or reload-needed Save
requires that reload, which replaces the stale unsaved draft with the current
saved order. For other Save failures: If Save fails, the board keeps displaying
the prior saved order and retains the unsaved editor draft so you can retry.
Changing row order never renames or moves a project, stage, or work item
directory, and never changes completion, approval, prerequisites,
dependencies, or execution behavior.

A prerequisite names one whole work item. Use the exact header
`Completion Prerequisite: UUID` to declare it, and repeat the row for each
further work item the dependent needs. Selecting a stage as finished never
changes it.

## Add another project or a dependency

Create another project directory alongside `Trail_Web` for work in a separate
repository or product area. Nyx shows each project as a column.

When one specification needs another whole work item, declare it on the dependent
specification with a `Completion Prerequisite` row. The
[sample walkthrough](../examples/sample-specifications/README.md) shows a web
feature waiting for the API implementation while separate design work waits for
the API contract.

Dependencies and program membership are optional. Start with titles, stable IDs,
and stages, then use the [specification reference](specification-reference.md)
when those relationships help you track the work.

The [running guide](running-nyx.md) describes the fictional event captures in
light and dark themes, including compact view, finished-row filtering, keyboard
card selection, and expanded empty dimensions.
