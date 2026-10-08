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
| `Status: text` | Optional reported text retained for search; it does not change the stage and is not shown on cards. |
| `Target repo: path` | The final path component supplies the displayed target project. |
| `Program Membership: UUID` | Optional membership in a named group of work. |
| `Claim: name \| state [\| evidence]` | A named outcome this specification reports. |
| `Prerequisite: UUID \| claim-name` | A required outcome from another specification. |
| `Completion Prerequisite: UUID` | A whole-item requirement satisfied only when the target's literal stage is in this workspace's explicit completed-stage policy. |
| `Superseded By: UUID` | Optional reference to a replacement specification. |

## File syntax and displayed terms

The field spellings above are the stable `spec.md` file contract. Keep writing
`Package ID`, `Target repo`, `Claim`, and `Prerequisite` exactly as shown, even
when the browser uses friendlier labels. Nyx presents each specification as a
**work item**, its directory location as a stage, and a `Target repo` value as
**Target Folder** on the card when that context is useful. Each card shows its
directory path as **Filepath**; the stable `Package ID` stays in the file.

Nyx reads a `spec.md` header as UTF-8. A single leading UTF-8 byte order mark
(BOM) is ignored; a second BOM, or a BOM anywhere else, is content. The header
limit is 65,536 bytes, counted from the first byte of the file and including
both the BOM and the line break before the cut. A header that needs more than
that limit is reported as an invalid package. Bytes after the cut are body
content and are never decoded as part of the header, so an undecodable byte
below the cut remains a header problem while the same byte in the body does not.

For example, a community-event item can point to two outcomes from one permit
item by repeating the `Prerequisite` field:

```text
Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | venue-confirmed
Prerequisite: 123e4567-e89b-42d3-a456-426614174102 | permit-approved
```

The providing item records those outcomes with separate `Claim` rows. The file
syntax remains unchanged while the board shows prerequisite and dependent links
on cards. A provider in a finished row appears when **Hide terminal rows** is
unchecked.

Other header lines of the form `Name: value`, such as `Owner Note: waiting for
venue confirmation`, are retained as reported text. The name may be bold, as in
`**Owner Note:** text` or `**Owner Note**: text`. Their values are searchable
but are not shown on cards and do not trigger actions. Names must start with a
capital letter (`A`–`Z`), be at most 64 characters long, and contain no `:`,
`*`, `` ` ``, `<`, `>`, `|`, `[`, `]`, or control characters. The field names in
the table above (`Package ID`, `Status`, `Target repo`, `Program Membership`,
`Claim`, `Prerequisite`, `Completion Prerequisite`, and `Superseded By`) never
become additional reported text, in any capitalization. Nyx ignores a line whose
name breaks these rules; it does not shorten a long name. Names are compared
case-insensitively for `A`–`Z`; the first occurrence with a nonempty value wins.
Nyx keeps at most the first 64 distinct fields in header order and shortens long
values.
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
and UUID program directories. At the workspace, project, stage, and grouping
levels, a link whose name ends in `.md` (case-sensitive) is instead treated as
a document: it is not followed and does not make discovery incomplete, so a
linked folder with such a name contributes no work items. Names that Nyx
already rejects as unsafe keep their diagnostic. A link named `spec.md` or
`program.md` where Nyx expects an anchor is treated as a nonregular anchor
rather than as a document. Symlinked or reparse anchors are not read. This is
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

For a whole-item completion requirement, use the distinct header below and no
claim name:

```text
Completion Prerequisite: 55555555-5555-4555-8555-555555555555
```

`Completion Prerequisite: UUID` is the one exact completion grammar. A one-part
`Prerequisite: UUID` is not an alternate spelling and remains invalid. Keep
`Prerequisite: UUID | claim-name` for named outcomes; the two relationships may
appear together on one dependent item. Completion never uses a magic claim name,
does not rewrite claim state, and does not verify the claim's evidence.

Progress and dependency information comes from your specification files. Board
cards show prerequisite and dependent links without a dependency status line.
Missing, ambiguous, or malformed information can leave a relationship unknown
or unavailable; it is not treated as satisfied. Moving a work item to `Done`
does not satisfy its claims automatically.

The completion policy is configured in the collapsed **Board settings** panel by
selecting literal stage names beside **Counts as finished**. It is saved per
canonical workspace root and can include temporarily absent names.
Without a configured policy, a completion dependency is **unknown** with an
actionable configuration explanation. Row order, stage spelling,
`Cancelled`, and `Archive` do not infer completion. A completed stage is a
recorded workflow assertion rather than evidence verification.

The selected finished stages also group terminal rows on the board. The **Hide
terminal rows** control starts checked, so finished rows are hidden until the
reader reveals them, and the preference belongs to the browser. It changes
presentation only: every discovered stage remains in the catalog, and unchecking
the control reveals finished rows without changing dependency results. This is
the only control that hides populated stage rows.

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
member list is not needed. A whole `program.md` descriptor obeys the same
65,536-byte limit as a `spec.md` header, counted from the first byte of the
file, and a larger descriptor is reported as an invalid package. The browser
includes the resolved program title in search. Board columns remain projects.

## Diagnostics

**Workspace issues** lists unreadable directories, unreadable or invalid
`spec.md` files, and invalid or duplicate program descriptors. A prerequisite
that cannot be resolved to exactly one work item shows on the dependent card as
**unresolved target**. Duplicate or malformed Package IDs and invalid claims or
fields are otherwise not listed; inspect the affected specification headers
rather than inferring completion from an incomplete view. Uncheck **Hide terminal
rows** to show providers in finished rows and their connections to dependent cards.

Discovery diagnostics also distinguish a safely identified but incomplete
project or stage from a confirmed empty one. Inspect the affected directory and
retry after restoring access; do not infer that no work items exist from an
incomplete scan. The browser retains the last valid displayed catalog when a
later refresh is malformed.
