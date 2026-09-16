# Nyx

Nyx is a Linux-local, loopback-only viewer for a specification workspace. It
reads package descriptors and presents their lifecycle placement, declared
metadata, optional program membership, claims, direct prerequisites, and
bounded diagnostics in a browser. The viewer does not modify the workspace;
setup and lifecycle commands persist only the current account's configuration
and runtime state.

The catalog is evidence about what the descriptors report. A card, lifecycle
placement, `Unblocked` label, claim, or prerequisite does not prove
implementation, approval, review, queue authority, merge, release, deployment,
confidentiality, or runtime execution. Nyx is read-only with respect to the
specification files, and it is intended for one Linux account through the
loopback interface, not for shared or remote hosting.

## Quick start

Run these commands from the repository root with Python 3.12 or newer:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
nyx --setup examples/sample-specifications --hide-stage Done
nyx --status
nyx
```

The empty `nyx` invocation starts the local instance and prints the fixed
browser URL:

```text
http://127.0.0.1:8765/
```

Run `nyx` again to reuse an authenticated ready instance; it prints the same
URL rather than starting a second daemon. `nyx --status` observes configuration
and runtime independently, reporting the configured specification root, hidden
stage policy, and whether the instance is running. It does not start, stop, or
reconfigure Nyx.

Open the printed URL in a browser. The board shows the stages admitted by the
configured policy. The viewer checks for a changed catalog every ten seconds;
when a new snapshot is available, the button changes to `Apply update`. Click
it to display that snapshot. `Refresh view` requests an immediate check when
there is no deferred update. Use the `Find` field to search visible package,
project, or program metadata, and select a card to inspect its declared values,
direct prerequisites, relationship state, and diagnostics.

Stop the instance before changing its setup policy:

```bash
nyx --stop
nyx --setup examples/sample-specifications --show-all-stages
nyx --status
nyx
nyx --stop
```

Setup persists the specification root and hidden-stage policy for the current
Linux account. `--hide-stage NAME` may be repeated with `--setup`; the explicit
`--show-all-stages` form clears the hidden-stage policy. Those options are
setup-only, and an active instance rejects a changed root or policy, so the
stop-before-reconfiguration order is required. Stopping an already stopped
instance is safe and reports `stopped`.

## Workspace and terminology

Nyx reads the admitted workspace shape:

```text
specification root / project / lifecycle / groups / package / spec.md
```

The lifecycle rows are Under Development, Queue, In Progress, Needs Fixes,
Awaiting Retrospective, Done, and Archive. A package is a directory containing
the descriptor `spec.md`; its package identity and declared fields come from
that descriptor. A project groups packages, while an optional program groups
selected package identities through a descriptor under `Reference/Programs`.
A claim is a named state reported by a package. A prerequisite is a package's
declared dependency on a claim from another package. A hidden package can still
be shown as referenced off-board context in a selected package's details.

Diagnostics describe malformed, incomplete, or unavailable catalog evidence;
they are not approval decisions. Search narrows the visible cards and details
show the declarations and relationship information for the selected card.
The [fictional sample walkthrough](examples/sample-specifications/README.md)
shows the smallest multi-stage, two-project example without copying real
specifications or generating catalog output. Its source descriptors are the
authoritative example; do not copy, generate, or modify files into that
directory.

For the parser and browser behavior behind these commands, consult the
existing CLI and installed-service checks in `tests/`; the root guide documents
their public workflow and does not create a second runtime or catalog owner.
