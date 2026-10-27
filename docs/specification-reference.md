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
| `Target repo: path` | The final path component supplies the displayed target project. `nyx checkouts` uses the whole absolute path. |
| `Branch: name` | Optional. The git branch the work is done on, or a value starting with `not applicable` (then a reason) for work that has no branch. `nyx checkouts` reads it, and its value also stays searchable reported text. |
| `Program Membership: UUID` | Optional membership in a named group of work. |
| `Claim: name \| state [\| evidence]` | A named outcome this specification reports. |
| `Prerequisite: UUID \| claim-name` | A required outcome from another specification. |
| `Completion Prerequisite: UUID` | A whole-item requirement satisfied only when the target's literal stage is in this workspace's explicit completed-stage policy. |
| `Superseded By: UUID` | Optional reference to a replacement specification. |

## Branch and target repository

`nyx checkouts` joins a package to the git checkouts on this machine through two
header fields. It uses `Target repo` whole, as the absolute path of a clone or of
a linked worktree of a clone; the browser still shows only its final path
component. A value that is not an absolute path, such as a relative path or the
text `TBD`, matches no checkout. `Branch` is optional and names the package's branch exactly as git spells it.
A value is accepted as a branch name when it passes these checks, which are a
subset of git's own rules: it has no space, no control character, none of
`~ ^ : ? * [ \`, backtick, `<` or `>`, no `..`, `@{` or `//`, does not start
with `-` or `/` or end with `/` or `.`, has no component that starts with `.` or
ends with `.lock`, and is no longer than 1,024 UTF-16 code units. A value
starting with `not applicable`, in any ASCII letter case, says the package has no
branch. Any other value, such as `spec/fictional-project (proposed)`, is not a
branch name and matches nothing. For `nyx checkouts`, a bold label or one pair of
backticks around the value is tolerated for both fields; the browser's display of
`Target repo` is unchanged and keeps a backtick. The core labels themselves stay plain, as
described below. How a branch
becomes a Code Status is described under
[Inspect checkouts](running-nyx.md#inspect-checkouts).

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
content and are never decoded as part of the header, so an undecodable byte in
the header before the cut still makes the item an invalid package while the
same byte after the cut does not.

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
become additional reported text, in any capitalization. `Branch` is not in that
list: although it has a row in the table, its value also stays searchable
reported text, retained under the same rules as any other reported field.
Nyx ignores a line whose
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
already rejects as unsafe keep their diagnostic. Grouping and work item folder
names receive the same unsafe-name screening as project and stage names, and an
entry whose path inside the workspace is too long to report safely (folder,
file, or link, including an `.md` document link) is skipped with a diagnostic
naming its parent folder and makes discovery incomplete. A link named `spec.md`
or `program.md` where Nyx expects an anchor is treated as a nonregular anchor
rather than as a document. Symlinked or reparse anchors are not read. This is
a trusted-local input contract and does not claim hostile concurrent
path-substitution containment. Case is preserved literally; case-distinct
siblings exist only where the host filesystem supports them, and Nyx does not
normalize or alias their names.

Bold names are documented for reported-text fields only. The core field labels
in the table above are written plain, exactly as shown.

Every UUID field (`Package ID`, `Program ID`, `Program Membership`,
`Prerequisite`, `Completion Prerequisite`, and `Superseded By`) takes a
lowercase version 4 UUID only; an uppercase or otherwise invalid value is not
accepted. The effect depends on the field. An invalid `Package ID` makes the item
lose its identity, so its dependents show **unresolved target** and **Workspace
issues** lists nothing. An invalid `Prerequisite` or `Completion Prerequisite`
shows **unresolved target**. An invalid `Program Membership` or `Superseded By`
leaves the item's identity intact and leaves only that relationship unresolved.
An invalid `Program ID` makes its program descriptor invalid, as described under
Programs, without affecting any item's identity.

Generate fresh lowercase IDs with `python3 -c 'import uuid; print(uuid.uuid4())'`
on Linux, `uuidgen | tr '[:upper:]' '[:lower:]'` on macOS (`uuidgen` prints
uppercase), or `[guid]::NewGuid().ToString()` in Windows PowerShell. Use each
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
Without a configured policy, a completion dependency is **unknown**; the board
shows no prompt or explanation. Row order, stage spelling,
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
member list is not needed.

A `program.md` descriptor holds exactly one `Program ID` line and one `Program
Title` line and no other non-empty content. The ID must equal the program
directory name, and the title must be 1–120 characters. One invalid descriptor
inside a canonical UUID program directory makes every program membership in the
workspace **unknown**, so program-title search finds nothing, while **Workspace
issues** names that descriptor. A program directory whose name is not a lowercase
version 4 UUID is skipped without a diagnostic, and its memberships stay
**unknown**. A whole `program.md` descriptor obeys the same
65,536-byte limit as a `spec.md` header, counted from the first byte of the
file, and a larger descriptor is reported as an invalid package. The browser
includes the resolved program title in search. Board columns remain projects.

## Diagnostics

**Workspace issues** lists unreadable directories, unreadable or invalid
`spec.md` files, and invalid or duplicate program descriptors. A prerequisite
that cannot be resolved to exactly one work item shows on the dependent card as
**unresolved target**. When one appears, check **Workspace issues** first for an
unreadable directory or specification file that made identity coverage
incomplete; if it is empty, check that the prerequisite UUID and the target's
`Package ID` are lowercase version 4 and unique, because an invalid or duplicate
ID produces no Workspace-issues entry, then inspect the affected specification
headers rather than inferring completion from an incomplete view. Nyx keeps its
conservative all-or-nothing resolution: while identity coverage is incomplete,
every resolved relationship shows **unresolved target**. Descriptor problems
affect programs only and do not make identity coverage incomplete. Uncheck **Hide
terminal rows** to show providers in finished rows and their connections to
dependent cards.

Discovery diagnostics also distinguish a safely identified but incomplete
project or stage from a confirmed empty one. Inspect the affected directory and
retry after restoring access; do not infer that no work items exist from an
incomplete scan. The browser retains the last valid displayed catalog when a
later refresh is malformed.
