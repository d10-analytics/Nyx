# Your specification workspace

[Return to Nyx](../README.md).

A workspace is a directory of development specifications, grouped by project
and stage. It can live alongside your repositories or in a separate location.
You can keep it in version control if that fits your workflow.

## Create your first specification

For a fictional trail-planning app, start with this layout:

```text
specifications/
└── Trail_Web/
    └── Under_Development/
        └── route-preview/
            └── spec.md
```

Each work item has its own directory containing `spec.md`. Nyx calls that
directory a **package**. You can add grouping directories between a stage and
its packages as the project grows.

Generate a unique package ID:

```bash
python3 -c 'import uuid; print(uuid.uuid4())'
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
nyx --setup specifications --show-all-stages
nyx
```

Open **http://127.0.0.1:8765/** to find your new card under **Trail_Web** and
**Under Development**.

## Move work through development

Move the package directory to another stage when your workflow calls for it.
For example, move `route-preview` from `Under_Development` to `Queue` when it
is ready to be scheduled. Keep its package ID unchanged so dependency references
continue to identify the same work.

Nyx recognizes these stage directory names:

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
than deciding when work can move. You do not need to create every stage in advance.
After moving files, refresh the board and apply the update.

## Add another project or a dependency

Create another project directory alongside `Trail_Web` for work in a separate
repository or product area. Nyx shows each project as a column.

When one specification needs an outcome from another, record that outcome as a
**claim** on the providing specification and a **prerequisite** on the dependent
specification. The [sample walkthrough](../examples/sample-specifications/README.md)
shows a web feature waiting for an API implementation while design work can
already proceed from the API contract.

Dependencies and program membership are optional. Start with titles, stable IDs,
and stages, then use the [specification reference](specification-reference.md)
when those relationships help you track the work.
