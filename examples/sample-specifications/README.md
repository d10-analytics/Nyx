# Walkthrough: a trail-planning app

[Return to Nyx](../../README.md).

Imagine you are building a trail-planning app with a web interface and a route
API. The API contract is complete, so you can plan and verify the route preview,
but the API implementation is not ready for the web feature to use. Meanwhile,
an existing navigation feature needs a fix and the route-search work is ready
for review.

This small workspace shows how Nyx brings those efforts together. Nyx displays
each specification as a **work item**. The work item directory and `spec.md`
remain the on-disk file contract. All names and identifiers are fictional.
`Trail_Web/Testing` is a custom stage, `Trail_Web/Ready_For_Review` is an
admitted empty stage, and the tracked `Trail_Mobile/.gitkeep` preserves an
admitted empty project without creating a work item.

## Open the board

Follow the [installation instructions](../../README.md#try-the-sample), then run
these commands from the repository root. If Nyx is already running, stop it
before changing its setup.

```bash
nyx --stop
nyx --setup examples/sample-specifications
nyx
```

Open **http://127.0.0.1:8765/**. The catalog contains three projects and seven
work items. Uncheck **Hide terminal rows** to show every populated stage,
including any stages already marked finished for this workspace. The work items
are:

| Project | Stage | Work |
| --- | --- | --- |
| Trail_API | Done | Define the route API contract |
| Trail_API | In Progress | Build the route API |
| Trail_Web | Under Development | Plan the route preview |
| Trail_Web | Queue | Build the route preview |
| Trail_Web | Needs Fixes | Fix saved-route navigation |
| Trail_Web | Awaiting Retrospective | Review the route search |
| Trail_Web | Testing | Verify the route preview |

The empty `Ready_For_Review` stage and `Trail_Mobile` project are inventory
facts. Direct `.gitkeep` files do not appear as cards. When **Hide empty rows and
columns** is checked, confirmed-empty dimensions are compacted out of the board.
Uncheck it to restore the empty `Ready_For_Review` row and `Trail_Mobile`
column. The browser preference is local to that browser and does not alter this
sample or Nyx setup.

## Follow a dependency

Select **Plan the route preview**. Its `Completion Prerequisite:` row names the
whole API contract work item, so the board draws an arrow from that card. The
prerequisite is the entire contract item, not a part of it.

**Build the route preview** names a different work item, **Build the route API**.
The contract can be agreed before any API exists, so the two dependents name two
work items rather than two parts of one.

This sample names no repository for the board to check, so the board serves no
merge record and every arrow stays outstanding. In the panel's **Needs** list
each prerequisite reads **merge state unknown**, and no dependent card shows
ready. The arrows are still drawn, because each header row names a card on the
board.

The three web work items belong to the **Route preview** program. Try searching
for `Route preview` to focus on that work.

## Focus on unfinished work

Open **Settings**, select **Counts as finished** beside `Done` only, and
choose **Save**. Check **Hide terminal rows** to hide its finished row. This
records `Done` as a finished stage for whole work items. It changes no arrow: a
target that stays unmerged is still an outstanding prerequisite.

There are now six displayed work items when search is clear. The API contract
stays in the catalog, and its two dependents still list it. Select **Plan the
route preview** or **Verify the route preview**. The panel's **Needs** list then
names the contract as **merge state unknown, in a finished row, hidden**.

Uncheck **Hide terminal rows** to restore the API contract card and the two
arrows that point to it. **Build the route preview** keeps its own arrow to
**Build the route API**, whose `In Progress` row stays on the board.

**Hide empty rows and columns** is a separate compact-view preference. It can
hide confirmed-empty dimensions, but unchecking it does not reveal finished
rows while **Hide terminal rows** remains checked.

## Move a work item

Nyx reads directory placement. It does not move work items. From the repository
root, move the fictional verification work item manually:

```bash
mv examples/sample-specifications/Trail_Web/Testing/verify \
   examples/sample-specifications/Trail_Web/Ready_For_Review/verify
```

Nyx's automatic update check runs every ten seconds, and at once when you return
to the window. The card appears in its new stage on its own. Move the work item
back to `Testing` and wait for the next check so the checked-in sample returns to
its documented starting state:

```bash
mv examples/sample-specifications/Trail_Web/Ready_For_Review/verify \
   examples/sample-specifications/Trail_Web/Testing/verify
```

If a refresh is malformed, Nyx reports the failure and retains the last valid
board rather than partially applying the result.

## Compare compact and expanded views

With `Done` marked finished, **Hide terminal rows** checked, and the sample work
item restored to `Testing`, leave **Hide empty rows and columns** checked for
the compact view. Then uncheck it to show the admitted empty `Ready_For_Review`
row and `Trail_Mobile` column alongside the populated dimensions. Search filters
cards but leaves the axes unchanged. Uncheck **Hide terminal rows** to reveal the
API contract card, then select **Verify the route preview** to highlight its
dependency connection.

When you finish exploring, run `nyx --stop`.

## Look at the files

The [plan](Trail_Web/Under_Development/plan/spec.md),
[verification](Trail_Web/Testing/verify/spec.md) and
[delivery](Trail_Web/Queue/delivery/spec.md) work items show how one header row
names a whole work item. The plan and the verification name the
[API contract](Trail_API/Done/record/spec.md), and delivery names the
[API implementation](Trail_API/In_Progress/implementation/spec.md). Their
optional [program descriptor](Trail_Web/Reference/Programs/99999999-9999-4999-8999-999999999999/program.md)
gives the related web work a shared name.

Each link is one `Completion Prerequisite:` row that names a package ID. The
board pairs it with the work item that declares that ID. The `Package ID` values
keep those links stable when a specification moves between stages.

Keep this bundled sample as a reference and create your own workspace using the
[workspace guide](../../docs/workspaces.md). The sample directory contains only
source Markdown. Screenshots and other generated files belong elsewhere.
