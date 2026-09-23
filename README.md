# Nyx

Nyx is a work item viewer for plans, progress, and dependencies in a local
browser view. A work item is one specification package on the board. When work
spans several features or repositories, Nyx helps you see where each item
stands, which pieces depend on one another, and what needs attention next.

Your specifications remain ordinary Markdown files, where you can develop an
idea, describe the intended behavior, and keep the context needed to carry it
through implementation and review. Nyx organizes that work into a board by
project and development stage, giving you an overview without having to open
each specification individually.

You create, edit, and move those files yourself with an editor or your existing
development tools, or you can use an LLM coding agent to help. Either way, the
files remain the shared record of the work, and Nyx provides a consistent place
to follow it. Nyx reads your specifications and displays the result; it does
not edit specifications or move them for you.

![Nyx showing a fictional community event, with the festival plan selected and its permit outcomes available.](docs/images/sample-board.png)

*This fictional community-event view shows the board and selected work item in
the light theme. The compact and expanded disclosure views are documented in
[running Nyx](docs/running-nyx.md). The bundled trail-planning software sample
remains available in the [sample walkthrough](examples/sample-specifications/README.md).*

## Following your work items

The board brings work from multiple projects into one view, with project columns
and rows for planning, queued work, implementation, fixes, review, completion,
and archived work. Dependency connections help you follow relationships within
and across projects, while card details explain the recorded prerequisites.

Search helps you find work by its title, project, or program—a named group of
related specifications. You can hide stages to focus on active work and still
inspect a hidden prerequisite through a visible card's details.

Your plans stay in files you can edit, keep in version control, and use with
other tools. On Linux, Nyx runs locally as a background service and opens in
your browser. On Windows and macOS it runs as a self-contained desktop
application that owns its runtime and shows the same board in the application
window. Setup and runtime state stay account-local, and the board itself
remains a read-only view of the workspace.

## Try the sample

On Linux you need Python 3.12. From the repository root, use the commands below;
virtual environment activation is not required. After starting Nyx, explore the
board before running the final stop command.

Linux (POSIX shell):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/nyx --setup examples/sample-specifications --show-all-stages
.venv/bin/nyx
.venv/bin/nyx --status
.venv/bin/nyx --stop
```

Open **http://127.0.0.1:8765/**. Select **Build the route preview** to see why
its prerequisite is not yet satisfied, then compare it with **Plan the route
preview**. The [sample walkthrough](examples/sample-specifications/README.md)
explains the fictional software project, its custom stage, and the compact-view
workflow. For a non-programming example, the [workspace guide](docs/workspaces.md)
also shows a fictional community event with a hidden permit work item and two
recorded outcomes.

Throughout the linked guides, bare `nyx` is shorthand for the console installed
above: `.venv/bin/nyx`. That relative path works from the repository root. When
a guide runs commands from another directory, use the absolute path to that same
console, and quote paths containing spaces.

When files change, Nyx checks for an update every ten seconds. Click **Apply
update** when it appears to load the new view. **Refresh view** checks immediately
when no update is waiting. Use the stop command above when you are finished.

Setup saves the workspace location and configured stage visibility for your
account on this host. These setup choices are separate from the browser's personal
**Hide empty rows and columns** preference, which is stored only in that
browser. If Nyx is already running with a different setup, stop it before
changing the account-local settings. See [running Nyx](docs/running-nyx.md) for
status and configuration options.

Nyx stores configuration and runtime state in `.nyx` inside your home directory.
This is intentionally a fresh state root: legacy Linux state is neither read nor
migrated, and Nyx does not copy, remove, or fall back to it. Stop Nyx with your
existing installation before upgrading, then rerun setup with the new installation.
Setup saves an absolute workspace path local to that host; rerun setup on each
host using its local workspace location.

## Windows and macOS: the private desktop application

Windows and macOS run Nyx as a self-contained desktop application instead of the
Linux background service. It is a private internal feasibility build, not a
public release, and no support is promised beyond the hosts that were actually
exercised: a hosted Windows Server x64 image and Apple Silicon macOS. Your
operating system may ask you to trust or open the build the first time you run
it.

Build the private artifact from the repository root with the pinned desktop and
build extras:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install '.[desktop,build]'
.\.venv\Scripts\python.exe scripts\build_desktop.py
```

The build stages `dist\desktop\Nyx\Nyx.exe` on Windows and
`dist/desktop/Nyx.app` on macOS; macOS uses the same three commands with
`python3.12` and `.venv/bin/python`. Opening the staged application does not
need a separate Python installation or virtual environment, and there is no
offline-install guarantee.

The first launch shows a workspace chooser. Nyx starts its own runtime and shows
the board in the application window; **Quit**, or closing the only window, stops
that work and exits only after cleanup completes. Use **Change workspace** to
stop, save, and restart, and **Retry** when an incomplete stop or an unexpected
crash recovery is still blocked. See [running Nyx](docs/running-nyx.md) for the
desktop actions and their limits.

## Track your own work

Create a workspace with a directory for each project, organize specifications
under development stages, and point Nyx at that workspace. A specification is a
Markdown file named `spec.md` in its own directory. Its location supplies its
project and stage; its contents supply its title and other recorded information.

The [workspace guide](docs/workspaces.md) walks through your first specification,
moving work between stages, and adding dependencies when you need them.

Progress and dependency information comes from your specification files.
“Unblocked” means the recorded prerequisites are satisfied or none are listed.

## Working with coding agents

An LLM coding agent can help write specifications, update their recorded state,
and move them between stages as part of your development workflow. Give the
agent the same workspace structure and metadata conventions that you use
manually; Nyx reads the resulting files in either case.

This file-based workflow needs no model connection in Nyx. You choose the agent,
its access to your files, and the instructions and review process it follows.
Nyx itself does not launch agents or execute the work described by a specification.

## Documentation

- [Sample walkthrough](examples/sample-specifications/README.md): explore a small project.
- [Your specification workspace](docs/workspaces.md): create and organize your own work.
- [Running Nyx](docs/running-nyx.md): start, stop, check status, and choose visible stages.
- [Specification reference](docs/specification-reference.md): metadata, programs, and dependencies.

Nyx serves one local account on the fixed loopback address. The browser
is read-only; shared or remote hosting is not supported.
