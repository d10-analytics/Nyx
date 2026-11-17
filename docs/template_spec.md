# <Feature title>

Status: designing
Branch: spec/<feature-name>
Target repo: /absolute/path/to/repository
Baseline: <full 40-character lowercase commit SHA>
Sanity Recommendation: <PROCEED_TO_DESIGN|REDUCE|REDESIGN|DEFER|REJECT>
Sanity Baseline: <same full commit SHA>
Human Sanity Decision: <PENDING|AFFIRMED|OVERRIDDEN|DEFERRED|REJECTED>
Closure ledger: 1
Closure: pending
Package ID: <lowercase UUIDv4>

## Package relationship metadata

Every new package declares exactly one `Package ID: <lowercase UUIDv4>` scalar.
The package may additionally declare one `Program Membership: <program UUID>`
scalar, one `Superseded By: <package UUID>` scalar, zero or more
`Prerequisite: <package UUID> | <claim-name>` rows, zero or more
`Completion Prerequisite: <package UUID>` rows, and claims in either form:
`Claim: <name> | satisfied | <typed-provenance>` or
`Claim: <name> | <unsatisfied|unknown>`. Place all declarations in the metadata
header above the first `##` heading; omit optional declarations that do not
apply. Claim names match `[a-z][a-z0-9-]{0,63}`. Typed provenance is exactly a
lowercase `git-object-sha1:<40 hex>`, `git-object-sha256:<64 hex>`, or
`sha256:<64 hex>` token. An invalid value is not interpreted: Nyx leaves the
affected relationship unresolved or unknown, and the validator does not check
these fields. Existing packages without an identity remain legacy; new
packages must replace the Package ID placeholder with a generated value.

`Branch:` names the package's feature branch exactly as git spells it, without
Markdown formatting; write `Branch: not applicable — <reason>` when the target
is not a Git checkout. The validator requires exactly one `Branch:` line once
`Closure: approved` is recorded and in Queue, In_Progress, `.pipeline`,
Needs_Fixes and Awaiting_Retrospective. `Proposed branch:` is retired and
rejected in every stage.

## Objective

<Observable outcome and why it matters.>

## Requirements

- `R-01` — <Required behavior.>

## Anti-requirements

- `AR-01` — <Explicit exclusion or boundary.>

## Verified current behavior

- `F-01` — <Verified fact with exact source evidence.>

## Decisions

- `D-01` — <Decision; terse rationale naming the rejected alternative and
  the consequence avoided.>

<When the design risk lens applies, keep composition evidence beside its
decisions: shared resources/identifiers and owners, unchanged participants,
transitions/commit points, concrete interacting event sequences, required
outcomes and natural-trigger proof obligations. Otherwise give the reason
the lens does not apply.>

## File boundaries

| Task | File | Access and purpose |
| --- | --- | --- |
| T01 | `path/to/file` | Write: <production purpose for `AC-01`> |
| T01 | `tests/path/test_file.py` | Write: <natural-trigger proof for `AC-01`> |

## Acceptance criteria

- [ ] `AC-01` — Under `R-01` and `D-01`, <observable behavior and
  failing-capable distinction.>

<Each criterion cites at least one governing requirement or anti-requirement
and the decisions needed to interpret it. Include AR references only when
they constrain that behavior; both R and AR references are not mandatory.>

## Tasks

### T01 — <Small observable slice>

- Owns: `AC-01`.
- Depends on: none.
- Boundaries: `path/to/file`, `tests/path/test_file.py`.
- Risk: <low|standard|high and reason>.
- Implementation: <only ownership, contract, or migration context the agent needs>.
- Behavioral proof: <natural trigger and assertion that fails when the behavior
  breaks; where ownership matters, distinguish the canonical owner from
  output-equivalent fallback or alternative paths>.

## Verification

| Lane | Authority | Command | Required stage | Runner | Sandbox policy | CI-only | Push required |
| --- | --- | --- | --- | --- | --- | --- | --- |
| <lane> | `<defined ID or exact source path>` | `<exact command>` | <stage> | <runner> | <policy> | <yes|no> | <yes|no> |

## Documentation and human materials

- Documentation impact: <none or exact files/sections>.
- Human materials: <none or exact material>.

## Package attachments

<Optional; omit when none. Mockups, proofs, and other ancillary files that stay
with the package live under `attachments/`. One entry per file:>

- `attachments/<path>` — <purpose>

## Closure preflight

| Invariant class | Evidence references | Status |
| --- | --- | --- |
| Stable-ID expansion and generated excerpt | <IDs/generated view> | <OPEN|RECONCILED> |
| Verification fidelity | <verification row/authority snapshot> | <OPEN|RECONCILED> |
| Behavioral proof discrimination | <criteria/tasks/assertions> | <OPEN|RECONCILED> |
| Contract-domain coverage | <IDs/domain cases> | <OPEN|RECONCILED> |
| Commit order | <task/dependency/boundary evidence> | <OPEN|RECONCILED> |

## Closure review

```json
{
  "attempts": [],
  "findings": [],
  "resumptions": []
}
```

<Use the closure ledger reference of the workflow for the checked schema.
Keep evidence, interruption history, and method changes below this block with
stable local labels. Reserve repair rounds at assignment; preserve prior
attempts and permissions on resume. No approval while accepted findings or
human decisions remain unresolved.>
