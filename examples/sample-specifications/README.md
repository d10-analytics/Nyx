# Fictional sample specifications

[Return to the root Nyx guide](../../README.md).

This directory is a small, self-contained walkthrough for Nyx's catalog. Every
name, identifier, claim, and provenance value here is fictional. Point Nyx at
this directory directly; do not copy or generate anything into it.

## Board walkthrough

Run the setup, start, and stop commands from the repository guide:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
nyx --setup examples/sample-specifications --hide-stage Done
nyx
nyx --stop
```

With `Done` hidden, the board has four visible cards:

| Project | Stage | Package title | Package ID |
| --- | --- | --- | --- |
| Fictional_A | Under Development | Plan fictional onboarding | `11111111-1111-4111-8111-111111111111` |
| Fictional_A | Queue | Deliver fictional onboarding | `22222222-2222-4222-8222-222222222222` |
| Fictional_A | Needs Fixes | Repair fictional walkthrough | `33333333-3333-4333-8333-333333333333` |
| Fictional_A | Awaiting Retrospective | Review fictional walkthrough | `44444444-4444-4444-8444-444444444444` |

The board also groups the plan and delivery cards under the fictional program
`Fictional delivery program` (`99999999-9999-4999-8999-999999999999`).

## Relationship walkthrough

Select a visible card and open its details to see the direct prerequisite:

- `Plan fictional onboarding` declares `design-input` on hidden package
  `Fictional_B/Done/record`. The target's `design-input` claim is satisfied
  (`claim_satisfied`) using visibly synthetic `sha256:` provenance, so the
  plan's direct prerequisite state is satisfied.
- `Deliver fictional onboarding` declares `delivery-clearance` on the same
  hidden package. That target claim is unsatisfied (`claim_unsatisfied`), so
  the delivery card's direct prerequisite state is unsatisfied.

The hidden target remains reachable only through the selected visible
dependent's details; it is emitted as referenced context, not as a board card.
Its title is `Fictional prerequisite record` and its package ID is
`55555555-5555-4555-8555-555555555555`. It has exactly the two lower-case
claims named above.

The four visible cards and the hidden referenced target are the complete sample
workspace. The source descriptors are authoritative; this directory contains
no generated catalog, screenshots, caches, or other artifacts.
