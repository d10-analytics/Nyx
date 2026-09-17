# Specification reference

[Return to Nyx](../README.md).

For a first workspace, start with the [workspace guide](workspaces.md). This
reference describes the metadata used when you need more than a title and stage.

## Package header

Nyx reads a package's `spec.md` header up to the first line starting with `## `.
Put metadata in that header, one field per line.

| Field | Meaning |
| --- | --- |
| `# Title` | The first top-level heading supplies the card title. |
| `Package ID: UUID` | A stable, unique identity for the specification. |
| `Status: text` | A reported status shown in details; it does not change the stage. |
| `Target repo: path` | The final path component supplies the displayed target project. |
| `Program Membership: UUID` | Optional membership in a named group of work. |
| `Claim: name \| state [\| evidence]` | A named outcome this specification reports. |
| `Prerequisite: UUID \| claim-name` | A required outcome from another specification. |
| `Superseded By: UUID` | Optional reference to a replacement specification. |

Nyx also reads `Closure`, `Sanity Recommendation`, and `Human Sanity Decision`
as reported text. These fields are optional; they do not trigger actions.
Project and stage come from the package's directory location.

Generate fresh IDs with `python3 -c 'import uuid; print(uuid.uuid4())'`. Use each
package ID once in a workspace and retain it when moving the package. Repeat
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

A dependent specification refers to the providing package's ID and claim name:

```text
Prerequisite: 55555555-5555-4555-8555-555555555555 | contract-ready
```

This points to the API contract in the [sample workspace](../examples/sample-specifications/README.md).
In your workspace, replace the ID and claim name with those of the outcome you need.

Progress and dependency information comes from your specification files.
“Unblocked” means the recorded prerequisites are satisfied or none are listed.
The indicator concerns direct prerequisites. Missing, ambiguous, or malformed
information can leave a relationship unknown or unavailable; it is not treated
as satisfied. Moving a package to `Done` does not satisfy its claims automatically.

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

A participating package declares:

```text
Program Membership: 99999999-9999-4999-8999-999999999999
```

Use a fresh UUID for your own program and match it in the directory name,
descriptor, and membership fields. Membership comes from the packages; a separate
member list is not needed. The browser shows resolved program membership in card
details and includes the program title in search. Board columns remain projects.

## Diagnostics

Diagnostics identify problems reading or interpreting the workspace, such as
unreadable files, duplicate IDs, invalid fields, or unresolved references.
Inspect the affected specification and its prerequisites rather than inferring
completion from an incomplete view. A prerequisite hidden from the board by
stage policy can still be included as context in a visible dependent's details.
