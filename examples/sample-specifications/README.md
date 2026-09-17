# Walkthrough: a trail-planning app

[Return to Nyx](../../README.md).

Imagine you are building a trail-planning app with a web interface and a route
API. The API contract is complete, so you can plan the route preview, but the API
implementation is not ready for the web feature to use. Meanwhile, an existing
navigation feature needs a fix and the route-search work is ready for review.

This small workspace shows how Nyx brings those efforts together. All names,
identifiers, and evidence values are fictional.

## Open the board

Follow the [installation instructions](../../README.md#try-the-sample), then run
these commands from the repository root. If Nyx is already running, stop it
before changing its setup.

```bash
nyx --stop
nyx --setup examples/sample-specifications --show-all-stages
nyx
```

Open **http://127.0.0.1:8765/**. You will see two project columns and five cards:

| Project | Stage | Work |
| --- | --- | --- |
| Trail_Web | Under Development | Plan the route preview |
| Trail_Web | Queue | Build the route preview |
| Trail_Web | Needs Fixes | Fix saved-route navigation |
| Trail_Web | Awaiting Retrospective | Review the route search |
| Trail_API | Done | Define the route API contract |

## Follow a dependency

Select **Plan the route preview**. Its `contract-ready` prerequisite is satisfied
by the API contract specification, so the design work has the input it needs.

Now select **Build the route preview**. It depends on a different claim from the
same specification: `implementation-ready`, which is still unsatisfied. Finishing
the contract did not finish the implementation. A specification's stage and its
individual claims describe different things.

Both web specifications belong to the **Route preview** program, which appears
in their details. Try searching for `Route preview` to focus on that work.

## Focus on unfinished work

Hide completed work by changing the setup:

```bash
nyx --stop
nyx --setup examples/sample-specifications --hide-stage Done
nyx
```

There are now four visible cards. Select **Build the route preview** again: the
API contract is still available as prerequisite context in its details, even
though its card is hidden. To restore the full board, stop Nyx and run setup
again with `--show-all-stages`.

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
