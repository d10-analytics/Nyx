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
directories, and program directories; linked or reparse entries are skipped with bounded
discovery information. This is a trusted-local workspace contract, not
containment against a hostile process changing a pathname while a scan is in
progress.

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
│   ├── Reference/Programs/123e4567-e89b-42d3-a456-426614174101/program.md
│   └── Under_Development/event-site/spec.md
└── Community_Resources/.gitkeep
```

The event plan can use the same metadata fields as a software specification:

```markdown
# Organize the neighborhood festival
Package ID: 123e4567-e89b-42d3-a456-426614174100
Status: planning
Completion Prerequisite: 123e4567-e89b-42d3-a456-426614174102
Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | venue-confirmed
Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | permit-approved

## Purpose

Coordinate volunteers, food, music, and a safe public event.
```

The completion row is the event plan's whole-item requirement: it becomes
satisfied only when this workspace explicitly marks the literal `Done` stage as
finished. The two `Prerequisite` rows remain separate named outcomes with their
own recorded claim and evidence state, so the event can show both a completed
permit item and the individual outcomes it provides.

The complete disposable fixture used by the checked-in captures adds these
fictional files:

```markdown
# Confirm the community festival permit
Package ID: 123e4567-e89b-42d3-a456-426614174102
Status: ready
Claim: venue-confirmed | satisfied | sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
Claim: permit-approved | satisfied | sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
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
`.gitkeep`. To make the captured **Workspace issues** disclosure deterministic,
the fixture also contains the valid-UUID directory
`Community_Event/Reference/Programs/123e4567-e89b-42d3-a456-426614174101/`
with this intentionally malformed `program.md`:

```text
# This extra line makes the fictional descriptor invalid
```

The normal scanner reports that descriptor as `invalid_package` while retaining
the four work items and resolving the hidden permit item. Configure the fixture
with `nyx --setup <fixture-root> --hide-stage Done`; the capture procedure then
uses the normal local server and scanner route. The interface calls each
specification a **work item** and displays its directory stage as a board row;
the original `Package ID`, `Claim`, and `Prerequisite` spellings remain the
file contract. Use fresh UUIDs in a real workspace.

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

Generate a unique `Package ID` value:

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

Move the work item directory to another stage when your workflow calls for it.
For example, move `route-preview` from `Under_Development` to `Queue` when it
is ready to be scheduled. Keep its `Package ID` unchanged so dependency references
continue to identify the same work.

These familiar stage directory names are used by the bundled examples:

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
After moving files, request a refresh and apply the update.

### Define whole-item completion

Completion is an explicit, literal policy for this workspace. Open the collapsed
**Board settings** panel, select **Counts as finished** beside each stage whose
literal name should count, and choose **Save**. There is no implicit `Done` or
`Archive` rule: row order, visual position, hidden status, cancellation, and
the spelling of a stage never infer completion. A cancelled or archived item
therefore remains outside the completion policy unless you deliberately select
that exact literal stage name.

Declare the requirement in the dependent `spec.md` with exactly one whole-item
header:

```text
Completion Prerequisite: 55555555-5555-4555-8555-555555555555
```

When the target moves into a selected stage, this whole-item requirement becomes
satisfied; moving it out reopens the requirement. A missing policy leaves the
requirement **unknown** and asks you to configure completed stages. A target in a
hidden completed stage still appears as dependency context. This assertion records
workflow completion; it does not verify evidence, approve work, or satisfy a
named outcome.

The selected stage names are saved per canonical workspace root and shared by
all projects in that root. Switching roots keeps each root's own policy, and a
root with no saved names reports unknown for its simple completion dependencies.
Hidden stages and names that are temporarily absent remain retained in the Board
settings panel so they can be used again when available. **Cancel** leaves the
saved policy unchanged; **Reset** removes the current root's completion policy.

At the project level, names beginning with `.`, plus `Reference`, are reserved
and are not project columns. At the stage level, names beginning with `.`, plus
`Reference` and `.pipeline`, are reserved and are not stage rows. Nyx ignores
non-directory stage candidates and never follows symlinks. A safely identified
but unreadable project or stage remains an incomplete dimension with a bounded
diagnostic; it is not reported as an empty directory.
Names are compared literally. Case-distinct siblings are representable only on
host filesystems that support them; Nyx does not normalize case or claim that
every host can store both spellings.

## Move files yourself

Nyx does not provide lifecycle buttons. Move a work item directory with your
editor or existing file tools while Nyx is running, then choose **Refresh view**
when no update is waiting. If the refreshed catalog differs, the button becomes
**Apply update**; the new inventory, cards, and diagnostics remain pending until
you apply them together. A failed or malformed refresh leaves the displayed
snapshot in place.

Configured hidden stages and compact view are different controls. `--hide-stage`
removes a literal stage from the displayed policy and from search and rails, but
its prerequisite can remain visible in a selected card's details. The browser's
**Hide empty rows and columns** checkbox only compacts admitted, policy-eligible
dimensions from the displayed snapshot; unchecking it restores confirmed-empty
rows and columns without changing the workspace or configuration.

The **Board row order** editor is another presentation control. It saves a
root-keyed order map in the current account's configuration, so browser and
desktop views for that account share the order and a supported restart restores
it. Switching between workspace roots does not carry one root's order into
another. **Move up** and **Move down** change the draft; **Save** persists it,
**Cancel** performs no write, and **Reset** clears only the current root's saved
order. Unlisted eligible stages are appended in canonical inventory order until
you save them. Saved names remain literal even when their stage is hidden or
absent; they retain their place for a later return but do not produce a row while
hidden or absent. If loading account settings fails, no saved order is available,
so the board uses canonical inventory order and reports the problem for retry.
Choose **Reload board row order** to retry. A conflict or reload-needed Save
requires that reload, which replaces the stale unsaved draft with the current
saved order. For other Save failures: If Save fails, the board keeps displaying
the prior saved order and retains the unsaved editor draft so you can retry.
Changing row order never renames or moves a project, stage, or work item
directory, and never changes completion, approval, claims, prerequisites,
dependencies, or execution behavior.

Completion and named outcomes are separate relationships. Use the exact
`Completion Prerequisite: UUID` header for a whole-item requirement. Keep
`Prerequisite: UUID | claim-name` for an individual claim; it still depends on
that claim's recorded state and evidence even when the provider is in a selected
completed stage. A mixed dependent item may declare both forms.

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

The [running guide](running-nyx.md) describes the fictional event captures in
light and dark themes, including compact view, hidden prerequisite context, and
keyboard-opened disclosures.
