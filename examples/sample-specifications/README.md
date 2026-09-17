# Walkthrough: a trail-planning app

[Return to Nyx](../../README.md).

Imagine you are building a trail-planning app with a web interface and a route
API. The API contract is complete, so you can plan and verify the route preview,
but the API implementation is not ready for the web feature to use. Meanwhile,
an existing navigation feature needs a fix and the route-search work is ready
for review.

This small workspace shows how Nyx brings those efforts together. All names,
identifiers, and evidence values are fictional. `Trail_Web/Testing` is a custom
stage, `Trail_Web/Ready_For_Review` is an admitted empty stage, and the tracked
`Trail_Mobile/.gitkeep` preserves an admitted empty project without creating a
package.

## Open the board

Follow the [installation instructions](../../README.md#try-the-sample), then run
these commands from the repository root. If Nyx is already running, stop it
before changing its setup.

```bash
nyx --stop
nyx --setup examples/sample-specifications --show-all-stages
nyx
```

Open **http://127.0.0.1:8765/**. With all stages shown, the catalog contains
three projects and six package records. The package records are:

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

Select **Plan the route preview**. Its `contract-ready` prerequisite is satisfied
by the API contract specification, so the design work has the input it needs.

Now select **Build the route preview**. It depends on a different claim from the
same specification: `implementation-ready`, which is still unsatisfied. Finishing
the contract did not finish the implementation. A specification's stage and its
individual claims describe different things.

The three web specifications belong to the **Route preview** program, which
appears in their details. Try searching for `Route preview` to focus on that
work.

## Focus on unfinished work

Hide completed work by changing the setup:

```bash
nyx --stop
nyx --setup examples/sample-specifications --hide-stage Done
nyx
```

There are now five visible package records. Select **Verify the route preview**:
the API contract is a configured-hidden prerequisite, but it remains available
in the selected card's details. Select **Build the route preview** as well to
compare its unsatisfied implementation prerequisite. The API contract remains
available as prerequisite context even though its card is hidden. To restore the
full board, stop Nyx and run setup again with `--show-all-stages`.

The configured `Done` policy is separate from compact view. The checkbox can
hide confirmed-empty rows and columns, but it cannot reveal a configured-hidden
stage or its cards.

## Move a package and apply the update

Nyx reads directory placement; it does not move packages. From the repository
root, move the fictional verification package manually:

```bash
mv examples/sample-specifications/Trail_Web/Testing/verify \
   examples/sample-specifications/Trail_Web/Ready_For_Review/verify
```

Wait for Nyx's automatic update check (it runs every ten seconds). The changed
catalog is held as a pending snapshot and the button becomes **Apply update**;
click it to adopt the new stage and card location together. When no update is
pending, **Refresh view** requests an immediate check and applies that response
directly. Move the package back to `Testing`, wait for the next automatic check,
and apply it again so the checked-in sample returns to its documented starting
state:

```bash
mv examples/sample-specifications/Trail_Web/Ready_For_Review/verify \
   examples/sample-specifications/Trail_Web/Testing/verify
```

If a refresh is malformed, Nyx reports the failure and retains the last valid
board rather than partially applying the result.

## Compare compact and expanded views

With `Done` hidden and the sample package restored to `Testing`, leave **Hide
empty rows and columns** checked for the compact view. Then uncheck it to show
the admitted empty `Ready_For_Review` row and `Trail_Mobile` column alongside
the populated dimensions. Search filters cards but leaves the axes unchanged.
Select **Verify the route preview** in either state to inspect its satisfied API
prerequisite; the hidden card is not added to the board or search results.

When you finish exploring, run `nyx --stop`.

## Look at the files

The [plan](Trail_Web/Under_Development/plan/spec.md) and
[implementation](Trail_Web/Queue/delivery/spec.md) show how two specifications
can depend on different claims from the same
[API contract](Trail_API/Done/record/spec.md). Their optional
[program descriptor](Trail_Web/Reference/Programs/99999999-9999-4999-8999-999999999999/program.md)
gives the related work a shared name.

The package IDs make those links stable when a specification moves between
stages. The repeated-f hash in the API contract is a synthetic evidence reference
for this demonstration; use a reference to your actual evidence in your own work.

Keep this bundled sample as a reference and create your own workspace using the
[workspace guide](../../docs/workspaces.md). The sample directory contains only
source Markdown; screenshots and other generated files belong elsewhere.
