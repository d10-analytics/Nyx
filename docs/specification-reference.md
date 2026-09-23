# Specification reference

[Return to Nyx](../README.md).

For a first workspace, start with the [workspace guide](workspaces.md). This
reference describes the metadata used when you need more than a title and stage.

## Work item header

Nyx reads a work item's `spec.md` header up to the first line starting with `## `.
Put metadata in that header, one field per line.

| Field | Meaning |
| --- | --- |
| `# Title` | The first top-level heading supplies the card title. |
| `Package ID: UUID` | A stable, unique identity for the specification. |
| `Status: text` | Optional reported text retained for search; it does not change the stage or appear in ordinary item details. |
| `Target repo: path` | The final path component supplies the displayed target project. |
| `Program Membership: UUID` | Optional membership in a named group of work. |
| `Claim: name \| state [\| evidence]` | A named outcome this specification reports. |
| `Prerequisite: UUID \| claim-name` | A required outcome from another specification. |
| `Superseded By: UUID` | Optional reference to a replacement specification. |

## File syntax and displayed terms

The field spellings above are the stable `spec.md` file contract. Keep writing
`Package ID`, `Target repo`, `Claim`, and `Prerequisite` exactly as shown, even
when the browser uses friendlier labels. Nyx presents each specification as a
**work item**, its directory location as a stage, and a `Target repo` value as
**Target project** when that context is useful. The stable `Package ID` is
available under the technical disclosure rather than occupying the everyday card view.

For example, a community-event item can point to two outcomes from one permit
item by repeating the `Prerequisite` field:

```text
Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | venue-confirmed
Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | permit-approved
```

The providing item records those outcomes with separate `Claim` rows. The file
syntax remains unchanged while the selected work item shows each prerequisite
in its Dependencies disclosure. A configured-hidden provider can still appear
there as context.

Nyx also reads `Closure`, `Sanity Recommendation`, and `Human Sanity Decision`
as reported text. These fields are optional; they do not trigger actions.
Project and stage come from the work item's directory location.

The project is the first directory below the configured workspace root, and the
stage is the next direct directory containing the work item. Directory names are
literal identities: `Testing` and `testing` are different stages, and Nyx does
not title-case, normalize, or alias them. Grouping directories may appear below
the stage before the work item directory.

Nyx discovers safe direct project and stage directories even when they are not
in the familiar lifecycle table. Names beginning with `.`, `Reference`, and
`.pipeline` are reserved at their documented levels. Direct regular files such
as `.gitkeep` are ignored, so they can preserve an empty project or stage in a
versioned sample without creating a work item. Setup resolves a supplied workspace
path to its literal root before saving it, while a catalog scan invoked directly
with a linked or reparse root rejects that root. Every discovered structural
directory must be literal: Nyx rejects symlinks and Windows reparse points before
descent, including project, stage, grouping, work item, `Reference`, `Programs`,
and UUID program directories. Symlinked or reparse anchors are not read. This is
a trusted-local input contract and does not claim hostile concurrent
path-substitution containment. Case is preserved literally; case-distinct
siblings exist only where the host filesystem supports them, and Nyx does not
normalize or alias their names.

Generate fresh IDs with `python3 -c 'import uuid; print(uuid.uuid4())'`. Use each
`Package ID` once in a workspace and retain it when moving the work item. Repeat
`Claim` and `Prerequisite` rows for distinct outcomes and requirements; do not
repeat scalar fields such as `Package ID` or `Program Membership`.

## Claims and prerequisites

A claim describes a named outcome, such as `contract-ready`. Claim names use
lowercase letters, digits, and hyphens, start with a letter, and have at most
64 characters. Claim states are `satisfied`, `unsatisfied`, or `unknown`.

An outcome that is not ready can be recorded as:

```text
Claim: implementation-ready | unsatisfied
```

Use `unknown` when its state is not known. A satisfied claim requires an evidence
reference. This fictional example uses a repeated-f placeholder hash:

```text
Claim: contract-ready | satisfied | sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
```

Supported evidence formats are `sha256:` or `git-object-sha256:` followed by
64 lowercase hexadecimal characters, or `git-object-sha1:` followed by 40.
Use a reference to the actual evidence in your own workspace. Nyx reads the
reference and validates its format; it does not retrieve or verify that evidence.

A dependent specification refers to the providing work item's ID and claim name:

```text
Prerequisite: 55555555-5555-4555-8555-555555555555 | contract-ready
```

This points to the API contract in the [sample workspace](../examples/sample-specifications/README.md).
In your workspace, replace the ID and claim name with those of the outcome you need.

Progress and dependency information comes from your specification files. A card
with declared direct prerequisites shows **Dependencies satisfied**, **Waiting on
dependencies**, **Dependencies unknown**, or **Dependencies unavailable**. A
work item with no declared direct prerequisites has no dependency marker. The
indicator concerns direct prerequisites only; it reports recorded information and
does not imply implementation approval or completion. Missing, ambiguous, or
malformed information can leave a relationship unknown or unavailable; it is not
treated as satisfied. Moving a work item to `Done` does not satisfy its claims
automatically.

## Programs

A program gives related specifications a shared name. Create its descriptor at:

```text
project/Reference/Programs/UUID/program.md
```

For example, the bundled sample uses:

```text
Program ID: 99999999-9999-4999-8999-999999999999
Program Title: Route preview
```

A participating work item declares:

```text
Program Membership: 99999999-9999-4999-8999-999999999999
```

Use a fresh UUID for your own program and match it in the directory name,
descriptor, and membership fields. Membership comes from the work items; a separate
member list is not needed. The browser shows resolved program membership in card
details and includes the program title in search. Board columns remain projects.

## Diagnostics

Diagnostics identify problems reading or interpreting the workspace, such as
unreadable files, duplicate IDs, invalid fields, or unresolved references.
Inspect the affected specification and its prerequisites rather than inferring
completion from an incomplete view. A prerequisite hidden from the board by
stage policy can still be included as context in a visible dependent's details.

Discovery diagnostics also distinguish a safely identified but incomplete
project or stage from a confirmed empty one. Inspect the affected directory and
retry after restoring access; do not infer that no work items exist from an
incomplete scan. The browser retains the last valid displayed catalog when a
later refresh is malformed.
