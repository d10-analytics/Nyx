# Walkthrough: a trail-planning app

[Return to Nyx](../../README.md).

Imagine you are building a trail-planning app with a web interface and a route
API. The API contract is complete, so you can plan and verify the route preview,
but the API implementation is not ready for the web feature to use. Meanwhile,
an existing navigation feature needs a fix and the route-search work is ready
for review.

This small workspace shows how Nyx brings those efforts together. Nyx displays
each specification as a **work item**; the work item directory and `spec.md`
remain the on-disk file contract. All names,
identifiers, and evidence values are fictional. `Trail_Web/Testing` is a custom
stage, `Trail_Web/Ready_For_Review` is an admitted empty stage, and the tracked
`Trail_Mobile/.gitkeep` preserves an admitted empty project without creating a
work item.

## Open the board

Follow the [installation instructions](../../README.md#try-the-sample), then run
these commands from the repository root. If Nyx is already running, stop it
before changing its setup.

```bash
nyx --stop
nyx --setup examples/sample-specifications
nyx
```

Open **http://127.0.0.1:8765/**. The catalog contains three projects and six work
items. Uncheck **Hide terminal rows** to show every populated stage, including
any stages already marked finished for this workspace. The work items are:

| Project | Stage | Work |
| --- | --- | --- |
| Trail_Web | Under Development | Plan the route preview |
| Trail_Web | Queue | Build the route preview |
| Trail_Web | Needs Fixes | Fix saved-route navigation |
| Trail_Web | Awaiting Retrospective | Review the route search |
| Trail_Web | Testing | Verify the route preview |
| Trail_API | Done | Define the route API contract |

The empty `Ready_For_Review` stage and `Trail_Mobile` project are inventory
facts; direct `.gitkeep` files do not appear as cards. When **Hide empty rows and
columns** is checked, confirmed-empty dimensions are compacted out of the board.
Uncheck it to restore the empty `Ready_For_Review` row and `Trail_Mobile`
column. The browser preference is local to that browser and does not alter this
sample or Nyx setup.

## Follow a dependency

Select **Plan the route preview** to highlight its connection to the API contract.
In the specification files linked below, its `contract-ready` prerequisite is
satisfied by the API contract specification, so the design work has the input it
needs.

Now compare **Build the route preview** in the files. It depends on a different
claim from the same specification: `implementation-ready`, which is still
unsatisfied. Finishing the contract did not finish the implementation. A
specification's stage and its
individual claims describe different things.

The three web specifications belong to the **Route preview** program.
Try searching for `Route preview` to focus on that work.

## Focus on unfinished work

Open **Board settings**, select **Counts as finished** beside `Done` only, and
choose **Save**. Check **Hide terminal rows** to hide its finished row. This
records `Done` as finished for whole-item completion requirements; it does not
change the API contract's individual claims.

There are now five displayed work items when search is clear. The API contract
remains in the catalog and its claim still satisfies **Verify the route
preview**, while its implementation claim remains unsatisfied for **Build the
route preview**. Uncheck **Hide terminal rows** to restore the API contract card
and its dependency connections. Select either dependent card to highlight those
connections; inspect the linked specification files below for claim state and
evidence.

**Hide empty rows and columns** is a separate compact-view preference. It can
hide confirmed-empty dimensions, but unchecking it does not reveal finished
rows while **Hide terminal rows** remains checked.

## Move a work item and apply the update

Nyx reads directory placement; it does not move work items. From the repository
root, move the fictional verification work item manually:

```bash
mv examples/sample-specifications/Trail_Web/Testing/verify \
   examples/sample-specifications/Trail_Web/Ready_For_Review/verify
```

Wait for Nyx's automatic update check (it runs every ten seconds). The changed
catalog is held as a pending snapshot and the button becomes **Apply update**;
click it to adopt the new stage and card location together. When no update is
pending, **Refresh view** requests an immediate check and applies that response
directly. Move the work item back to `Testing`, wait for the next automatic check,
and apply it again so the checked-in sample returns to its documented starting
state:

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
row and `Trail_Mobile` column alongside
the populated dimensions. Search filters cards but leaves the axes unchanged.
Uncheck **Hide terminal rows** to reveal the API contract card, then select
**Verify the route preview** to highlight its dependency connection.

When you finish exploring, run `nyx --stop`.

## Look at the files

The [plan](Trail_Web/Under_Development/plan/spec.md) and
[implementation](Trail_Web/Queue/delivery/spec.md) show how two specifications
can depend on different claims from the same
[API contract](Trail_API/Done/record/spec.md). Their optional
[program descriptor](Trail_Web/Reference/Programs/99999999-9999-4999-8999-999999999999/program.md)
gives the related work a shared name.

The `Package ID` values make those links stable when a specification moves between
stages. The repeated-f hash in the API contract is a synthetic evidence reference
for this demonstration; use a reference to your actual evidence in your own work.

For a non-programming example using the same `Claim` and `Prerequisite` file
syntax, see the fictional community-event item in the [workspace guide](../../docs/workspaces.md).

Keep this bundled sample as a reference and create your own workspace using the
[workspace guide](../../docs/workspaces.md). The sample directory contains only
source Markdown; screenshots and other generated files belong elsewhere.
