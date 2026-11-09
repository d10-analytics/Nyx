(() => {
  "use strict";

  const BOARD_ROWS = [
    ["under_development", "Under Development"],
    ["queue", "Queue"],
    ["in_progress", "In Progress"],
    ["needs_fixes", "Needs Fixes"],
    ["awaiting_retrospective", "Awaiting Retrospective"],
    ["done", "Done"],
    ["archive", "Archive"],
  ];
  const BOARD_LABELS = new Map(BOARD_ROWS);
  const BOARD_STAGES = new Map([
    ["Under_Development", "under_development"], ["Queue", "queue"],
    ["In_Progress", "in_progress"], ["Needs_Fixes", "needs_fixes"],
    ["Awaiting_Retrospective", "awaiting_retrospective"], ["Done", "done"],
    ["Archive", "archive"],
  ]);
  const UNRESOLVED_EDGE_REASONS = new Set([
    "missing_target", "duplicate_target", "identity_coverage_incomplete",
    "target_unreadable", "target_changed_during_read", "target_invalid_identity",
    "invalid_prerequisite", "self_edge",
  ]);
  const CLAIM_EDGE_REASONS = new Set([
    "claim_satisfied", "claim_unsatisfied", "claim_unknown", "missing_claim",
    "invalid_claim", "missing_target", "duplicate_target",
    "identity_coverage_incomplete", "target_unreadable",
    "target_changed_during_read", "target_invalid_identity", "self_edge",
    "invalid_prerequisite",
  ]);
  const COMPLETION_EDGE_REASONS = new Set([
    "completion_satisfied", "completion_unsatisfied", "completion_policy_needed",
    "completion_policy_invalid", "missing_target", "duplicate_target",
    "identity_coverage_incomplete", "target_unreadable",
    "target_changed_during_read", "target_invalid_identity", "self_edge",
    "invalid_prerequisite",
  ]);
  // These fields are indexed for search. Only the title and target project
  // are displayed.
  const DECLARED_FIELDS = ["title", "target_project", "status"];
  const REPORTED_FIELD_KEYS = ["name", "value"];
  const MAX_REPORTED_FIELDS = 64;
  const SCHEMA_VERSION = 8;
  const SAFE_CATEGORIES = [
    "producer_unavailable", "producer_timeout", "producer_failed",
    "producer_output_too_large", "producer_protocol_error",
    "checkouts_unavailable", "checkouts_timeout", "checkouts_failed",
    "checkouts_output_too_large", "checkouts_cancelled",
    "checkouts_protocol_error",
  ];
  const SVG_NS = "http://www.w3.org/2000/svg";
  const RAIL_PITCH = 14;
  const RAIL_INSET = 20;
  const RAIL_LANE_CAP = 8;
  const POLL_INTERVAL = 10000;
  const CATALOG_ROUTE = "/api/catalog";
  const CHECKOUTS_ROUTE = "/api/checkouts";
  // The card reads the checkout facts document at exactly this schema, and the
  // digest covers every top-level key except the digest and its observation
  // instant, so a document the collector wrote authenticates unchanged.
  const CHECKOUTS_SCHEMA_VERSION = 9;
  const CHECKOUTS_DIGEST_OMITTED = ["checkouts_digest", "observed_at"];
  const CHECKOUTS_KEYS = [
    "schema_version", "checkouts_digest", "observed_at", "configuration_revision",
    "git", "remote_checks", "github_lookup", "folders", "repositories",
    "diagnostics", "package_scan", "folder_suggestions", "packages", "attention",
  ];
  const CHECKOUTS_PACKAGE_KEYS = [
    "package_path", "package_id", "finished", "superseded", "target", "branch",
    "baseline", "code_status",
  ];
  const CHECKOUTS_CODE_STATUS_KEYS = [
    "state", "text", "pull_request", "push", "unpushed", "cleanup",
    "checked_out_elsewhere",
  ];
  const SETTINGS_ROUTE = "/api/settings";
  const WORKSPACE_ROUTE = "/api/workspace";
  const REMOTE_CHECK_ROUTE = "/api/checkouts/remote-check";
  const VIEW_BOARD = "board";
  const VIEW_CHECKOUTS = "checkouts";
  const COMPACT_STORAGE_KEY = "spec-tracker-compact-view";
  // The terminal-row preference mirrors the compact-view preference: it is a
  // browser-local display choice applied to the catalog already fetched.  It
  // never changes setup, the workspace, or the catalog.  Unchecking the box
  // makes finished stages eligible for display again.
  const TERMINAL_STORAGE_KEY = "spec-tracker-hide-terminal-rows";
  const DESELECT_STORAGE_KEY = "spec-tracker-deselect-on-empty-click";
  // The shown view is a browser-local display choice stored like the other
  // preferences: a fixed Board default, read and written in try/catch, and an
  // unknown stored value falls back to Board.
  const VIEW_STORAGE_KEY = "spec-tracker-view";
  const QUIET_CHECKOUTS_STORAGE_KEY = "spec-tracker-hide-quiet-checkouts";

  // The words of the checkouts view.  Every enumerated value the view shows and
  // every sentence it composes is read from this one block, so a value a newer
  // collector serves before this page learns its words still renders its raw
  // code instead of throwing.  The block is JSON so a browserless test can read
  // it between its markers and compare it with the collector's enumerations.
  // checkout words: begin
  const WORDS = {
    "attentionClass": {
      "unique_work": "Work only on this machine",
      "awaiting_merge": "Finished, not merged",
      "cleanup_ready": "Ready to clean up",
      "base_moved": "Base moved",
      "configuration": "Setup problems"
    },
    "attentionCode": {
      "branch_unpushed": "{branch} in {clone} has {count} commits not on the remote",
      "branch_not_pushed": "{branch} in {clone} has never been pushed",
      "default_branch_ahead": "default branch {branch} in {clone} has {count} commits not on the remote",
      "stash": "{clone} has {count} stashes",
      "uncommitted_changes": "{worktree} has {count} uncommitted changes",
      "awaiting_merge": "{branch} is finished but not merged",
      "removable": "{clone} has {count} branches or worktrees ready to remove",
      "base_moved": "the base of {workItem} moved {count} commits past its Baseline",
      "target_repo_missing": "{workItem} names a Target repo that does not exist",
      "target_repo_not_git": "{workItem} names a Target repo that is not a git checkout",
      "branch_invalid": "{workItem} names a Branch that is not a branch name",
      "upstream_mismatch": "{branch} in {clone} does not track the same-named branch of origin",
      "never_fetched": "{clone} has never fetched",
      "no_origin_remote": "{clone} has no origin remote",
      "checkout_unavailable": "{clone} could not be read",
      "finished_stages_not_configured": "No stage counts as finished; choose one under Counts as finished in Board settings",
      "package_scan_incomplete": "Some work items could not be read",
      "package_scan_unavailable": "Work items could not be read"
    },
    "availabilityState": {
      "free": "Free",
      "assigned": "Assigned",
      "occupied": "In use",
      "unknown": "Unknown"
    },
    "availabilityReason": {
      "not_on_default_branch": "not on the default branch",
      "detached_head": "detached HEAD",
      "linked_worktrees": "has linked worktrees",
      "uncommitted_changes": "uncommitted changes",
      "operation_in_progress": "git operation in progress"
    },
    "assignmentBasis": {
      "target_repo": "Target repo",
      "target_repo_worktree": "Target repo worktree"
    },
    "status": {
      "ok": "",
      "unsafe_ownership": "git refused it: ownership",
      "failed": "could not be read",
      "timed_out": "git did not answer in time",
      "too_many_branches": "more than 100 local branches; not examined"
    },
    "comparisonRelation": {
      "equal": "up to date",
      "ahead": "{a} ahead",
      "behind": "{b} behind",
      "diverged": "{a} ahead, {b} behind",
      "differs": "differs (not counted)",
      "absent": "not on the remote",
      "unknown": "unknown"
    },
    "comparisonBasis": {
      "live": "live",
      "recorded": "recorded {time}",
      "cached": "cached"
    },
    "verdictState": {
      "merged": "merged",
      "not_merged": "not merged",
      "unknown": "merge state unknown"
    },
    "verdictRung": {
      "ancestor": "tip is in {default}",
      "pull_request": "pull request #{n} merged",
      "patch_equivalent": "every change is in {default}",
      "squash_equivalent": "squash-merged into {default}",
      "tree_equal": "{default} has the same files",
      "none": ""
    },
    "verdictReason": {
      "no_base": "no default branch to compare",
      "incomplete_clone": "shallow or partial clone",
      "git_too_old": "git too old for this check",
      "history_limit": "history too long to check",
      "deadline": "ran out of time",
      "failed": "a git call failed",
      "objects_missing": "no clone holds its commits",
      "ambiguous_tip": "its history is ambiguous"
    },
    "branchBlocker": {
      "not_merged": "not merged",
      "verdict_unknown": "merge state unknown",
      "checked_out_in_main_worktree": "checked out in the main worktree",
      "worktree_not_clean": "its worktree has changes",
      "worktree_locked": "its worktree is locked"
    },
    "worktreeBlocker": {
      "not_merged": "not merged",
      "verdict_unknown": "merge state unknown",
      "not_clean": "has changes",
      "locked": "locked"
    },
    "operation": {
      "am": "applying patches",
      "bisect": "bisect",
      "cherry_pick": "cherry-pick",
      "merge": "merge",
      "rebase": "rebase",
      "revert": "revert"
    },
    "changeKind": {
      "staged": "staged",
      "unstaged": "unstaged",
      "untracked": "untracked",
      "conflicted": "conflicted"
    },
    "folderStatus": {
      "ok": "",
      "missing": "does not exist",
      "unreadable": "cannot be read",
      "not_directory": "is not a folder"
    },
    "gitStatus": {
      "available": "",
      "missing": "Git was not found, so Nyx cannot read checkouts. Install Git and make sure it is on the PATH Nyx runs with (on macOS, the Command Line Tools).",
      "too_old": "Git {version} is older than Nyx supports (2.36)",
      "tooOldUnread": "Git is older than Nyx supports (2.36), or its version could not be read"
    },
    "checkStatus": {
      "live": "checked live",
      "recorded": "recorded {time}",
      "failed": "check failed",
      "off": "remote checks off"
    },
    "checkReason": {
      "unreachable": "remote unreachable",
      "not_found": "remote repository not found",
      "authentication_failed": "authentication failed",
      "timed_out": "timed out",
      "failed": "failed",
      "remote_checks_off": "remote checks are off"
    },
    "lookupStatus": {
      "live": "pull requests live",
      "recorded": "pull requests recorded {time}",
      "off": "pull request lookup off",
      "unavailable": "pull requests unavailable: {reason}"
    },
    "lookupReason": {
      "gh_missing": "the GitHub CLI (gh) was not found",
      "not_authenticated": "gh is not signed in",
      "not_found": "not found on GitHub",
      "rate_limited": "GitHub rate limit reached",
      "timed_out": "timed out",
      "failed": "failed",
      "setting_off": "lookup is off for this workspace",
      "not_github": "not a GitHub repository",
      "remote_checks_off": "remote checks are off"
    },
    "remoteCopy": {
      "present": "on the remote ({basis})",
      "absent": "not on the remote",
      "unknown": "unknown"
    },
    "pullRequestState": {
      "open": "open",
      "merged": "merged",
      "closed": "closed",
      "differentHead": "(different head)"
    },
    "branchPackageBasis": {
      "target_clone": "Target repo",
      "sibling_clone": "same branch in another clone",
      "contained": "folded into this branch"
    },
    "containment": {
      "complete": "",
      "deadline": "ran out of time",
      "failed": "failed",
      "no_base": "no default branch",
      "incomplete": "shallow or partial clone",
      "git_too_old": "git too old"
    },
    "worktreeFlag": {
      "prunable": "directory is gone",
      "locked": "locked"
    },
    "diagnostic": {
      "not_a_repository": "{path} has a .git entry but is not the top of its repository",
      "unrepresentable_path": "a checkout under {path} (or under a configured folder) has a path that cannot be shown",
      "checkout_cache_unreadable": "the saved verdict cache {path} could not be used and was treated as empty",
      "checkout_cache_write_failed": "the saved verdict cache {path} could not be updated"
    },
    "clearing": {
      "checkouts_unavailable": "It clears once Nyx serves checkout state again.",
      "checkouts_cancelled": "It clears when Nyx starts again.",
      "checkouts_failed": "It clears when a later collection publishes a document.",
      "checkouts_timeout": "It clears when a later collection finishes in time.",
      "checkouts_output_too_large": "It clears when a later collection's document fits.",
      "checkouts_protocol_error": "It clears when a later collection returns a valid document."
    },
    "column": {
      "checkout": "Checkout",
      "availability": "Availability",
      "head": "HEAD",
      "default": "Default branch vs remote",
      "tree": "Tree",
      "branches": "Branches",
      "cleanup": "Cleanup",
      "package": "Package",
      "branch": "Branch",
      "remoteCopy": "Remote copy",
      "verdict": "Verdict",
      "pullRequests": "Pull requests",
      "workItems": "Work items"
    },
    "text": {
      "localState": "Local state {time}",
      "remotesNotChecked": "Remotes not checked yet",
      "remotesChecked": "Remotes checked {time}",
      "remoteCheckFailedAt": "Last remote check failed ({outcome}) at {time}",
      "checkingRemotes": "Checking remotes…",
      "checkRemotes": "Check remotes now",
      "nextCheckAbout": "Next check about {time}",
      "nextCheckAtRefresh": "Next check at the next refresh",
      "lookupLive": "Pull requests live",
      "lookupRecorded": "Pull requests recorded {time}",
      "lookupOff": "Pull request lookup off",
      "lookupUnavailable": "Pull requests unavailable: {reason}",
      "notOffered": "This Nyx server does not serve checkout state.",
      "unavailable": "Checkout state is unavailable: {category}.",
      "clearsDefault": "It clears when a later collection succeeds.",
      "doesNotOfferRemotes": "This server does not offer remote checks.",
      "runCompleted": "Remote check finished.",
      "runFailed": "Remote check failed: {category}.",
      "noOriginRemote": "no origin remote",
      "noFolders": "No checkout folders are configured. Add them to the workspace record; see the guide section Your specification workspace → Checkout folders.",
      "ghMissing": "The GitHub CLI (gh) was not found; pull request states are not shown.",
      "setupHeading": "Checkout setup",
      "quietRow": "Quiet repositories ({count})",
      "freeClones": "Free clones: {clones}",
      "workItemBranches": "Work item branches",
      "showOnBoard": "Show on board",
      "showOnBoardFinished": "This work item is in a finished row; uncheck Hide terminal rows to show it.",
      "showOnBoardMissing": "This work item is not on the displayed board; choose Refresh view on the board.",
      "uncommittedOperation": "an unfinished git operation",
      "awaitingMergePullRequest": "(pull request #{n})",
      "settingsDiffer": "Collected under different settings from the board shown.",
      "shallow": "shallow",
      "partial": "partial",
      "neverFetched": "cached, never fetched",
      "fetchedAgo": "cached, fetched {age} ago",
      "latestCheckFailed": "(latest check failed: {reason})",
      "clean": "clean",
      "operationInProgress": "{operation} in progress",
      "indexLock": "index lock present",
      "bareRepository": "bare repository",
      "noWorkingTree": "no working tree",
      "detachedAt": "detached at {sha}",
      "unborn": "unborn {branch}",
      "assignedPrefix": "Assigned",
      "noPackage": "—",
      "nothingToRemove": "nothing to remove",
      "removable": "Removable",
      "foldingNotChecked": "Branches were not checked for folding: {reason}",
      "folderProblem": "{path} — {problem}",
      "unknownPath": "unknown path"
    }
  };
  // checkout words: end

  const board = document.querySelector("#board");
  const toolbar = document.querySelector(".toolbar");
  const filter = document.querySelector("#filter");
  const compactControl = document.querySelector("#compact-view");
  const terminalControl = document.querySelector("#hide-terminal-rows");
  const deselectControl = document.querySelector("#deselect-on-empty-click");
  const quietCheckoutsControl = document.querySelector("#hide-quiet-checkouts");
  const quietCheckoutsLabel = quietCheckoutsControl.closest("label");
  const viewBoardButton = document.querySelector("#view-board");
  const viewCheckoutsButton = document.querySelector("#view-checkouts");
  const checkoutsView = document.querySelector("#checkouts");
  const checkoutsStatusLine = document.querySelector("#checkouts-status-line");
  const checkoutsActionStatus = document.querySelector("#checkouts-action-status");
  const checkoutsBody = document.querySelector("#checkouts-body");
  const checkRemotesButton = document.querySelector("#check-remotes");
  const refreshButton = document.querySelector("#refresh");

  const copyStatus = document.querySelector("#copy-status");
  const stageOrderControls = document.querySelector("#stage-order-controls");
  const stageOrderList = document.querySelector("#stage-order-list");
  const stageOrderSave = document.querySelector("#stage-order-save");
  const stageOrderCancel = document.querySelector("#stage-order-cancel");
  const stageOrderReset = document.querySelector("#stage-order-reset");
  const stageOrderReload = document.querySelector("#stage-order-reload");
  const stageOrderStatus = document.querySelector("#stage-order-status");

  let displayed = null;
  let pending = null;
  let busy = false;
  let queued = null;
  let selectedPath = null;
  let workspace = null;
  let workspaceRequest = 0;
  let copyAttempt = 0;
  let copyStatusTimer = null;
  let railPlan = new Map();
  let railColumns = [];
  let railEdges = [];
  let cardPrerequisites = new Map();
  let compactView = true;
  let hideTerminalRows = true;
  let deselectOnEmptyClick = false;
  let refreshFailure = null;
  let shownView = VIEW_BOARD;
  let hideQuietCheckouts = true;
  let savedStageOrder = [];
  let savedCompletedStages = [];
  let editorStageOrder = [];
  let editorCompletedStages = [];
  let settingsRevision = null;
  let settingsAvailable = false;
  let settingsReloadAvailable = false;
  let settingsBusy = false;
  let settingsRequestSerial = 0;
  let queuedSettingsRefresh = null;
  // The last checkouts outcome: "idle", "not_offered", "ready" with its
  // envelope, or "failed" with its category.  "failed" adds a Code Status part
  // to the issues panel, and every settled outcome rewrites the cards' lines.
  let checkoutsOutcome = {state: "idle", envelope: null, failure: null};
  let checkoutsBusy = false;
  // One press at a time: a press while a request is in flight is remembered as
  // the single queued POST and sent when that request settles.  The flag stays
  // set from the press to the POST's settle, so further presses do nothing.
  let checkoutsCheckRequested = false;
  let checkoutsQueuedCheck = false;
  // The Code Status line of each rendered card, and the entry it was rendered
  // from, so the in-place update rewrites exactly the line and the search text.
  let cardCodeStatus = new Map();
  let renderedEntries = new Map();

  function text(value) {
    const raw = value === null || value === undefined || value === "" ? "Unknown" : String(value);
    return raw.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }

  // The words of one enumerated value.  An unknown code renders as itself, so a
  // value this page has no words for is still shown rather than thrown away.
  function codeWords(table, code) {
    const words = WORDS[table];
    if (words === undefined || words === null) return String(code);
    const found = words[code];
    return found === undefined ? String(code) : found;
  }

  // One word entry with its placeholders filled; every value is escaped for
  // markup unless the caller passes markup it built itself.
  function fillWords(template, values, escape) {
    return String(template).replace(/\{(\w+)\}/g, (match, key) => {
      const value = values[key];
      if (value === undefined || value === null) return "";
      return escape(String(value));
    });
  }

  function wordText(table, code, values = {}) {
    return fillWords(codeWords(table, code), values, text);
  }

  function wordHtml(table, code, values = {}) {
    return fillWords(codeWords(table, code), values, (value) => value);
  }

  function titleOf(entry) {
    return entry.declared.title || entry.package_path;
  }

  function columnKeyOf(entry) {
    return entry.project || "";
  }

  function projectOf(entry) {
    return entry.project || "";
  }

  function indexByPackageId(entries) {
    const index = new Map();
    entries.forEach((entry) => {
      if (!entry.package_id) return;
      index.set(entry.package_id, index.has(entry.package_id) ? null : entry);
    });
    return index;
  }

  function indexDependents(entries, byId) {
    const index = new Map();
    entries.forEach((entry) => {
      (entry.relationship.prerequisites || []).forEach((edge) => {
        if (!prerequisiteTarget(edge, byId)) return;
        if (!index.has(edge.target_package_id)) index.set(edge.target_package_id, []);
        const dependents = index.get(edge.target_package_id);
        if (!dependents.includes(entry)) dependents.push(entry);
      });
    });
    return index;
  }

  function prerequisiteTargets(entry, byId) {
    const targets = new Map();
    (entry.relationship.prerequisites || []).forEach((edge) => {
      const key = edge.target_package_id || "";
      const target = prerequisiteTarget(edge, byId);
      const previous = targets.get(key);
      // An invalid declaration must not hide a valid relationship to the same item.
      if (!previous || (!previous.target && target)) targets.set(key, {edge, target});
    });
    return [...targets.values()];
  }

  function depthForColumn(columnEntries, edges) {
    const ids = new Set(columnEntries.map((entry) => entry.package_id).filter(Boolean));
    const dependents = new Map([...ids].map((id) => [id, new Set()]));
    const prerequisites = new Map([...ids].map((id) => [id, new Set()]));
    // Ordering and arrows share the same globally unambiguous connections.
    edges.forEach(({ source, dependent }) => {
      if (!ids.has(source) || !ids.has(dependent)) return;
      dependents.get(source).add(dependent);
      prerequisites.get(dependent).add(source);
    });

    // Collapse strongly connected packages before assigning dependency depth.
    // Iterative traversals also accommodate chains beyond the browser's call-stack limit.
    const visited = new Set();
    const finished = [];
    ids.forEach((root) => {
      if (visited.has(root)) return;
      visited.add(root);
      const stack = [[root, dependents.get(root).values()]];
      while (stack.length) {
        const [id, children] = stack[stack.length - 1];
        const next = children.next();
        if (next.done) {
          finished.push(id);
          stack.pop();
        } else if (!visited.has(next.value)) {
          visited.add(next.value);
          stack.push([next.value, dependents.get(next.value).values()]);
        }
      }
    });
    const componentOf = new Map();
    let componentCount = 0;
    finished.reverse().forEach((root) => {
      if (componentOf.has(root)) return;
      const component = componentCount++;
      componentOf.set(root, component);
      const stack = [root];
      while (stack.length) {
        prerequisites.get(stack.pop()).forEach((id) => {
          if (componentOf.has(id)) return;
          componentOf.set(id, component);
          stack.push(id);
        });
      }
    });

    const outgoing = Array.from({ length: componentCount }, () => new Set());
    const indegree = Array(componentCount).fill(0);
    dependents.forEach((children, source) => {
      const from = componentOf.get(source);
      children.forEach((dependent) => {
        const to = componentOf.get(dependent);
        if (from === to || outgoing[from].has(to)) return;
        outgoing[from].add(to);
        indegree[to] += 1;
      });
    });
    const depth = Array(componentCount).fill(0);
    const queue = [...outgoing.keys()].filter((component) => indegree[component] === 0);
    for (let cursor = 0; cursor < queue.length; cursor += 1) {
      const component = queue[cursor];
      outgoing[component].forEach((dependent) => {
        depth[dependent] = Math.max(depth[dependent], depth[component] + 1);
        indegree[dependent] -= 1;
        if (indegree[dependent] === 0) queue.push(dependent);
      });
    }
    return new Map([...ids].map((id) => [id, depth[componentOf.get(id)]]));
  }

  function prerequisiteTarget(edge, byId) {
    if (UNRESOLVED_EDGE_REASONS.has(edge.reason)) return null;
    return byId.get(edge.target_package_id) || null;
  }

  function stageKeyOf(stage) {
    return stage;
  }

  function rowDataKeyOf(stage) {
    return stage;
  }

  function stageLabelOf(stage) {
    return BOARD_LABELS.get(BOARD_STAGES.get(stage)) || stage;
  }

  function inventoryStageNames(snapshot = displayed) {
    if (!snapshot) return [];
    const names = [];
    const seen = new Set();
    (snapshot.inventory.stages || []).forEach((record) => {
      if (seen.has(record.stage)) return;
      seen.add(record.stage);
      names.push(record.stage);
    });
    return names;
  }

  function projectedStageNames(snapshot = displayed) {
    const eligible = inventoryStageNames(snapshot);
    const available = new Set(eligible);
    const result = [];
    const seen = new Set();
    savedStageOrder.forEach((stage) => {
      if (!available.has(stage) || seen.has(stage)) return;
      seen.add(stage);
      result.push(stage);
    });
    eligible.forEach((stage) => {
      if (seen.has(stage)) return;
      seen.add(stage);
      result.push(stage);
    });
    return result;
  }

  function editorNames(snapshot = displayed) {
    const result = [];
    const seen = new Set();
    [...savedStageOrder, ...savedCompletedStages, ...inventoryStageNames(snapshot)].forEach((stage) => {
      if (seen.has(stage)) return;
      seen.add(stage);
      result.push(stage);
    });
    return result;
  }

  function hasUnsavedStageOrder() {
    return JSON.stringify(editorStageOrder) !== JSON.stringify(editorNames()) ||
      JSON.stringify([...editorCompletedStages].sort(scalarCompare)) !==
        JSON.stringify([...savedCompletedStages].sort(scalarCompare));
  }

  function renderStageOrderEditor() {
    const editor = document.querySelector("#stage-order-editor");
    const controls = () => [...editor.querySelectorAll("summary, input, button, select")];
    const focused = document.activeElement;
    const priorControls = controls();
    const priorIndex = priorControls.indexOf(focused);
    const keyOf = (control) => {
      const item = control.closest(".stage-order-item");
      return item ? [item.dataset.stage, control.dataset.stageMove || "completed"] : [control.id];
    };
    const priorKey = priorIndex >= 0 ? keyOf(focused) : null;
    const restoreFocus = () => {
      if (!priorKey) return;
      const current = controls();
      const operable = (control) => control?.isConnected && !control.disabled &&
        !control.closest("[hidden]");
      const same = current.find((control) =>
        JSON.stringify(keyOf(control)) === JSON.stringify(priorKey) && operable(control));
      const siblings = priorKey.length === 2 ? current.filter((control) => {
        const key = keyOf(control);
        return key.length === 2 && key[0] === priorKey[0] && operable(control);
      }) : [];
      const successor = same || siblings.find((control) => control.dataset.stageMove) ||
        siblings[0] || current.slice(priorIndex + 1).find(operable) ||
        current.slice(0, priorIndex).reverse().find(operable) ||
        document.querySelector("#stage-order-heading");
      successor.focus();
    };
    stageOrderControls.hidden = !settingsAvailable && !settingsReloadAvailable;
    if (stageOrderControls.hidden) {
      restoreFocus();
      return;
    }
    if (settingsAvailable && !editorStageOrder.length) editorStageOrder = editorNames();
    const controlsDisabled = !settingsAvailable;
    const unavailable = (disabled) => disabled ? " disabled" :
      settingsBusy ? ' aria-disabled="true"' : "";
    stageOrderList.innerHTML = editorStageOrder.map((stage, index) => {
      const label = stageLabelOf(stage);
      const dormant = inventoryStageNames().includes(stage) ? "" :
        '<span class="stage-order-dormant">(not currently available)</span>';
      const completed = editorCompletedStages.includes(stage);
      return `<li class="stage-order-item" data-stage="${text(stage)}">` +
        `<span class="stage-order-name">${text(label)}${dormant}</span>` +
        `<label class="stage-completed"><input type="checkbox" class="stage-completed-toggle" ` +
        `aria-label="Counts as finished: ${text(label)}" data-stage="${text(stage)}"` +
        `${completed ? " checked" : ""}${unavailable(controlsDisabled)}>` +
        `Counts as finished</label>` +
        `<button type="button" class="stage-order-move" data-stage-move="up" ` +
        `aria-label="Move ${text(label)} up"${unavailable(controlsDisabled || index === 0)}>Move up</button>` +
        `<button type="button" class="stage-order-move" data-stage-move="down" ` +
        `aria-label="Move ${text(label)} down"${unavailable(controlsDisabled || index === editorStageOrder.length - 1)}>Move down</button>` +
        `</li>`;
    }).join("");
    stageOrderSave.disabled = controlsDisabled || !hasUnsavedStageOrder();
    stageOrderCancel.disabled = controlsDisabled || !hasUnsavedStageOrder();
    stageOrderReset.disabled = controlsDisabled;
    stageOrderReload.hidden = !settingsReloadAvailable;
    stageOrderReload.disabled = false;
    [stageOrderSave, stageOrderCancel, stageOrderReset, stageOrderReload].forEach((control) => {
      if (settingsBusy && !control.disabled) control.setAttribute("aria-disabled", "true");
      else control.removeAttribute("aria-disabled");
    });
    restoreFocus();
  }

  // One arrow per unambiguous prerequisite/dependent pair, regardless of claim count.
  function planEdges(entries, byId) {
    const seen = new Set();
    const edges = [];
    entries.forEach((entry) => {
      if (byId.get(entry.package_id) !== entry) return;
      (entry.relationship.prerequisites || []).forEach((edge) => {
        const source = prerequisiteTarget(edge, byId);
        if (!source || source.package_id === entry.package_id) return;
        const key = `${source.package_id}>${entry.package_id}`;
        if (seen.has(key)) return;
        seen.add(key);
        edges.push({
          source: source.package_id, dependent: entry.package_id,
          sourceColumn: columnKeyOf(source), dependentColumn: columnKeyOf(entry),
          row: rowDataKeyOf(source.stage),
        });
      });
    });
    edges.sort((a, b) => a.source.localeCompare(b.source) || a.dependent.localeCompare(b.dependent));
    return edges;
  }

  function orderCell(cell, depth) {
    return [...cell].sort((a, b) => {
      const depthA = a.package_id ? depth.get(a.package_id) ?? 0 : 0;
      const depthB = b.package_id ? depth.get(b.package_id) ?? 0 : 0;
      if (depthA !== depthB) return depthA - depthB;
      return titleOf(a).localeCompare(titleOf(b));
    });
  }

  // Keep names visible for long arrows or missing lanes, and available to
  // screen readers when a same-project arrow supplies the visual connection.
  function needsHtml(entry, byId) {
    const railNames = [];
    const lanes = railPlan.get(columnKeyOf(entry))?.lanes;
    const parts = prerequisiteTargets(entry, byId).map(({edge, target}) => {
      if (!target) {
        // A valid edge may point to a target in a gated finished row. It is
        // absent from the interactive index, but it is not an unresolved target.
        if (edge.target_package_id && !UNRESOLVED_EDGE_REASONS.has(edge.reason)) return "";
        return '<span class="link unresolved">unresolved target</span>';
      }
      if (target.package_id === entry.package_id) return "";
      if (columnKeyOf(target) === columnKeyOf(entry)) {
        if (lanes?.has(`${target.package_id}>${entry.package_id}`)) {
          railNames.push(titleOf(target));
          return "";
        }
        return `<span class="link">${text(titleOf(target))}</span>`;
      }
      return `<span class="link cross">${text(titleOf(target))} ` +
        `<em>${text(projectOf(target))}</em></span>`;
    }).filter(Boolean);
    return (parts.length ? `<span class="card-links">needs: ${parts.join(" · ")}</span>` : "") +
      (railNames.length ? `<span class="visually-hidden card-rail-text">${text(`needs: ${railNames.join(" · ")}`)}</span>` : "");
  }

  function blocksHtml(entry, dependentsOf) {
    const railNames = [];
    const lanes = railPlan.get(columnKeyOf(entry))?.lanes;
    const parts = (dependentsOf.get(entry.package_id) || [])
      .map((dependent) => {
        if (dependent.package_id === entry.package_id) return "";
        if (columnKeyOf(dependent) === columnKeyOf(entry)) {
          if (lanes?.has(`${entry.package_id}>${dependent.package_id}`)) {
            railNames.push(titleOf(dependent));
            return "";
          }
          return `<span class="link">${text(titleOf(dependent))}</span>`;
        }
        return `<span class="link cross">${text(titleOf(dependent))} ` +
          `<em>${text(projectOf(dependent))}</em></span>`;
      }).filter(Boolean);
    return (parts.length ? `<span class="card-links">blocks: ${parts.join(" · ")}</span>` : "") +
      (railNames.length ? `<span class="visually-hidden card-rail-text">${text(`blocks: ${railNames.join(" · ")}`)}</span>` : "");
  }

  // The line map every card reads: empty unless the last outcome is "ready" and
  // its document was scanned under the configuration the board is showing.  A
  // text computed under another configuration is wrong rather than late, so it
  // is hidden; a null catalog revision never matches.
  function codeStatusLineMap() {
    const lines = new Map();
    const revision = displayed?.configuration_revision;
    if (!revision || checkoutsOutcome.state !== "ready") return lines;
    const document = checkoutsOutcome.envelope.checkouts;
    if (document.configuration_revision !== revision) return lines;
    document.packages.forEach((item) => {
      if (item.code_status) lines.set(item.package_path, item.code_status.text);
    });
    return lines;
  }

  function searchText(entry, line) {
    return [
      entry.package_path, entry.package_id, entry.stage, entry.relationship.program.title,
      ...DECLARED_FIELDS.map((field) => entry.declared[field]),
      ...entry.reported_fields.map((field) => field.value),
      line,
    ].filter((value) => value !== null && value !== undefined).join(" ").toLowerCase();
  }

  function cardHtml(entry, byId, dependentsOf) {
    const selected = selectedPath === entry.package_path;
    const idAttribute = entry.package_id
      ? ` data-package-id="${text(entry.package_id)}"` : "";
    const targetProject = entry.declared.target_project &&
      entry.declared.target_project !== entry.project ? entry.declared.target_project : "";
    const codeStatus = cardCodeStatus.get(entry.package_path);
    return `<button class="card${selected ? " selected" : ""}" ` +
      'type="button" ' +
      `aria-pressed="${selected}" data-package-path="${text(entry.package_path)}"` +
      `${idAttribute} data-search="${text(searchText(entry, codeStatus))}">` +
      `<span class="card-title">${text(titleOf(entry))}</span>` +
      (targetProject ? `<span class="card-project">Target Folder: ${text(targetProject)}</span>` : "") +
      `<span class="card-filepath">Filepath: ${text(entry.package_path)}</span>` +
      (codeStatus === undefined ? "" :
        `<span class="card-code-status">Code Status: ${text(codeStatus)}</span>`) +
      '<span class="visually-hidden card-start-text"></span>' +
      needsHtml(entry, byId) + blocksHtml(entry, dependentsOf) + "</button>";
  }

  function cellHtml(cell, byId, dependentsOf, gutter) {
    const padding = gutter ? ` style="padding-left:${gutter}px"` : "";
    if (!cell.length) return `<div class="cell vacant"${padding}><span class="empty">—</span></div>`;
    return `<div class="cell"${padding}>` +
      cell.map((entry) => cardHtml(entry, byId, dependentsOf)).join("") + "</div>";
  }

  // Finished ("terminal") stages the reader has chosen to hide right now.  The
  // set is recomputed on every render because the completion policy can change
  // when Board settings are saved.  When the box is unchecked the set is empty,
  // so finished rows are treated like any other row.
  function gatedTerminalStages() {
    if (!hideTerminalRows) return new Set();
    return new Set(displayed?.terminal_stages || []);
  }

  function inventoryAxes(entries) {
    // Terminal gating is purely a browser display choice: it removes finished
    // rows from this render and touches neither the catalog nor the saved
    // completion policy.  Unchecking the control restores those rows.
    const gatedStages = gatedTerminalStages();
    const inventory = displayed?.inventory || { projects: [], stages: [] };
    const eligibleStages = [];
    const stages = [];
    const stageByKey = new Map();
    inventory.stages.forEach((record) => {
      const key = stageKeyOf(record.stage);
      let dimension = stageByKey.get(key);
      if (!dimension) {
        dimension = { key, stage: record.stage, availability: record.availability };
        stageByKey.set(key, dimension);
        eligibleStages.push(dimension);
      } else if (record.availability === "incomplete") {
        dimension.availability = "incomplete";
      }
    });
    eligibleStages.forEach((dimension) => {
      if (gatedStages.has(dimension.stage)) return;
      stages.push(dimension);
    });

    const entryProjects = new Set(entries.map((entry) => entry.project));
    const hasEligibleStages = eligibleStages.length > 0;
    // Explain hidden finished work even when empty unfinished rows remain.
    const terminalHidden = (hasEligibleStages && stages.length === 0) ||
      (hideTerminalRows && displayed?.entries.length > 0 && entries.length === 0);
    const projects = [];
    inventory.projects.forEach((record) => {
      const projectStages = inventory.stages.filter((stage) => stage.project === record.name);
      const incompleteStage = projectStages.some((stage) =>
        stage.availability === "incomplete");
      const dimension = {
        key: record.name,
        project: record.name,
        availability: record.availability,
        incompleteStage,
      };
      if (!compactView || dimension.availability === "incomplete" || incompleteStage || entryProjects.has(record.name)) {
        projects.push(dimension);
      }
    });

    const populatedStages = new Set();
    const populatedProjects = new Set();
    entries.forEach((entry) => {
      const stage = stageKeyOf(entry.stage);
      if (stageByKey.has(stage)) populatedStages.add(stage);
      populatedProjects.add(entry.project);
    });
    const visibleStages = compactView
      ? stages.filter((stage) => stage.availability === "incomplete" || populatedStages.has(stage.key))
      : stages;
    const order = new Map(projectedStageNames().map((stage, index) => [stage, index]));
    visibleStages.sort((first, second) =>
      (order.get(first.key) ?? Number.MAX_SAFE_INTEGER) -
      (order.get(second.key) ?? Number.MAX_SAFE_INTEGER));
    const visibleProjects = compactView
      ? projects.filter((project) =>
        project.availability === "incomplete" || project.incompleteStage ||
          populatedProjects.has(project.key))
      : projects;
    return { stages: visibleStages, projects: visibleProjects, hasEligibleStages, terminalHidden };
  }

  function dimensionNotice(dimension) {
    return dimension.availability === "incomplete"
      ? '<span class="dimension-incomplete" title="Discovery is incomplete; this dimension may contain undiscovered work items">' +
        "incomplete / unavailable</span>"
      : "";
  }

  function workspaceDiagnostics(snapshot) {
    if (!snapshot) return [];
    const diagnostics = [...(snapshot.discovery_diagnostics || [])];
    [snapshot.identity_coverage, snapshot.program_coverage].forEach((coverage) => {
      diagnostics.push(...(coverage?.diagnostics || []));
    });
    return diagnostics;
  }

  function syncBoardIssues(panel = board.querySelector("#board-issues")) {
    const diagnostics = workspaceDiagnostics(displayed);
    // A checkouts failure is a card-scoped part beside the catalog's refresh
    // issue: it never changes the catalog, the button, focus or selection, and
    // it never reaches the live region.
    const checkoutsFailure = checkoutsOutcome.state === "failed"
      ? checkoutsOutcome.failure : null;
    if (!diagnostics.length && !refreshFailure && !checkoutsFailure) {
      panel?.remove();
      return;
    }
    // Each part is its own element so one issue stays addressable when both
    // kinds are present; the separator keeps the summary text unchanged.
    const summary = [
      diagnostics.length
        ? `<span class="workspace-issue-summary">${text(`Workspace issues: ${
          diagnostics.map((item) => item.code).join(", ")} (${diagnostics.length})`)}</span>` : "",
      refreshFailure
        ? `<span class="refresh-issue-summary">${text(`Latest refresh issue: ${refreshFailure}`)}</span>` : "",
      checkoutsFailure
        ? `<span class="checkouts-issue-summary">${text(`Code Status unavailable: ${checkoutsFailure}`)}</span>` : "",
    ].filter(Boolean).join(" · ");
    const issueItems = diagnostics.map((item) =>
      `<li><code>${text(item.code)}</code> ${text(item.message)}</li>`).join("");
    const refreshItem = refreshFailure
      ? `<li><code>refresh</code> ${text(refreshFailure)}</li>` : "";
    const checkoutsItem = checkoutsFailure
      ? `<li><code>checkouts</code> ${text(`Code Status unavailable: ${checkoutsFailure}`)}</li>` : "";
    if (!panel) {
      panel = document.createElement("details");
      panel.id = "board-issues";
      panel.className = "board-issues";
      panel.innerHTML = '<summary></summary><ul class="diagnostics"></ul>';
    }
    panel.querySelector("summary").innerHTML = summary;
    panel.querySelector("ul").innerHTML = issueItems + refreshItem + checkoutsItem;
    if (board.firstElementChild !== panel) board.prepend(panel);
  }

  function terminalHiddenHtml() {
    return '<p class="empty board-empty terminal-hidden">Finished rows are hidden. ' +
      'Uncheck “Hide terminal rows” to show them.</p>';
  }

  function emptyBoardHtml(axes, entries) {
    if (!displayed) return '<p class="empty board-empty">No work items in the catalog.</p>';
    if (!displayed.entries.length && !displayed.inventory.projects.length && !displayed.inventory.stages.length) {
      return '<p class="empty board-empty">No work items in the catalog.</p>';
    }
    // Name hidden finished rows before the generic empty-board explanations.
    if (axes.terminalHidden) {
      return terminalHiddenHtml();
    }
    const incomplete = displayed.inventory.projects.some((project) => project.availability === "incomplete") ||
      displayed.inventory.stages.some((stage) =>
        stage.availability === "incomplete");
    if (incomplete && !axes.stages.length) {
      return '<p class="empty board-empty incomplete-empty">Discovery is incomplete; unavailable dimensions remain hidden until they can be confirmed.</p>';
    }
    if (!axes.hasEligibleStages) {
      if (!compactView && displayed.inventory.projects.length) {
        return '<p class="empty board-empty no-eligible-stages">No eligible stage directories were found.</p>';
      }
      return '<p class="empty board-empty compact-hidden">Empty folders are hidden. Uncheck “Hide empty rows and columns” to show them.</p>';
    }
    if (compactView && !axes.stages.length && !axes.projects.length) {
      return '<p class="empty board-empty compact-hidden">Empty folders are hidden. Uncheck “Hide empty rows and columns” to show them.</p>';
    }
    if (!entries.length) {
      return '<p class="empty board-empty admitted-empty">The catalog contains admitted folders but no work items.</p>';
    }
    return '<p class="empty board-empty">No work items match the current board.</p>';
  }

  function renderBoard() {
    // Finished rows the reader is hiding are removed from the working entry set
    // (not just from the axes) so dependency edges, rail planning, and column
    // depths treat them as absent, exactly like an entry outside the board.
    const gatedStages = gatedTerminalStages();
    const entries = displayed
      ? displayed.entries.filter((entry) => !gatedStages.has(entry.stage))
      : [];
    const byId = indexByPackageId(entries);
    const dependentsOf = indexDependents(entries, byId);
    // A render shows the current lines for the snapshot it is given, so a
    // catalog change and the checkouts outcome never disagree about them.
    renderedEntries = new Map(entries.map((entry) => [entry.package_path, entry]));
    cardCodeStatus = codeStatusLineMap();
    // Keyed by path so cards sharing a package identity keep their own relationships.
    cardPrerequisites = new Map(entries.map((entry) => [
      entry.package_path,
      prerequisiteTargets(entry, byId)
        .filter(({target}) => target && target.package_id !== entry.package_id)
        .map(({target}) => target.package_id),
    ]));
    const axes = inventoryAxes(entries);
    const issues = board.querySelector("#board-issues");
    issues?.remove();
    const columns = axes.projects.map((project) => [project.key, project.project]);
    railColumns = columns.map(([key]) => key);
    railPlan = new Map();
    railEdges = [];
    const setBoardColumns = (gutters) => {
      const widths = gutters.map((gutter) => `calc(var(--card-min-width) + ${gutter}px)`);
      board.style.setProperty("--column-tracks", widths.map((width) =>
        `minmax(${width}, 1fr)`).join(" "));
      board.style.setProperty("--board-min-width", widths.length
        ? `calc(var(--label-min-width) + ${widths.join(" + ")} + ` +
          `${columns.length * .9}rem + var(--board-horizontal-padding))`
        : "100vw");
    };
    if (!axes.stages.length && axes.projects.length && !axes.terminalHidden) {
      setBoardColumns(columns.map(() => 0));
      board.innerHTML = '<h2 class="board-corner" aria-hidden="true"></h2>' +
        axes.projects.map((project) => `<h2 class="column-head">${text(project.project)}${dimensionNotice(project)}</h2>`).join("") +
        '<p class="empty board-empty no-eligible-stages">No eligible stage directories were found.</p>';
      syncBoardIssues(issues);
      return;
    }
    if (!axes.stages.length || !axes.projects.length) {
      setBoardColumns([]);
      board.innerHTML = emptyBoardHtml(axes, entries);
      syncBoardIssues(issues);
      return;
    }
    railEdges = planEdges(entries, byId);
    const plans = new Map(columns.map(([key]) => {
      const columnEntries = entries.filter((entry) => columnKeyOf(entry) === key);
      const depth = depthForColumn(columnEntries, railEdges);
      const cells = new Map();
      const positions = new Map();
      const bandSlots = new Map();
      let position = 0;
      axes.stages.forEach((stage) => {
        bandSlots.set(rowDataKeyOf(stage.stage), position++);
        const ordered = orderCell(columnEntries.filter((entry) =>
          stageKeyOf(entry.stage) === stage.key), depth);
        cells.set(stage.key, ordered);
        ordered.forEach((entry) => positions.set(entry.package_id, position++));
      });
      const intervals = railEdges.filter((edge) =>
        edge.sourceColumn === key || edge.dependentColumn === key).map((edge) => {
        const local = edge.sourceColumn === edge.dependentColumn;
        const first = local ? positions.get(edge.source) : bandSlots.get(edge.row);
        const last = positions.get(edge.dependentColumn === key ? edge.dependent : edge.source);
        return {edge, start: Math.min(first, last), end: Math.max(first, last)};
      });
      intervals.sort((a, b) => a.start - b.start || a.end - b.end ||
        a.edge.source.localeCompare(b.edge.source) || a.edge.dependent.localeCompare(b.edge.dependent));
      const ends = [];
      const lanes = new Map();
      intervals.forEach(({edge, start, end}) => {
        // Closed intervals sharing a card or band position cannot share a lane.
        let lane = ends.findIndex((lastEnd) => lastEnd < start);
        if (lane < 0) {
          if (ends.length === RAIL_LANE_CAP) return;
          lane = ends.length;
        }
        ends[lane] = end;
        lanes.set(`${edge.source}>${edge.dependent}`, lane);
      });
      railPlan.set(key, {lanes});
      return [key, {cells, reserved: ends.length}];
    }));
    const bands = new Map();
    railEdges.forEach((edge) => {
      const pair = `${edge.source}>${edge.dependent}`;
      edge.drawn = railPlan.get(edge.sourceColumn).lanes.has(pair) &&
        railPlan.get(edge.dependentColumn).lanes.has(pair);
      if (!edge.drawn || edge.sourceColumn === edge.dependentColumn) return;
      edge.bandLane = bands.get(edge.row) || 0;
      bands.set(edge.row, edge.bandLane + 1);
    });
    setBoardColumns(columns.map(([key]) => {
      const reserved = plans.get(key).reserved;
      return reserved ? reserved * RAIL_PITCH + RAIL_INSET : 0;
    }));
    let html = (!entries.length
      ? (axes.terminalHidden ? terminalHiddenHtml()
        : `<p class="empty board-empty admitted-empty">${axes.stages.some((stage) => stage.availability === "incomplete")
        ? "The catalog has incomplete dimensions; no work items are currently available."
        : "The catalog contains admitted folders but no work items."}</p>`) : "") +
      '<h2 class="board-corner" aria-hidden="true"></h2>' +
      axes.projects.map((project) => `<h2 class="column-head">${text(project.project)}${dimensionNotice(project)}</h2>`).join("");
    axes.stages.forEach((stage) => {
      const key = stage.key;
      const rowKey = rowDataKeyOf(stage.stage);
      const cells = columns.map(([columnKey]) => {
        const { cells: orderedCells, reserved } = plans.get(columnKey);
        const gutter = reserved ? reserved * RAIL_PITCH + RAIL_INSET : 0;
        return cellHtml(
          orderedCells.get(key),
          byId,
          dependentsOf,
          gutter,
        );
      }).join("");
      const band = bands.has(rowKey)
        ? `<span class="connection-band" aria-hidden="true" ` +
          `style="height:${bands.get(rowKey) * RAIL_PITCH + RAIL_INSET}px"></span>` : "";
      html += `<section class="board-row" data-lifecycle="${text(rowDataKeyOf(stage.stage))}">${band}` +
        `<h2 class="row-head">${text(stageLabelOf(stage.stage))}${dimensionNotice(stage)}</h2>${cells}</section>`;
    });
    board.innerHTML = html;
    syncBoardIssues(issues);
    applyFilter();
  }

  function applyFilter() {
    const query = filter.value.trim().toLowerCase();
    // The one query applies to the shown view: a checkouts group stays while
    // its served names, branches or work items match, and is hidden otherwise.
    if (shownView === VIEW_CHECKOUTS) {
      checkoutsBody.querySelectorAll(".checkouts-group").forEach((group) => {
        const search = group.dataset.search || "";
        group.hidden = Boolean(query) && !search.includes(query);
      });
      return;
    }
    board.querySelectorAll(".card").forEach((card) => {
      card.hidden = Boolean(query) && !card.dataset.search.includes(query);
    });
    board.querySelectorAll(".cell").forEach((cell) => {
      const cards = [...cell.querySelectorAll(".card")];
      cell.classList.toggle("filtered-empty", cards.length > 0 && cards.every((card) => card.hidden));
    });
    drawRails();
  }

  // Every settled checkouts outcome reaches the rendered cards directly: only
  // each card's Code Status line and search text change, so keyboard focus,
  // selection, the issues panel's open state and the scroll position stay, and
  // one filter pass redraws the rails.  Rebuilding the board here would lose
  // all four, and a line that no card's path holds is simply absent.
  function updateCardCodeStatus() {
    cardCodeStatus = codeStatusLineMap();
    let changed = false;
    board.querySelectorAll(".card[data-package-path]").forEach((card) => {
      const path = card.dataset.packagePath;
      const value = cardCodeStatus.get(path);
      const shown = card.querySelector(".card-code-status");
      if (value === undefined) {
        if (shown) {
          shown.remove();
          changed = true;
        }
      } else if (shown) {
        const label = `Code Status: ${value}`;
        if (shown.textContent !== label) {
          shown.textContent = label;
          changed = true;
        }
      } else {
        const line = document.createElement("span");
        line.className = "card-code-status";
        line.textContent = `Code Status: ${value}`;
        card.querySelector(".card-filepath").after(line);
        changed = true;
      }
      const entry = renderedEntries.get(path);
      if (entry) {
        const search = searchText(entry, value);
        if (card.dataset.search !== search) {
          card.dataset.search = search;
          changed = true;
        }
      }
    });
    if (changed) applyFilter();
  }

  function drawRails() {
    board.querySelector(".rail-layer")?.remove();
    if (!displayed) return;
    const heads = [...board.querySelectorAll(".column-head")];
    const boardRect = board.getBoundingClientRect();
    const cards = new Map();
    board.querySelectorAll(".card[data-package-id]").forEach((card) => {
      if (!card.hidden) cards.set(card.dataset.packageId, card);
    });
    const visibleEdges = railEdges.filter((edge) =>
      cards.has(edge.source) && cards.has(edge.dependent));
    // A prerequisite counts while its card is visible, whether it is shown by
    // an arrow or only as relationship text.
    board.querySelectorAll(".card").forEach((card) => {
      const prerequisites = cardPrerequisites.get(card.dataset.packagePath) || [];
      const noIncoming = !card.hidden && !prerequisites.some((id) => cards.has(id));
      card.classList.toggle("no-incoming-arrow", noIncoming);
      card.querySelector(".card-start-text").textContent = noIncoming ? "No prerequisite shown." : "";
    });
    const drawnEdges = visibleEdges.filter((edge) => edge.drawn);
    if (!drawnEdges.length) return;
    const selected = [...cards.values()].find((card) => card.dataset.packagePath === selectedPath);
    const selectedId = selected?.dataset.packageId;
    const related = (edge) => edge.source === selectedId || edge.dependent === selectedId;
    const bands = new Map([...board.querySelectorAll(".board-row")].map((row) =>
      [row.dataset.lifecycle, row.querySelector(".connection-band")]));
    const laneX = (key, edge) => {
      const columnLeft = heads[railColumns.indexOf(key)].offsetLeft;
      return columnLeft + (railPlan.get(key).lanes.get(`${edge.source}>${edge.dependent}`) + .5) * RAIL_PITCH;
    };
    const port = (id, edge) => {
      const rect = cards.get(id).getBoundingClientRect();
      const incident = drawnEdges.filter((item) => item.source === id || item.dependent === id);
      return {
        x: rect.left - boardRect.left,
        y: rect.top - boardRect.top + 12 +
          (rect.height - 24) * (incident.indexOf(edge) + 1) / (incident.length + 1),
      };
    };
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "rail-layer");
    svg.setAttribute("aria-hidden", "true");
    const width = Math.max(board.scrollWidth, boardRect.width);
    const height = Math.max(board.scrollHeight, boardRect.height);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.style.width = `${width}px`;
    svg.style.height = `${height}px`;
    svg.innerHTML = '<defs>' + ["normal", "emphasized"].map((kind) =>
      `<marker id="rail-arrow-${kind}" viewBox="0 0 10 10" refX="10" refY="5" ` +
      'markerWidth="10" markerHeight="10" markerUnits="userSpaceOnUse" orient="auto">' +
      `<path class="arrow-${kind}" d="M0 0L10 5L0 10Z"></path></marker>`).join("") + '</defs>';
    // Draw emphasized connections last so their crossings remain easy to follow.
    [...drawnEdges].sort((a, b) => Number(related(a)) - Number(related(b))).forEach((edge) => {
      const start = port(edge.source, edge);
      const end = port(edge.dependent, edge);
      const sourceX = laneX(edge.sourceColumn, edge);
      const dependentX = laneX(edge.dependentColumn, edge);
      let route = `M ${start.x - 1} ${start.y} H ${sourceX}`;
      if (edge.sourceColumn !== edge.dependentColumn) {
        const bandY = bands.get(edge.row).getBoundingClientRect().top - boardRect.top +
          RAIL_INSET / 2 + (edge.bandLane + .5) * RAIL_PITCH;
        route += ` V ${bandY} H ${dependentX}`;
      }
      route += ` V ${end.y} H ${end.x - 5}`;
      const emphasized = selectedId && related(edge);
      const group = document.createElementNS(SVG_NS, "g");
      group.setAttribute("class", "connection" +
        (selectedId ? emphasized ? " emphasized" : " muted" : ""));
      group.dataset.source = edge.source;
      group.dataset.dependent = edge.dependent;
      const halo = document.createElementNS(SVG_NS, "path");
      halo.setAttribute("class", "rail-halo");
      halo.setAttribute("d", route);
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("class", "rail");
      path.setAttribute("d", route);
      path.setAttribute("marker-end", `url(#rail-arrow-${emphasized ? "emphasized" : "normal"})`);
      group.append(halo, path);
      svg.append(group);
    });
    board.append(svg);
  }

  function select(path) {
    selectedPath = selectedPath === path ? null : path;
    board.querySelectorAll(".card").forEach((card) => {
      const selected = card.dataset.packagePath === selectedPath;
      card.classList.toggle("selected", selected);
      card.setAttribute("aria-pressed", String(selected));
    });
    drawRails();
  }

  function digestOf(snapshot) {
    return snapshot && typeof snapshot.catalog_digest === "string" ? snapshot.catalog_digest : "";
  }

  function exactKeys(value, expected) {
    return value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).sort().join("\u0000") === expected.slice().sort().join("\u0000");
  }

  function protocol(condition) {
    if (!condition) throw new Error("producer_protocol_error");
  }

  // Python compares Unicode strings by scalar value; JavaScript relational
  // operators compare UTF-16 code units. Protocol order must use one scalar
  // comparator for every producer-canonical string sequence.
  function scalarCompare(first, second) {
    const left = [...first];
    const right = [...second];
    const length = Math.min(left.length, right.length);
    for (let index = 0; index < length; index += 1) {
      const leftCode = left[index].codePointAt(0);
      const rightCode = right[index].codePointAt(0);
      if (leftCode !== rightCode) return leftCode - rightCode;
    }
    return left.length - right.length;
  }

  function scalarDiagnosticCompare(first, second) {
    return scalarCompare(first.code, second.code) || scalarCompare(first.message, second.message);
  }

  function safeText(value, nullable = false) {
    return (nullable && value === null) ||
      (typeof value === "string" && value.length > 0 && value.length <= 1024 &&
        ![...value].some((character) => {
          const code = character.codePointAt(0);
          return code < 32 || code === 127 || (code >= 0xd800 && code <= 0xdfff);
        }));
  }

  function component(value) {
    return safeText(value) && value !== "." && value !== ".." &&
      !value.includes("/") && !value.includes("\\");
  }

  function declaredText(value) {
    return value === null || (typeof value === "string" &&
      (value === "" || safeText(value)));
  }

  function diagnostics(value) {
    protocol(Array.isArray(value));
    value.forEach((item) => {
      protocol(exactKeys(item, ["code", "message"]) && safeText(item.code) && safeText(item.message));
    });
    protocol(value.every((item, index) => index === 0 ||
      scalarDiagnosticCompare(item, value[index - 1]) >= 0));
  }

  function uuid(value, nullable = false) {
    protocol((nullable && value === null) ||
      (typeof value === "string" &&
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)));
  }

  function provenance(value, nullable = false) {
    protocol((nullable && value === null) ||
      (typeof value === "string" &&
        /^(?:git-object-sha1:[0-9a-f]{40}|git-object-sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$/.test(value)));
  }

  function relationship(value) {
    protocol(exactKeys(value, ["claims", "direct_prerequisite_state", "participation",
      "prerequisites", "program", "superseded_by"]));
    protocol(["available", "legacy", "invalid"].includes(value.participation));
    protocol(["satisfied", "unsatisfied", "unknown", "no_declared_prerequisites",
      "relationship_unavailable"].includes(value.direct_prerequisite_state));
    protocol(Array.isArray(value.claims));
    value.claims.forEach((claim) => {
      protocol(exactKeys(claim, ["evidence_ref", "name", "state"]));
      protocol(/^[a-z][a-z0-9-]{0,63}$/.test(claim.name));
      protocol(["satisfied", "unsatisfied", "unknown"].includes(claim.state));
      provenance(claim.evidence_ref, true);
    });
    protocol(value.claims.every((claim, index) => index === 0 || claim.name >= value.claims[index - 1].name));
    protocol(Array.isArray(value.prerequisites));
    value.prerequisites.forEach((edge) => {
      protocol(typeof edge.kind === "string");
      if (edge.kind === "claim") {
        protocol(exactKeys(edge, ["kind", "claim_name", "observed_evidence_ref", "observed_state",
          "reason", "resolved_state", "target_package_id"]));
        uuid(edge.target_package_id, true);
        protocol(edge.claim_name === null || /^[a-z][a-z0-9-]{0,63}$/.test(edge.claim_name));
        protocol(edge.observed_state === null || ["satisfied", "unsatisfied", "unknown"].includes(edge.observed_state));
        provenance(edge.observed_evidence_ref, true);
        protocol(CLAIM_EDGE_REASONS.has(edge.reason));
      } else if (edge.kind === "completion") {
        protocol(exactKeys(edge, ["kind", "observed_stage", "reason", "resolved_state", "target_package_id"]));
        uuid(edge.target_package_id, true);
        protocol(edge.observed_stage === null || component(edge.observed_stage));
        protocol(COMPLETION_EDGE_REASONS.has(edge.reason));
      } else {
        protocol(false);
      }
      protocol(["satisfied", "unsatisfied", "unknown"].includes(edge.resolved_state));
    });
    protocol(exactKeys(value.program, ["program_id", "resolution", "title"]));
    uuid(value.program.program_id, true);
    protocol(["not_declared", "resolved", "unknown"].includes(value.program.resolution));
    protocol(value.program.title === null || safeText(value.program.title));
    protocol(exactKeys(value.superseded_by, ["package_id", "resolution"]));
    uuid(value.superseded_by.package_id, true);
    protocol(["not_declared", "resolved", "unknown"].includes(value.superseded_by.resolution));
  }

  function asciiString(value) {
    return JSON.stringify(value).replace(/[^\x00-\x7f]/g,
      (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`);
  }

  function canonicalJson(value) {
    if (typeof value === "string") return asciiString(value);
    if (value === null || typeof value === "number" || typeof value === "boolean") {
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
    return `{${Object.keys(value).sort().map((key) => `${asciiString(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }

  async function authenticate(snapshot, digestKey, omitted) {
    const unsigned = {...snapshot};
    omitted.forEach((key) => delete unsigned[key]);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonicalJson(unsigned)));
    const actual = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    protocol(actual === snapshot[digestKey]);
    return snapshot;
  }

  // The one relative-package-path rule both documents use: bounded text of the
  // same shape as the producer's normalized relative POSIX path.
  function relativePackagePath(path) {
    return safeText(path) &&
      !(path.startsWith("/") || path.includes("\\") ||
        path.split("/").some((part) => !part || part === "." || part === ".."));
  }

  function validateSnapshot(snapshot) {
    const top = ["catalog_digest", "configuration_revision", "discovery_diagnostics", "entries", "identity_coverage",
      "inventory", "program_coverage", "programs", "schema_version", "terminal_stages"];
    protocol(exactKeys(snapshot, top) && snapshot.schema_version === SCHEMA_VERSION &&
      /^[0-9a-f]{64}$/.test(snapshot.catalog_digest) && Array.isArray(snapshot.entries) &&
      Array.isArray(snapshot.programs));
    protocol(snapshot.configuration_revision === null || safeText(snapshot.configuration_revision));
    protocol(exactKeys(snapshot.inventory, ["projects", "stages"]) &&
      Array.isArray(snapshot.inventory.projects) && Array.isArray(snapshot.inventory.stages));
    const inventoryProjects = new Set();
    snapshot.inventory.projects.forEach((project) => {
      protocol(exactKeys(project, ["availability", "name"]) && component(project.name) &&
        ["complete", "incomplete"].includes(project.availability) &&
        !inventoryProjects.has(project.name));
      inventoryProjects.add(project.name);
    });
    protocol(snapshot.inventory.projects.every((project, index) => index === 0 ||
      scalarCompare(project.name, snapshot.inventory.projects[index - 1].name) > 0));
    const inventoryStages = new Set();
    snapshot.inventory.stages.forEach((stage) => {
      const key = `${stage.project}\u0000${stage.stage}`;
      protocol(exactKeys(stage, ["availability", "project", "stage"]) &&
        component(stage.project) && component(stage.stage) && inventoryProjects.has(stage.project) &&
        ["complete", "incomplete"].includes(stage.availability) && !inventoryStages.has(key));
      inventoryStages.add(key);
    });
    protocol(snapshot.inventory.stages.every((stage, index) => index === 0 ||
      scalarCompare(stage.project, snapshot.inventory.stages[index - 1].project) > 0 ||
      (stage.project === snapshot.inventory.stages[index - 1].project &&
        scalarCompare(stage.stage, snapshot.inventory.stages[index - 1].stage) > 0)));
    protocol(Array.isArray(snapshot.terminal_stages));
    // Terminal stages are the account's completion policy, carried for the
    // reversible "Hide terminal rows" grouping.
    snapshot.terminal_stages.forEach((stage) => protocol(component(stage)));
    protocol(snapshot.terminal_stages.every((stage, index) =>
      index === 0 || scalarCompare(stage, snapshot.terminal_stages[index - 1]) > 0));
    diagnostics(snapshot.discovery_diagnostics);
    [snapshot.identity_coverage, snapshot.program_coverage].forEach((coverage) => {
      protocol(exactKeys(coverage, ["diagnostics", "state"]) && ["complete", "incomplete"].includes(coverage.state));
      diagnostics(coverage.diagnostics);
    });
    snapshot.programs.forEach((program) => {
      protocol(exactKeys(program, ["member_package_ids", "program_id", "title"]));
      uuid(program.program_id);
      protocol(safeText(program.title));
      protocol(Array.isArray(program.member_package_ids));
      program.member_package_ids.forEach((member) => uuid(member));
    });
    const paths = [];
    snapshot.entries.forEach((entry) => {
      protocol(exactKeys(entry, ["declared", "reported_fields", "package_id", "package_path",
        "project", "relationship", "stage"]));
      uuid(entry.package_id, true);
      protocol(component(entry.project) && component(entry.stage) &&
        inventoryStages.has(`${entry.project}\u0000${entry.stage}`) &&
        relativePackagePath(entry.package_path) &&
        (entry.package_path === `${entry.project}/${entry.stage}` ||
          entry.package_path.startsWith(`${entry.project}/${entry.stage}/`)));
      protocol(exactKeys(entry.declared, DECLARED_FIELDS));
      Object.values(entry.declared).forEach((value) => protocol(declaredText(value)));
      protocol(Array.isArray(entry.reported_fields) && entry.reported_fields.length <= MAX_REPORTED_FIELDS);
      const reportedNames = new Set();
      entry.reported_fields.forEach((field, index) => {
        protocol(exactKeys(field, REPORTED_FIELD_KEYS) && safeText(field.name) && safeText(field.value));
        const folded = field.name.replace(/[A-Z]/g, (character) => character.toLowerCase());
        protocol(!reportedNames.has(folded) && (index === 0 ||
          scalarCompare(field.name, entry.reported_fields[index - 1].name) > 0));
        reportedNames.add(folded);
      });
      relationship(entry.relationship);

      paths.push(entry.package_path);
    });
    protocol(paths.every((path, index) => index === 0 ||
      scalarCompare(path, paths[index - 1]) >= 0) &&
      new Set(paths).size === paths.length);
    return authenticate(snapshot, "catalog_digest", ["catalog_digest"]);
  }

  function setPending(available) {
    refreshButton.dataset.pending = String(available);
    refreshButton.classList.toggle("pending", available);
    syncViewLabels();
  }

  function readViewPreference() {
    shownView = VIEW_BOARD;
    try {
      const value = window.localStorage.getItem(VIEW_STORAGE_KEY);
      if (value === VIEW_BOARD || value === VIEW_CHECKOUTS) shownView = value;
    } catch (_) {
      // Board is the safe fallback when browser storage is unavailable.
    }
  }

  function syncViewLabels() {
    refreshButton.textContent = pending && shownView === VIEW_BOARD
      ? "Apply update" : "Refresh view";
    viewBoardButton.textContent = pending && shownView === VIEW_CHECKOUTS
      ? "Board (update waiting)" : "Board";
    viewBoardButton.setAttribute("aria-pressed", String(shownView === VIEW_BOARD));
    viewCheckoutsButton.setAttribute("aria-pressed", String(shownView === VIEW_CHECKOUTS));
  }

  // Exactly one view is shown.  Switching keeps focus on the pressed switch
  // button and never applies a waiting catalog: the board keeps its pending
  // snapshot, and the switch names it while the checkouts view is shown.
  function applyView() {
    const checkoutsShown = shownView === VIEW_CHECKOUTS;
    board.hidden = checkoutsShown;
    checkoutsView.hidden = !checkoutsShown;
    compactControl.closest("label").hidden = checkoutsShown;
    terminalControl.closest("label").hidden = checkoutsShown;
    quietCheckoutsLabel.hidden = !checkoutsShown;
    filter.placeholder = checkoutsShown
      ? "Find a checkout, branch, or work item…"
      : "Find a work item, project, or program…";
    syncViewLabels();
    // The shown view applies the shared query; switching views re-runs it.
    applyFilter();
  }

  function setShownView(value) {
    shownView = value;
    try { window.localStorage.setItem(VIEW_STORAGE_KEY, value); } catch (_) { /* fallback is in-memory */ }
    applyView();
  }

  function readQuietCheckoutsPreference() {
    hideQuietCheckouts = true;
    try {
      const value = window.localStorage.getItem(QUIET_CHECKOUTS_STORAGE_KEY);
      if (value === "true") hideQuietCheckouts = true;
      if (value === "false") hideQuietCheckouts = false;
    } catch (_) {
      // A checked box is the safe fallback when browser storage is unavailable.
    }
    quietCheckoutsControl.checked = hideQuietCheckouts;
  }

  function setQuietCheckoutsPreference(value) {
    hideQuietCheckouts = value;
    try { window.localStorage.setItem(QUIET_CHECKOUTS_STORAGE_KEY, String(value)); } catch (_) { /* fallback is in-memory */ }
    quietCheckoutsControl.checked = hideQuietCheckouts;
    // The preference changes which groups the view shows, so the view follows
    // it at once without waiting for the next tick.
    renderCheckouts();
  }

  function settingsErrorMessage(error) {
    return error instanceof Error && error.message ? error.message : "settings_unavailable";
  }

  function parseSettings(payload, allowOutcome = false) {
    protocol(payload && typeof payload === "object" && !Array.isArray(payload));
    const expected = allowOutcome
      ? ["completed", "order", "outcome", "revision"]
      : ["completed", "order", "revision"];
    protocol(exactKeys(payload, expected));
    protocol(Array.isArray(payload.order) && payload.order.every((stage) => component(stage)));
    protocol(new Set(payload.order).size === payload.order.length);
    protocol(Array.isArray(payload.completed) && payload.completed.every((stage) => component(stage)));
    protocol(new Set(payload.completed).size === payload.completed.length);
    protocol((allowOutcome && payload.revision === null) ||
      (typeof payload.revision === "string" && payload.revision.length > 0));
    if (allowOutcome) {
      protocol(["success", "conflict", "failure", "reload-needed"].includes(payload.outcome));
    }
    return payload;
  }

  function settingsStatus(message, isError = false) {
    stageOrderStatus.textContent = message;
    stageOrderStatus.classList.toggle("error", isError);
  }

  function loadSettings({refreshCatalog = false} = {}) {
    const serial = ++settingsRequestSerial;
    settingsBusy = true;
    settingsAvailable = false;
    settingsRevision = null;
    renderStageOrderEditor();
    fetch(SETTINGS_ROUTE, {cache: "no-store"})
      .then(async (response) => {
        let payload = null;
        try { payload = await response.json(); } catch (_) { /* handled below */ }
        if (response.status === 404) return null;
        if (!response.ok || !payload || payload.error) {
          throw new Error((payload && payload.error) || "settings_unavailable");
        }
        return parseSettings(payload);
      })
      .then((payload) => {
        if (serial !== settingsRequestSerial) return;
        if (!payload) {
          settingsAvailable = false;
          settingsReloadAvailable = false;
          settingsRevision = null;
          renderStageOrderEditor();
          return;
        }
        settingsAvailable = true;
        settingsReloadAvailable = false;
        savedStageOrder = [...payload.order];
        savedCompletedStages = [...payload.completed];
        settingsRevision = payload.revision;
        editorStageOrder = editorNames();
        editorCompletedStages = [...savedCompletedStages];
        settingsStatus("Current board settings loaded.");
        renderStageOrderEditor();
        if (displayed) renderBoard();
        if (refreshCatalog) request("manual");
      })
      .catch((error) => {
        if (serial !== settingsRequestSerial) return;
        settingsAvailable = false;
        settingsReloadAvailable = true;
        settingsRevision = null;
        settingsStatus(`Could not load board settings: ${settingsErrorMessage(error)}`, true);
        renderStageOrderEditor();
      }).finally(() => {
        if (serial !== settingsRequestSerial) return;
        settingsBusy = false;
        renderStageOrderEditor();
      });
  }

  function saveStageOrder(order, completed) {
    if (!settingsAvailable || !settingsRevision || settingsBusy) return;
    settingsBusy = true;
    settingsStatus("Saving board settings…");
    renderStageOrderEditor();
    fetch(SETTINGS_ROUTE, {
      method: "PUT",
      cache: "no-store",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({revision: settingsRevision, order, completed}),
    }).then(async (response) => {
      let payload = null;
      try { payload = await response.json(); } catch (_) { /* handled below */ }
      if (!payload || payload.error) {
        throw new Error((payload && payload.error) || "settings_unavailable");
      }
      const outcome = parseSettings(payload, true);
      if (!response.ok && !["conflict", "failure", "reload-needed"].includes(outcome.outcome)) {
        throw new Error("settings_unavailable");
      }
      return outcome;
    }).then((payload) => {
      if (payload.outcome !== "success") {
        const message = payload.outcome === "conflict" || payload.outcome === "reload-needed"
          ? `Save not applied: ${payload.outcome}. Reload board settings to replace this unsaved draft and try again.`
          : "Save failed: the board settings were not persisted.";
        if (payload.outcome === "conflict" || payload.outcome === "reload-needed") {
          settingsAvailable = false;
          settingsReloadAvailable = true;
          settingsRevision = null;
        }
        settingsStatus(message, true);
        return;
      }
      savedStageOrder = [...payload.order];
      savedCompletedStages = [...payload.completed];
      settingsRevision = payload.revision;
      editorStageOrder = editorNames();
      editorCompletedStages = [...savedCompletedStages];
      settingsStatus("Board settings saved.");
      renderStageOrderEditor();
      request("settings", payload.revision);
    }).catch((error) => {
      settingsStatus(`Save failed: ${settingsErrorMessage(error)}`, true);
      renderStageOrderEditor();
    }).finally(() => {
      settingsBusy = false;
      renderStageOrderEditor();
    });
  }

  function apply(snapshot) {
    const priorEditor = editorStageOrder.length ? [...editorStageOrder] : [];
    const retainDraft = priorEditor.length > 0 && hasUnsavedStageOrder();
    displayed = snapshot;
    pending = null;
    refreshFailure = null;
    document.querySelector("#refresh-status").textContent = "";
    setPending(false);
    editorStageOrder = [...new Set([
      ...(retainDraft ? priorEditor : editorNames(snapshot)),
      ...inventoryStageNames(snapshot),
    ])];
    if (!retainDraft) editorCompletedStages = [...savedCompletedStages];
    if (selectedPath && !snapshot.entries.some((entry) =>
      entry.package_path === selectedPath)) {
      selectedPath = null;
    }
    renderBoard();
    // The status line names how the scan and the board's settings differ, so a
    // newly applied catalog re-renders it.
    renderCheckoutsStatusLine();
    renderStageOrderEditor();
    if (!workspace || workspace.revision !== snapshot.configuration_revision) loadWorkspace();
    // Settings saved from another page change the catalog revision; an unsaved
    // draft is kept and must be replaced explicitly rather than discarded.
    if (snapshot.configuration_revision === null || settingsRevision === null || settingsBusy ||
      snapshot.configuration_revision === settingsRevision) return;
    if (!retainDraft) {
      loadSettings();
      return;
    }
    settingsAvailable = false;
    settingsReloadAvailable = true;
    settingsRevision = null;
    settingsStatus("Board settings changed elsewhere. Reload board settings to replace this unsaved draft.", true);
    renderStageOrderEditor();
  }

  function loadWorkspace() {
    const serial = ++workspaceRequest;
    workspace = null;
    fetch(WORKSPACE_ROUTE, {cache: "no-store"})
      .then((response) => response.ok ? response.json() : null)
      .then((value) => {
        if (serial !== workspaceRequest || !value ||
          !exactKeys(value, ["root", "revision"]) ||
          typeof value.root !== "string" || !value.root ||
          typeof value.revision !== "string" || !value.revision) return;
        workspace = value;
      }).catch(() => { /* Copy feedback is shown when requested. */ });
  }

  function fullDirectoryPath(root, relative) {
    const windows = /^[A-Za-z]:[\\/]/.test(root) || root.startsWith("\\\\");
    const separator = windows ? "\\" : "/";
    return root + (root.endsWith("/") || root.endsWith("\\") ? "" : separator) +
      (windows ? relative.replaceAll("/", "\\") : relative);
  }

  function showCopyStatus(message, anchor, isError = false) {
    if (copyStatusTimer !== null) window.clearTimeout(copyStatusTimer);
    copyStatus.textContent = message;
    copyStatus.classList.toggle("error", isError);
    copyStatus.classList.add("visible");
    const bubble = copyStatus.getBoundingClientRect();
    const margin = 8;
    const gap = 10;
    const right = anchor.right + gap;
    const left = anchor.left - bubble.width - gap;
    const x = right + bubble.width <= window.innerWidth - margin ? right
      : left >= margin ? left
        : Math.max(margin, Math.min(anchor.left, window.innerWidth - bubble.width - margin));
    const y = Math.max(margin, Math.min(anchor.top + margin,
      window.innerHeight - bubble.height - margin));
    copyStatus.style.left = `${x}px`;
    copyStatus.style.top = `${y}px`;
    copyStatusTimer = window.setTimeout(() => {
      copyStatus.classList.remove("visible");
      copyStatusTimer = null;
    }, isError ? 4000 : 2000);
  }

  function readCompactPreference() {
    compactView = true;
    try {
      const value = window.localStorage.getItem(COMPACT_STORAGE_KEY);
      if (value === "true") compactView = true;
      if (value === "false") compactView = false;
    } catch (_) {
      // A checked checkbox is the safe fallback when browser storage is unavailable.
    }
    compactControl.checked = compactView;
  }

  function setCompactPreference(value) {
    compactView = value;
    try { window.localStorage.setItem(COMPACT_STORAGE_KEY, String(value)); } catch (_) { /* fallback is in-memory */ }
    compactControl.checked = compactView;
    if (displayed) {
      renderBoard();
    }
  }

  // Terminal-row preference: same contract as the compact preference.  It is
  // stored per browser, never sent to the server, and defaults to checked so
  // finished rows start hidden.  Storage failures fall back to the in-memory
  // value for this page and back to checked on a fresh page.
  function readTerminalPreference() {
    hideTerminalRows = true;
    try {
      const value = window.localStorage.getItem(TERMINAL_STORAGE_KEY);
      if (value === "true") hideTerminalRows = true;
      if (value === "false") hideTerminalRows = false;
    } catch (_) {
      // A checked checkbox is the safe fallback when browser storage is unavailable.
    }
    terminalControl.checked = hideTerminalRows;
  }

  function setTerminalPreference(value) {
    hideTerminalRows = value;
    try { window.localStorage.setItem(TERMINAL_STORAGE_KEY, String(value)); } catch (_) { /* fallback is in-memory */ }
    terminalControl.checked = hideTerminalRows;
    if (displayed) {
      renderBoard();
    }
  }

  function readDeselectPreference() {
    deselectOnEmptyClick = false;
    try {
      deselectOnEmptyClick = window.localStorage.getItem(DESELECT_STORAGE_KEY) === "true";
    } catch (_) { /* unchecked is the fallback when storage is unavailable */ }
    deselectControl.checked = deselectOnEmptyClick;
  }

  function setDeselectPreference(value) {
    deselectOnEmptyClick = value;
    try { window.localStorage.setItem(DESELECT_STORAGE_KEY, String(value)); } catch (_) { /* fallback is in-memory */ }
  }

  // Only the surface's own prefix is accepted, so one provider's failure can
  // never be displayed as the other's.
  function safeCategory(error, prefix, fallback) {
    const code = error && typeof error.message === "string" ? error.message : "";
    return SAFE_CATEGORIES.includes(code) && code.startsWith(prefix) ? code : fallback;
  }

  // A UTC timestamp in the one shape the collector and the runtime write.
  function utcTimestamp(value) {
    if (typeof value !== "string" ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) return false;
    const parsed = new Date(value);
    return !Number.isNaN(parsed.getTime()) && parsed.toISOString() === `${value.slice(0, 19)}.000Z`;
  }

  // The schedule the status line reads: the check state, its interval, when the
  // next check is due, and the last run's times, outcome and repositories.
  function validateRemoteChecks(value) {
    protocol(exactKeys(value, ["state", "interval_seconds", "next_due_at", "last"]));
    protocol(["idle", "running"].includes(value.state));
    protocol(typeof value.interval_seconds === "number" &&
      Number.isFinite(value.interval_seconds) && value.interval_seconds > 0);
    protocol(value.next_due_at === null || utcTimestamp(value.next_due_at));
    protocol(value.last === null || exactKeys(value.last,
      ["started_at", "finished_at", "outcome", "repositories"]));
    if (value.last !== null) {
      protocol(utcTimestamp(value.last.started_at) && utcTimestamp(value.last.finished_at));
      protocol(value.last.outcome === "completed" ||
        (typeof value.last.outcome === "string" &&
          value.last.outcome.startsWith("checkouts_") &&
          SAFE_CATEGORIES.includes(value.last.outcome)));
      protocol(Array.isArray(value.last.repositories));
    }
  }

  // The view reads the envelope shape, the document's identity, the package
  // paths and Code Status text, and the remote-check schedule; every other
  // subtree stays the server's, which parsed the whole document before
  // serving it.
  async function validateCheckouts(payload) {
    protocol(exactKeys(payload, ["checkouts", "remote_checks"]));
    validateRemoteChecks(payload.remote_checks);
    const document = payload.checkouts;
    protocol(exactKeys(document, CHECKOUTS_KEYS) &&
      document.schema_version === CHECKOUTS_SCHEMA_VERSION &&
      /^[0-9a-f]{64}$/.test(document.checkouts_digest));
    protocol(document.configuration_revision === null ||
      (typeof document.configuration_revision === "string" &&
        /^[0-9a-f]{64}$/.test(document.configuration_revision)));
    protocol(Array.isArray(document.packages));
    const paths = new Set();
    document.packages.forEach((item) => {
      protocol(exactKeys(item, CHECKOUTS_PACKAGE_KEYS) &&
        relativePackagePath(item.package_path) && !paths.has(item.package_path));
      paths.add(item.package_path);
      const status = item.code_status;
      protocol(status === null ||
        (exactKeys(status, CHECKOUTS_CODE_STATUS_KEYS) && safeText(status.text)));
    });
    await authenticate(document, "checkouts_digest", CHECKOUTS_DIGEST_OMITTED);
    return payload;
  }

  // The document's own settings record, so the status line can name the lookup
  // state without re-deriving the collector's decisions.
  function lookupReasonWords(reason) {
    return codeWords("lookupReason", reason);
  }

  function utcDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function localDateKey(date) {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function clockText(date, seconds) {
    return `${pad(date.getHours())}:${pad(date.getMinutes())}` +
      (seconds ? `:${pad(date.getSeconds())}` : "");
  }

  // A local time in a time element; the date is named when the value falls on
  // another local day than the observation it belongs to.
  function localTimeHtml(value, referenceValue, seconds = true) {
    const date = utcDate(value);
    if (date === null) return text(String(value));
    const reference = utcDate(referenceValue);
    const day = reference === null ? "" :
      localDateKey(date) === localDateKey(reference) ? "" : `${localDateKey(date)} `;
    return `<time datetime="${text(String(value))}">${text(day + clockText(date, seconds))}</time>`;
  }

  // The pull-request lookup state of the document: the settings record, then
  // the most serious state any repository reached.
  function lookupWords(document) {
    if (document.github_lookup === "off") return codeWords("text", "lookupOff");
    const lookups = document.repositories
      .map((repository) => repository.pr_lookup)
      .filter((lookup) => lookup !== null && lookup !== undefined);
    const unavailable = lookups.find((lookup) => lookup.status === "unavailable");
    if (unavailable) {
      return wordText("text", "lookupUnavailable", {
        reason: lookupReasonWords(unavailable.reason),
      });
    }
    const recorded = lookups
      .filter((lookup) => lookup.status === "recorded")
      .sort((first, second) => String(second.checked_at).localeCompare(String(first.checked_at)))[0];
    if (recorded) {
      return wordHtml("text", "lookupRecorded", {
        time: localTimeHtml(recorded.checked_at, document.observed_at, false),
      });
    }
    if (lookups.some((lookup) => lookup.status === "live")) return codeWords("text", "lookupLive");
    const off = lookups.find((lookup) => lookup.reason && lookup.reason !== "setting_off");
    if (off) {
      return wordText("text", "lookupUnavailable", {reason: lookupReasonWords(off.reason)});
    }
    return codeWords("text", "lookupOff");
  }

  function nextCheckWords(envelope) {
    const next = envelope.remote_checks.next_due_at;
    return next === null
      ? codeWords("text", "nextCheckAtRefresh")
      : wordHtml("text", "nextCheckAbout", {
        time: localTimeHtml(next, envelope.checkouts.observed_at),
      });
  }

  function remoteCheckWords(envelope) {
    if (checkoutsCheckRequested || envelope.remote_checks.state === "running") {
      return codeWords("text", "checkingRemotes");
    }
    const last = envelope.remote_checks.last;
    if (last === null) return codeWords("text", "remotesNotChecked");
    const when = localTimeHtml(last.finished_at, envelope.checkouts.observed_at);
    return last.outcome === "completed"
      ? wordHtml("text", "remotesChecked", {time: when})
      : wordHtml("text", "remoteCheckFailedAt", {
        outcome: text(last.outcome), time: when,
      });
  }

  // The scan is its own dated answer, so a document collected under other
  // settings than the board shown says so once instead of hiding a value.
  function settingsSentence(document) {
    const catalogRevision = displayed === null ? null : displayed.configuration_revision;
    return catalogRevision === document.configuration_revision
      ? "" : codeWords("text", "settingsDiffer");
  }

  function renderCheckoutsStatusLine() {
    const envelope = checkoutsOutcome.state === "ready" ? checkoutsOutcome.envelope : null;
    if (envelope === null) {
      checkoutsStatusLine.innerHTML = "";
      return;
    }
    const observed = envelope.checkouts.observed_at;
    checkoutsStatusLine.innerHTML = [
      wordHtml("text", "localState", {time: localTimeHtml(observed, observed)}),
      remoteCheckWords(envelope),
      nextCheckWords(envelope),
      lookupWords(envelope.checkouts),
      settingsSentence(envelope.checkouts),
    ].filter(Boolean).join(" · ");
  }

  // One panel per unavailable state, before the groups.  A ready document
  // shows no panel, and its own setup problems belong to the setup panel.
  function checkoutsPanelHtml() {
    if (checkoutsOutcome.state === "not_offered") {
      return '<section class="checkouts-panel"><p>' +
        `${text(codeWords("text", "notOffered"))}</p></section>`;
    }
    if (checkoutsOutcome.state === "failed") {
      const category = checkoutsOutcome.failure;
      // What the next successful collection does for this category, when the
      // words know it.
      const clearing = (WORDS.clearing || {})[category] ||
        codeWords("text", "clearsDefault");
      return '<section class="checkouts-panel"><p>' +
        `${wordText("text", "unavailable", {category})} ${text(clearing)}` +
        "</p></section>";
    }
    return "";
  }

  // ---------------------------------------------------------------------
  // The ready view: the setup panel, the quiet row, groups and their tables
  // ---------------------------------------------------------------------

  const CHECK_FAILURE_REASONS = [
    "unreachable", "not_found", "authentication_failed", "timed_out", "failed",
  ];
  const CHANGE_KINDS = ["staged", "unstaged", "untracked", "conflicted"];
  const CLONE_COLUMNS = [
    "checkout", "availability", "head", "default", "tree", "branches", "cleanup", "package",
  ];
  const PACKAGE_COLUMNS = ["branch", "remoteCopy", "verdict", "pullRequests", "workItems"];

  function checkFailed(reason) {
    return CHECK_FAILURE_REASONS.includes(reason);
  }

  function dashHtml() {
    return '<span class="checkouts-empty">—</span>';
  }

  function checkoutName(path) {
    const parts = String(path).split(/[\\/]/);
    const last = parts[parts.length - 1];
    return last === "" ? String(path) : last;
  }

  // The count and its noun in the plural: "es" after a sibilant, "ies" after a
  // consonant and "y", else "s".  Every noun the view counts goes through here,
  // so "stash" reads "2 stashes" and "branch" "2 branches".
  function plural(count, noun) {
    if (count === 1) return `1 ${noun}`;
    if (/[^aeiou]y$/.test(noun)) return `${count} ${noun.slice(0, -1)}ies`;
    if (/(s|x|z|ch|sh)$/.test(noun)) return `${count} ${noun}es`;
    return `${count} ${noun}s`;
  }

  // A whole unit rounded down against the document's own observation instant,
  // so two collections of one state show the same age.
  function ageWords(value, observedAt) {
    const moment = utcDate(value);
    const observed = utcDate(observedAt);
    if (moment === null || observed === null) return "";
    const minutes = Math.floor(Math.max(0, observed.getTime() - moment.getTime()) / 60000);
    if (minutes < 1) return "<1 min";
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} h`;
    return `${Math.floor(hours / 24)} d`;
  }

  // The comparison a clone's default branch carries: the live or recorded
  // answer, else the cached one.
  function defaultDistance(clone) {
    const local = clone.local_default;
    if (local === null || local === undefined) return null;
    return local.vs_remote || local.vs_cached || null;
  }

  function defaultBranchName(clone) {
    if (clone.verdict_base !== null && clone.verdict_base !== undefined) {
      return clone.verdict_base.name;
    }
    if (clone.local_default !== null && clone.local_default !== undefined) {
      return clone.local_default.name;
    }
    return null;
  }

  function comparisonWords(comparison) {
    if (comparison === null || comparison === undefined) {
      return codeWords("comparisonRelation", "unknown");
    }
    return wordText("comparisonRelation", comparison.relation, {
      a: comparison.ahead === null ? "" : comparison.ahead,
      b: comparison.behind === null ? "" : comparison.behind,
    });
  }

  // A recorded value is dated by the answer it was recorded from; pull-request
  // values are dated by the lookup, remote values by the remote check.
  function basisWords(document, basis, checkedAt) {
    if (basis !== "recorded") return text(codeWords("comparisonBasis", basis));
    if (checkedAt === null || checkedAt === undefined || checkedAt === "") {
      return text(codeWords("comparisonBasis", "recorded"));
    }
    return wordHtml("comparisonBasis", "recorded", {
      time: localTimeHtml(checkedAt, document.observed_at, false),
    });
  }

  function remoteName(group) {
    return group.remote === null ? codeWords("text", "noOriginRemote") : group.remote.display;
  }

  function checkWords(document, group) {
    const check = group.remote === null ? null : group.remote.check;
    if (check === null || check === undefined) return "";
    if (check.status === "recorded") {
      const words = wordHtml("checkStatus", "recorded", {
        time: localTimeHtml(check.checked_at, document.observed_at, false),
      });
      return checkFailed(check.reason)
        ? `${words} (${text(codeWords("checkReason", check.reason))})` : words;
    }
    if (check.status === "failed") {
      return `${text(codeWords("checkStatus", "failed"))} ` +
        `(${text(codeWords("checkReason", check.reason))})`;
    }
    return text(codeWords("checkStatus", check.status));
  }

  function groupLookupWords(document, group) {
    const lookup = group.pr_lookup;
    if (lookup === null || lookup === undefined) return "";
    if (lookup.status === "unavailable") {
      return wordText("lookupStatus", "unavailable", {
        reason: lookupReasonWords(lookup.reason || "failed"),
      });
    }
    if (lookup.status === "recorded") {
      return wordHtml("lookupStatus", "recorded", {
        time: localTimeHtml(lookup.checked_at, document.observed_at, false),
      });
    }
    return text(codeWords("lookupStatus", lookup.status));
  }

  function freeClonesHtml(document, group) {
    const parts = (group.free_clones || []).map((path) => {
      const clone = group.clones.find((item) => item.path === path);
      const distance = clone === undefined ? null : defaultDistance(clone);
      return `<span class="checkouts-free-clone">${text(checkoutName(path))}` +
        ` <span class="checkouts-relation">${comparisonWords(distance)}</span></span>`;
    });
    return parts.join('<span class="checkouts-separator"> · </span>');
  }

  function groupHeaderHtml(document, group) {
    const defaultBranch = group.default_branch === null ? null : group.default_branch.name;
    const free = freeClonesHtml(document, group);
    return '<h2 class="checkouts-group-head">' +
      `<span class="checkouts-group-name">${text(remoteName(group))}</span>` +
      (defaultBranch === null ? "" :
        `<span class="checkouts-group-default">${text(defaultBranch)}</span>`) +
      `<span class="checkouts-group-check">${checkWords(document, group)}</span>` +
      `<span class="checkouts-group-lookup">${groupLookupWords(document, group)}</span>` +
      (free === "" ? "" : `<span class="checkouts-group-free">` +
        `${wordHtml("text", "freeClones", {clones: free})}</span>`) +
      "</h2>";
  }

  // A repository is quiet when its served facts say nothing about it needs a
  // person.  Equality needs a served comparison: two null comparisons prove
  // nothing, so such a repository is shown.
  function isQuietRepository(document, group) {
    if (group.clones.length !== 1) return false;
    const clone = group.clones[0];
    if (clone.status !== "ok") return false;
    if (clone.availability === null || clone.availability === undefined ||
      clone.availability.state !== "free") return false;
    const distance = defaultDistance(clone);
    if (distance === null || distance.relation !== "equal") return false;
    if (clone.stash_count !== 0) return false;
    if (clone.worktrees.some((worktree) => !worktree.main)) return false;
    const base = defaultBranchName(clone);
    if (clone.branches.some((branch) => branch.name !== base)) return false;
    if ((group.package_branches || []).length) return false;
    if (document.packages.some((item) =>
      item.target !== null && item.target !== undefined && item.target.clone === clone.path)) {
      return false;
    }
    const key = group.remote === null ? null : group.remote.key;
    return !document.attention.some((item) => item.clone === clone.path ||
      (key !== null && item.repository === key));
  }

  function quietRowHtml(document, quiet) {
    const items = quiet.map((group) => {
      const clone = group.clones[0];
      return "<li>" +
        `<span class="checkouts-quiet-display">${text(remoteName(group))}</span> ` +
        `<span class="checkouts-quiet-checkout">${text(checkoutName(clone.path))}</span></li>`;
    }).join("");
    return '<details class="checkouts-quiet">' +
      `<summary>${wordText("text", "quietRow", {count: quiet.length})}</summary>` +
      `<ul>${items}</ul></details>`;
  }

  function folderProblemWords(folder) {
    return wordText("text", "folderProblem", {
      path: folder.path, problem: codeWords("folderStatus", folder.status),
    });
  }

  function gitProblemWords(git) {
    if (git === null || git === undefined) return "";
    if (git.status === "missing") return text(codeWords("gitStatus", "missing"));
    if (git.status !== "too_old") return "";
    return git.version === null || git.version === undefined
      ? text(codeWords("gitStatus", "tooOldUnread"))
      : wordText("gitStatus", "too_old", {version: git.version});
  }

  function lookupMissing(document) {
    return document.github_lookup === "on" && document.repositories.some((group) =>
      group.pr_lookup !== null && group.pr_lookup !== undefined &&
      group.pr_lookup.reason === "gh_missing");
  }

  function diagnosticWords(diagnostic) {
    const path = diagnostic.path === null || diagnostic.path === undefined
      ? codeWords("text", "unknownPath") : diagnostic.path;
    return wordText("diagnostic", diagnostic.code, {path});
  }

  // One setup panel for a ready document, in one order; a folder problem never
  // suppresses a group, and the panel is neither a live region nor a focus
  // target.
  function checkoutSetupHtml(document) {
    const items = [];
    const git = gitProblemWords(document.git);
    if (git !== "") items.push(["git", git]);
    if (!document.folders.length) {
      items.push(["folders", text(codeWords("text", "noFolders"))]);
    }
    document.folders.forEach((folder) => {
      if (folder.status === "ok") return;
      items.push(["folder", folderProblemWords(folder)]);
    });
    if (lookupMissing(document)) {
      items.push(["gh", text(codeWords("text", "ghMissing"))]);
    }
    document.diagnostics.forEach((diagnostic) => {
      items.push(["diagnostic", diagnosticWords(diagnostic)]);
    });
    if (!items.length) return "";
    return '<section class="checkouts-panel checkouts-setup">' +
      `<h2>${text(codeWords("text", "setupHeading"))}</h2><ul>` +
      items.map(([kind, html]) =>
        `<li class="checkouts-setup-item" data-setup="${kind}">${html}</li>`).join("") +
      "</ul></section>";
  }

  function localBranchOf(group, name) {
    for (const clone of group.clones) {
      if (clone.status !== "ok") continue;
      const found = clone.branches.find((branch) => branch.name === name);
      if (found !== undefined) return {clone, branch: found};
    }
    return null;
  }

  function firstReadableClone(group) {
    return group.clones.find((clone) => clone.status === "ok") || null;
  }

  // A merge verdict carries the basis its verdict base was compared against.
  function verdictBasisHtml(document, group, clone) {
    if (clone === null || clone.verdict_base === null || clone.verdict_base === undefined) {
      return "";
    }
    const check = group.remote === null ? null : group.remote.check;
    const basis = basisWords(document, clone.verdict_base.basis,
      check === null || check === undefined ? null : check.checked_at);
    return ` <span class="checkouts-basis">${basis}</span>`;
  }

  function verdictWords(document, group, entry) {
    const remoteVerdict = entry.remote_verdict;
    const local = localBranchOf(group, entry.name);
    const verdict = remoteVerdict !== null && remoteVerdict !== undefined
      ? remoteVerdict
      : (local === null ? null : local.branch.verdict);
    if (verdict === null || verdict === undefined) return dashHtml();
    const state = text(codeWords("verdictState", verdict.state));
    const base = group.default_branch === null || group.default_branch.name === null
      ? "" : group.default_branch.name;
    const basis = verdictBasisHtml(document, group,
      local === null ? firstReadableClone(group) : local.clone);
    let words = state;
    if (verdict.state === "merged" && verdict.rung !== null && verdict.rung !== undefined) {
      const rung = wordText("verdictRung", verdict.rung, {
        default: base,
        n: verdict.pull_request === null || verdict.pull_request === undefined
          ? "" : verdict.pull_request,
      });
      if (rung !== "") words = `${state} — ${rung}`;
    } else if (verdict.state === "unknown" &&
      verdict.reason !== null && verdict.reason !== undefined) {
      words = `${state} — ${text(codeWords("verdictReason", verdict.reason))}`;
    }
    return `<span class="checkouts-verdict-words">${words}</span>${basis}`;
  }

  function remoteCopyWords(document, group, copy) {
    if (copy === null || copy === undefined) return dashHtml();
    if (copy.state !== "present") return text(codeWords("remoteCopy", copy.state));
    const check = group.remote === null ? null : group.remote.check;
    const basis = basisWords(document, copy.basis,
      check === null || check === undefined ? null : check.checked_at);
    return wordHtml("remoteCopy", "present", {basis});
  }

  function pullRequestsWords(document, group, entry) {
    const local = localBranchOf(group, entry.name);
    const pulls = local === null ? null : local.branch.pull_requests;
    if (pulls === null || pulls === undefined || !pulls.length) {
      return dashHtml();
    }
    return pulls.map((pull) =>
      `<span class="checkouts-pull">#${text(pull.number)} ` +
      `${text(codeWords("pullRequestState", pull.state))}` +
      (pull.head_matches ? "" :
        ` ${text(codeWords("pullRequestState", "differentHead"))}`) +
      "</span>").join(" · ");
  }

  // How the group's clones serve one work item: the basis each branch reference
  // in the group carries for it.
  function workItemBases(group, path) {
    const bases = new Set();
    group.clones.forEach((clone) => {
      if (clone.status !== "ok") return;
      (clone.branches || []).forEach((branch) => {
        (branch.packages || []).forEach((reference) => {
          if (reference.package_path === path) bases.add(reference.basis);
        });
      });
    });
    return [...bases].sort();
  }

  // Each work item names how the group serves it and its own board action.
  function workItemsHtml(group, entry) {
    return entry.packages.map((path) => {
      const bases = workItemBases(group, path);
      const suffix = bases.length
        ? ` <span class="checkouts-basis">(${text(bases.map((basis) =>
          codeWords("branchPackageBasis", basis)).join(", "))})</span>`
        : "";
      return '<span class="checkouts-work-item">' +
        `<span class="checkouts-package-path">${text(path)}</span>${suffix} ` +
        '<button type="button" class="checkouts-show-on-board" ' +
        `data-show-on-board="${text(path)}">` +
        `${text(codeWords("text", "showOnBoard"))}</button></span>`;
    }).join(" · ");
  }

  function packageBranchesHtml(document, group) {
    const entries = group.package_branches || [];
    if (!entries.length) return "";
    const head = PACKAGE_COLUMNS.map((key) =>
      `<th scope="col">${text(codeWords("column", key))}</th>`).join("");
    const rows = entries.map((entry) =>
      `<tr class="checkouts-branch" data-branch="${text(entry.name)}">` +
      `<th scope="row">${text(entry.name)}</th>` +
      `<td class="checkouts-remote-copy">${remoteCopyWords(document, group, entry.remote)}</td>` +
      `<td class="checkouts-verdict">${verdictWords(document, group, entry)}</td>` +
      `<td class="checkouts-pulls">${pullRequestsWords(document, group, entry)}</td>` +
      `<td class="checkouts-work-items">${workItemsHtml(group, entry)}</td>` +
      "</tr>").join("");
    return '<table class="checkouts-package-branches">' +
      `<caption>${text(codeWords("text", "workItemBranches"))}</caption>` +
      `<thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  }

  function cloneFlagsHtml(document, clone) {
    const flags = [];
    if (clone.shallow) flags.push(text(codeWords("text", "shallow")));
    if (clone.partial_clone) flags.push(text(codeWords("text", "partial")));
    return flags.length
      ? `<span class="checkouts-flags">${flags.join(", ")}</span>` : "";
  }

  // Once on the clone row: the fetch age a cached value was read from.
  function freshnessHtml(document, clone) {
    const distance = defaultDistance(clone);
    if (distance === null || distance.basis !== "cached") return "";
    const cached = clone.cached_default;
    if (cached === null || cached === undefined) return "";
    if (cached.fetched_since_clone === false ||
      cached.last_fetched_at === null || cached.last_fetched_at === undefined) {
      return `<span class="checkouts-freshness">${text(codeWords("text", "neverFetched"))}</span>`;
    }
    return `<span class="checkouts-freshness">` +
      `${wordText("text", "fetchedAgo", {age: ageWords(cached.last_fetched_at, document.observed_at)})}` +
      "</span>";
  }

  function checkoutCellHtml(document, clone) {
    return '<th scope="row" class="checkouts-checkout">' +
      `<span class="checkouts-name">${text(checkoutName(clone.path))}</span>` +
      cloneFlagsHtml(document, clone) + freshnessHtml(document, clone) +
      "</th>";
  }

  function availabilityCellHtml(clone) {
    if (clone.status !== "ok") return text(codeWords("availabilityState", "unknown"));
    const availability = clone.availability || {state: "unknown", reasons: [], assigned_to: []};
    const parts = [text(codeWords("availabilityState", availability.state))];
    const reasons = (availability.reasons || []).map((reason) =>
      text(codeWords("availabilityReason", reason)));
    if (reasons.length) parts.push(`(${reasons.join(", ")})`);
    const assigned = (availability.assigned_to || []).map((item) =>
      `<span class="checkouts-assigned">${text(item.package_path)} ` +
      `(${text(codeWords("assignmentBasis", item.basis))})</span>`).join(" · ");
    if (assigned !== "") {
      parts.push(`${text(codeWords("text", "assignedPrefix"))}: ${assigned}`);
    }
    return parts.join(" · ");
  }

  function headCellHtml(worktree) {
    if (worktree === null || worktree.status !== "ok" || worktree.head === null) {
      return dashHtml();
    }
    const head = worktree.head;
    if (head.branch !== null && (head.sha === null || head.sha === undefined)) {
      return wordText("text", "unborn", {branch: head.branch});
    }
    if (head.branch !== null) return text(head.branch);
    return wordText("text", "detachedAt", {sha: String(head.sha).slice(0, 7)});
  }

  function treeCellHtml(worktree) {
    if (worktree === null) return dashHtml();
    if (worktree.status !== "ok") return text(codeWords("status", worktree.status));
    const parts = [];
    CHANGE_KINDS.forEach((kind) => {
      const count = (worktree.changes || {})[kind];
      if (!count) return;
      parts.push(`${count} ${text(codeWords("changeKind", kind))}`);
    });
    if (!parts.length) parts.push(text(codeWords("text", "clean")));
    (worktree.operations || []).forEach((operation) => {
      parts.push(wordText("text", "operationInProgress", {
        operation: codeWords("operation", operation),
      }));
    });
    if (worktree.index_lock) parts.push(text(codeWords("text", "indexLock")));
    return parts.join(" · ");
  }

  function defaultCellHtml(document, group, clone) {
    const distance = defaultDistance(clone);
    const check = group.remote === null ? null : group.remote.check;
    const basis = distance === null ? "" : basisWords(document, distance.basis,
      check === null || check === undefined ? null : check.checked_at);
    const failed = checkFailed(check === null || check === undefined ? null : check.reason);
    return '<td class="checkouts-default">' +
      `<span class="checkouts-relation">${comparisonWords(distance)}</span>` +
      (basis === "" ? "" : ` <span class="checkouts-basis">${basis}</span>`) +
      (failed ? ` <span class="checkouts-note">${wordText("text", "latestCheckFailed", {
        reason: codeWords("checkReason", check.reason),
      })}</span>` : "") +
      "</td>";
  }

  function branchesCellHtml(clone) {
    const parts = [plural(clone.branches.length, "branch")];
    if (clone.stash_count > 0) parts.push(plural(clone.stash_count, "stash"));
    if (clone.containment !== null && clone.containment !== undefined &&
      clone.containment !== "complete") {
      parts.push(`<span class="checkouts-caption">` +
        `${wordText("text", "foldingNotChecked", {
          reason: codeWords("containment", clone.containment),
        })}</span>`);
    }
    return parts.join(" · ");
  }

  function blockersHtml(blockers, table) {
    return blockers.map((code) => text(codeWords(table, code))).join(", ");
  }

  function cleanupCellHtml(clone) {
    const count = clone.removable_count || {branches: 0, worktrees: 0};
    const parts = [];
    if (count.branches === 0 && count.worktrees === 0) {
      parts.push(text(codeWords("text", "nothingToRemove")));
    } else {
      parts.push(`${plural(count.branches, "branch")}, ` +
        `${plural(count.worktrees, "worktree")} removable`);
    }
    const blocked = [];
    clone.branches.forEach((branch) => {
      if (branch.removable === null || branch.removable === undefined ||
        branch.removable.removable) return;
      blocked.push(`<span class="checkouts-blocker">${text(branch.name)} ` +
        `(${blockersHtml(branch.removable.blockers, "branchBlocker")})</span>`);
    });
    clone.worktrees.forEach((worktree) => {
      if (worktree.main || worktree.removable === null ||
        worktree.removable === undefined || worktree.removable.removable) return;
      blocked.push(`<span class="checkouts-blocker">${text(checkoutName(worktree.path))} ` +
        `(${blockersHtml(worktree.removable.blockers, "worktreeBlocker")})</span>`);
    });
    if (blocked.length) parts.push(blocked.join(" · "));
    return parts.join(" · ");
  }

  function packageCellHtml(document, clone, worktree) {
    const paths = document.packages
      .filter((item) => {
        const target = item.target;
        if (target === null || target === undefined) return false;
        if (worktree !== null) return target.worktree === worktree.path;
        if (target.clone !== clone.path) return false;
        return clone.status !== "ok" || target.worktree === null;
      })
      .map((item) => item.package_path);
    return paths.length
      ? paths.map((path) => `<span class="checkouts-package-path">${text(path)}</span>`).join(" · ")
      : dashHtml();
  }

  function cloneRowHtml(document, group, clone) {
    const name = checkoutName(clone.path);
    const cells = [checkoutCellHtml(document, clone),
      `<td class="checkouts-availability">${availabilityCellHtml(clone)}</td>`];
    if (clone.status !== "ok") {
      // A clone that could not be read keeps its Package cell and has no
      // toggle; one cell spans the columns its facts would have filled.
      cells.push(`<td class="checkouts-status" colspan="5">` +
        `${text(codeWords("status", clone.status))}</td>`);
      cells.push(`<td class="checkouts-package">${packageCellHtml(document, clone, null)}</td>`);
      return `<tr class="checkouts-clone" data-clone="${text(name)}">${cells.join("")}</tr>`;
    }
    const main = clone.worktrees.find((worktree) => worktree.main) || null;
    const readable = main !== null && main.status === "ok";
    // A main worktree that could not be read shows its own status words in
    // both cells; it is never a nested row of its own.
    const mainStatus = main === null ? "" : text(codeWords("status", main.status));
    cells.push(`<td class="checkouts-head">${clone.bare
      ? text(codeWords("text", "bareRepository"))
      : readable ? headCellHtml(main) : mainStatus}</td>`);
    cells.push(defaultCellHtml(document, group, clone));
    cells.push(`<td class="checkouts-tree">${clone.bare
      ? text(codeWords("text", "noWorkingTree"))
      : readable ? treeCellHtml(main) : mainStatus}</td>`);
    cells.push(`<td class="checkouts-branches">${branchesCellHtml(clone)}</td>`);
    cells.push(`<td class="checkouts-cleanup">${cleanupCellHtml(clone)}</td>`);
    cells.push(`<td class="checkouts-package">${packageCellHtml(document, clone, null)}</td>`);
    return `<tr class="checkouts-clone" data-clone="${text(name)}">${cells.join("")}</tr>`;
  }

  function worktreeCleanupHtml(worktree) {
    if (worktree.removable === null || worktree.removable === undefined) return dashHtml();
    if (worktree.removable.removable) return text(codeWords("text", "removable"));
    return blockersHtml(worktree.removable.blockers, "worktreeBlocker");
  }

  function worktreeRowHtml(document, clone, worktree) {
    const name = checkoutName(worktree.path);
    const flags = [];
    if (worktree.prunable) flags.push(`<span class="checkouts-flag">` +
      `${text(codeWords("worktreeFlag", "prunable"))}</span>`);
    if (worktree.locked) flags.push(`<span class="checkouts-flag">` +
      `${text(codeWords("worktreeFlag", "locked"))}</span>`);
    const status = text(codeWords("status", worktree.status));
    const cells = [
      '<th scope="row" class="checkouts-checkout checkouts-worktree-name">' +
        `<span class="checkouts-name">${text(name)}</span>` +
        (flags.length ? `<span class="checkouts-flags">${flags.join(", ")}</span>` : "") +
        "</th>",
      `<td class="checkouts-availability">${dashHtml()}</td>`,
      `<td class="checkouts-head">${worktree.status === "ok" ? headCellHtml(worktree) : status}</td>`,
      `<td class="checkouts-default">${dashHtml()}</td>`,
      `<td class="checkouts-tree">${worktree.status === "ok" ? treeCellHtml(worktree) : status}</td>`,
      `<td class="checkouts-branches">${dashHtml()}</td>`,
      `<td class="checkouts-cleanup">${worktree.status === "ok"
        ? worktreeCleanupHtml(worktree) : dashHtml()}</td>`,
      `<td class="checkouts-package">${packageCellHtml(document, clone, worktree)}</td>`,
    ];
    return `<tr class="checkouts-worktree" data-worktree="${text(name)}">` +
      `${cells.join("")}</tr>`;
  }

  function cloneTableHtml(document, group) {
    const head = CLONE_COLUMNS.map((key) =>
      `<th scope="col">${text(codeWords("column", key))}</th>`).join("");
    const rows = [];
    group.clones.forEach((clone) => {
      rows.push(cloneRowHtml(document, group, clone));
      if (clone.status !== "ok") return;
      clone.worktrees.forEach((worktree) => {
        if (!worktree.main) rows.push(worktreeRowHtml(document, clone, worktree));
      });
    });
    return `<table class="checkouts-clones"><thead><tr>${head}</tr></thead>` +
      `<tbody>${rows.join("")}</tbody></table>`;
  }

  function groupSearchText(document, group) {
    const values = [remoteName(group)];
    if (group.default_branch !== null && group.default_branch.name !== null) {
      values.push(group.default_branch.name);
    }
    const clonePaths = new Set(group.clones.map((clone) => clone.path));
    group.clones.forEach((clone) => {
      values.push(checkoutName(clone.path), clone.path);
      (clone.worktrees || []).forEach((worktree) => {
        values.push(checkoutName(worktree.path), worktree.path);
      });
      (clone.branches || []).forEach((branch) => values.push(branch.name));
    });
    (group.package_branches || []).forEach((entry) => {
      values.push(entry.name);
      entry.packages.forEach((path) => values.push(path));
    });
    document.packages.forEach((item) => {
      if (item.target !== null && item.target !== undefined &&
        clonePaths.has(item.target.clone)) {
        values.push(item.package_path);
      }
    });
    return values
      .filter((value) => value !== null && value !== undefined && value !== "")
      .join(" ").toLowerCase();
  }

  function groupKey(group) {
    return group.remote === null ? group.clones[0].path : group.remote.key;
  }

  function groupHtml(document, group) {
    return `<section class="checkouts-group" data-checkouts-group="${text(groupKey(group))}" ` +
      `data-search="${text(groupSearchText(document, group))}">` +
      groupHeaderHtml(document, group) +
      packageBranchesHtml(document, group) +
      cloneTableHtml(document, group) +
      "</section>";
  }

  function checkoutsGroupsHtml(document) {
    const quiet = document.repositories.filter((group) =>
      isQuietRepository(document, group));
    const shown = hideQuietCheckouts
      ? document.repositories.filter((group) => !isQuietRepository(document, group))
      : document.repositories;
    return checkoutSetupHtml(document) +
      (hideQuietCheckouts && quiet.length ? quietRowHtml(document, quiet) : "") +
      shown.map((group) => groupHtml(document, group)).join("");
  }

  function renderCheckouts() {
    renderCheckoutsStatusLine();
    checkoutsBody.innerHTML = checkoutsPanelHtml() +
      (checkoutsOutcome.state === "ready"
        ? checkoutsGroupsHtml(checkoutsOutcome.envelope.checkouts) : "");
    applyFilter();
  }

  // Show one work item on the board: the selection moves to it and is never
  // toggled off, a search that hides its card is cleared first, and a work item
  // the board cannot show says why and leaves the view where it is.
  function showOnBoard(path) {
    const known = displayed !== null &&
      displayed.entries.some((entry) => entry.package_path === path);
    if (!known) {
      setCheckoutsActionStatus(codeWords("text", "showOnBoardMissing"));
      return;
    }
    if (!renderedEntries.has(path)) {
      setCheckoutsActionStatus(codeWords("text", "showOnBoardFinished"));
      return;
    }
    const card = [...board.querySelectorAll(".card")]
      .find((item) => item.dataset.packagePath === path);
    // The reader's search survives the jump unless it is what hides the card;
    // the card's own search text decides that, so a query matching it stays.
    const query = filter.value.trim().toLowerCase();
    if (query !== "" && card !== undefined && !(card.dataset.search || "").includes(query)) {
      filter.value = "";
      applyFilter();
    }
    setShownView(VIEW_BOARD);
    if (selectedPath !== path) select(path);
    if (card === undefined) return;
    card.scrollIntoView({block: "nearest"});
    card.focus();
  }

  checkoutsBody.addEventListener("click", (event) => {
    const button = event.target.closest("[data-show-on-board]");
    if (button === null) return;
    showOnBoard(button.dataset.showOnBoard);
  });

  // Every settle of a pressed check announces its result once, even when it is
  // the same words as the previous one: one write per settle is one text
  // change, so a repeated completion or failure is still announced.
  function setCheckoutsActionStatus(message) {
    const text = checkoutsActionStatus.firstChild;
    if (text !== null && text === checkoutsActionStatus.lastChild &&
      text.nodeType === Node.TEXT_NODE) {
      text.data = message;
    } else {
      checkoutsActionStatus.textContent = message;
    }
  }

  function remoteCheckMessage(outcome) {
    if (outcome.state === "ready") {
      const last = outcome.envelope.remote_checks.last;
      if (last !== null && last.outcome === "completed") {
        return codeWords("text", "runCompleted");
      }
      return wordText("text", "runFailed", {
        category: last !== null ? last.outcome : "checkouts_failed",
      });
    }
    if (outcome.state === "failed") {
      return wordText("text", "runFailed", {category: outcome.failure});
    }
    return codeWords("text", "doesNotOfferRemotes");
  }

  function renderCheckRemotesButton() {
    if (checkoutsCheckRequested) {
      checkRemotesButton.setAttribute("aria-disabled", "true");
      checkRemotesButton.textContent = codeWords("text", "checkingRemotes");
    } else {
      checkRemotesButton.removeAttribute("aria-disabled");
      checkRemotesButton.textContent = codeWords("text", "checkRemotes");
    }
  }

  // One press asks for one remote check.  It is sent at once when nothing is in
  // flight, and otherwise as the single queued follow-up of the in-flight
  // request; a second press before the answer does nothing.
  function pressCheckRemotes() {
    if (checkoutsCheckRequested) return;
    checkoutsCheckRequested = true;
    renderCheckRemotesButton();
    renderCheckoutsStatusLine();
    if (checkoutsBusy) {
      checkoutsQueuedCheck = true;
      return;
    }
    requestCheckouts("check");
  }

  // One checkouts request at most is in flight: a tick, or the view's refresh
  // button, that finds one running is skipped rather than queued, because the
  // server answers every request with the document it has and rescans at most
  // once a minute.  A POST queued by the button is the one exception.
  function requestCheckouts(kind = "read") {
    if (checkoutsBusy) return;
    checkoutsBusy = true;
    const checking = kind === "check";
    fetch(checking ? REMOTE_CHECK_ROUTE : CHECKOUTS_ROUTE, checking ? {
      method: "POST",
      cache: "no-store",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    } : {cache: "no-store"})
      .then(async (response) => {
        if (response.status === 404) {
          // A 404 on the POST means this provider serves checkout state without
          // a remote-check method, so the shown outcome stays as it is.
          return checking
            ? {state: "unchanged", envelope: null, failure: null}
            : {state: "not_offered", envelope: null, failure: null};
        }
        let payload = null;
        try {
          payload = JSON.parse(await response.text());
        } catch (_) {
          return {state: "failed", envelope: null, failure: "checkouts_protocol_error"};
        }
        if (!response.ok) {
          const body = payload && typeof payload === "object" && !Array.isArray(payload);
          const code = body && typeof payload.error === "string" ? payload.error : "";
          return {
            state: "failed", envelope: null,
            failure: safeCategory(new Error(code), "checkouts_", "checkouts_unavailable"),
          };
        }
        try {
          return {state: "ready", envelope: await validateCheckouts(payload), failure: null};
        } catch (_) {
          // Validation and digest failures are their own category and never a
          // mapped server code.
          return {state: "failed", envelope: null, failure: "checkouts_protocol_error"};
        }
      })
      .catch(() => ({state: "failed", envelope: null, failure: "checkouts_unavailable"}))
      .then((outcome) => {
        checkoutsBusy = false;
        if (outcome.state === "unchanged") {
          setCheckoutsActionStatus("This server does not offer remote checks.");
        } else {
          const changed = outcome.state !== checkoutsOutcome.state ||
            outcome.failure !== checkoutsOutcome.failure;
          checkoutsOutcome = outcome;
          // The cards follow every settled outcome, including one that only
          // rewrites the text they already show.
          updateCardCodeStatus();
          if (changed) syncBoardIssues();
          if (checking) setCheckoutsActionStatus(remoteCheckMessage(outcome));
        }
        // The press state lasts from the press until the request that answers
        // it settles.  A read hands the press to its queued POST and keeps the
        // state, so the button and the status line still say a check is running
        // while that POST is in flight; only a POST's own settle clears it.
        const handOff = checkoutsQueuedCheck;
        if (!handOff) {
          checkoutsCheckRequested = false;
          renderCheckRemotesButton();
        }
        renderCheckouts();
        // The press that arrived while a read was in flight is sent now, on
        // that read's settle, rather than at the next tick.
        if (handOff) {
          checkoutsQueuedCheck = false;
          requestCheckouts("check");
        }
      });
  }

  function request(kind, expectedRevision = null) {
    if (busy) {
      if (kind === "manual") queued = "manual";
      if (kind === "settings") queuedSettingsRefresh = expectedRevision;
      return;
    }
    busy = true;
    fetch(CATALOG_ROUTE, { cache: "no-store" })
      .then(async (response) => {
        let payload;
        try { payload = await response.json(); }
        catch (_) { throw new Error("producer_protocol_error"); }
        if (!response.ok || !payload || payload.error) {
          throw new Error((payload && payload.error) || "producer_protocol_error");
        }
        return validateSnapshot(payload);
      })
      .then((snapshot) => {
        if (kind !== "settings") return snapshot;
        if (snapshot.configuration_revision !== expectedRevision) {
          throw new Error("settings_refresh_mismatch");
        }
        return fetch(SETTINGS_ROUTE, {cache: "no-store"})
          .then(async (response) => {
            let payload = null;
            try { payload = await response.json(); } catch (_) { /* handled below */ }
            if (!response.ok || !payload || payload.error) {
              throw new Error((payload && payload.error) || "settings_unavailable");
            }
            return parseSettings(payload);
          })
          .then((settings) => {
            if (settings.revision !== expectedRevision) {
              throw new Error("settings_refresh_mismatch");
            }
            return snapshot;
          });
      })
      .then((snapshot) => {
        const hadRefreshFailure = Boolean(refreshFailure);
        refreshFailure = null;
        if (kind === "manual" || kind === "settings" || !displayed) {
          apply(snapshot);
        } else {
          if (digestOf(snapshot) !== digestOf(displayed)) {
            pending = snapshot;
            setPending(true);
          } else {
            pending = null;
            setPending(false);
          }
          if (hadRefreshFailure) {
            syncBoardIssues();
            drawRails();
          }
        }
        if (hadRefreshFailure) {
          document.querySelector("#refresh-status").textContent = "Refresh issue resolved.";
        }
      })
      .catch((error) => {
        if (kind === "settings") {
          settingsAvailable = false;
          settingsReloadAvailable = true;
          settingsRevision = null;
          settingsStatus("Saved board settings could not be matched to a fresh catalog; reloading current settings.", true);
          renderStageOrderEditor();
          loadSettings({refreshCatalog: true});
          return;
        }
        const failure = safeCategory(error, "producer_", "producer_unavailable");
        const changed = failure !== refreshFailure;
        refreshFailure = failure;
        syncBoardIssues();
        drawRails();
        if (changed) {
          document.querySelector("#refresh-status").textContent = `Latest refresh issue: ${failure}`;
        }
      })
      .finally(() => {
        busy = false;
        const next = queued;
        queued = null;
        if (next) request(next);
        else if (queuedSettingsRefresh) {
          const revision = queuedSettingsRefresh;
          queuedSettingsRefresh = null;
          request("settings", revision);
        }
      });
  }

  board.addEventListener("click", (event) => {
    const card = event.target.closest("[data-package-path]");
    if (card) select(card.dataset.packagePath);
    else if (deselectOnEmptyClick && selectedPath && !event.target.closest(".board-issues")) select(null);
  });
  board.addEventListener("contextmenu", (event) => {
    const card = event.target.closest(".card[data-package-path]");
    if (!card) return;
    event.preventDefault();
    const attempt = ++copyAttempt;
    const anchor = card.getBoundingClientRect();
    if (copyStatusTimer !== null) window.clearTimeout(copyStatusTimer);
    copyStatusTimer = null;
    copyStatus.classList.remove("visible");
    copyStatus.textContent = "";
    if (!displayed || !workspace ||
      workspace.revision !== displayed.configuration_revision) {
      showCopyStatus("Full directory path is unavailable. Refresh the view and try again.", anchor, true);
      return;
    }
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      showCopyStatus("Clipboard access is unavailable in this browser.", anchor, true);
      return;
    }
    const path = fullDirectoryPath(workspace.root, card.dataset.packagePath);
    navigator.clipboard.writeText(path).then(() => {
      if (attempt === copyAttempt) showCopyStatus("Full directory path copied.", anchor);
    }).catch(() => {
      if (attempt === copyAttempt) {
        showCopyStatus("Clipboard access was denied; directory path was not copied.", anchor, true);
      }
    });
  });
  viewBoardButton.addEventListener("click", () => {
    if (shownView !== VIEW_BOARD) setShownView(VIEW_BOARD);
  });
  viewCheckoutsButton.addEventListener("click", () => {
    if (shownView !== VIEW_CHECKOUTS) setShownView(VIEW_CHECKOUTS);
  });
  checkRemotesButton.addEventListener("click", () => {
    if (checkRemotesButton.getAttribute("aria-disabled") === "true") return;
    pressCheckRemotes();
  });
  refreshButton.addEventListener("click", () => {
    // In the checkouts view the button asks for the latest served scan and
    // never applies a waiting board catalog.
    if (shownView === VIEW_CHECKOUTS) {
      requestCheckouts("read");
      return;
    }
    if (pending) apply(pending);
    else request("manual");
  });
  stageOrderList.addEventListener("click", (event) => {
    if (settingsBusy && event.target.closest(".stage-completed-toggle")) {
      event.preventDefault();
      return;
    }
    const move = event.target.closest("[data-stage-move]");
    const item = event.target.closest("[data-stage]");
    if (!settingsAvailable || settingsBusy || !move || !item) return;
    const stage = item.dataset.stage;
    const index = editorStageOrder.indexOf(stage);
    const target = move.dataset.stageMove === "up" ? index - 1 : index + 1;
    if (index < 0 || target < 0 || target >= editorStageOrder.length) return;
    [editorStageOrder[index], editorStageOrder[target]] =
      [editorStageOrder[target], editorStageOrder[index]];
    renderStageOrderEditor();
    settingsStatus("Unsaved board row order changes.");
  });
  stageOrderList.addEventListener("change", (event) => {
    const checkbox = event.target.closest(".stage-completed-toggle");
    if (!settingsAvailable || settingsBusy || !checkbox) return;
    const stage = checkbox.dataset.stage;
    const selected = new Set(editorCompletedStages);
    if (checkbox.checked) selected.add(stage);
    else selected.delete(stage);
    editorCompletedStages = editorStageOrder.filter((name) => selected.has(name));
    settingsStatus("Unsaved board settings changes.");
    renderStageOrderEditor();
  });
  stageOrderSave.addEventListener("click", () =>
    saveStageOrder([...editorStageOrder], [...editorCompletedStages].sort(scalarCompare)));
  stageOrderCancel.addEventListener("click", () => {
    if (settingsBusy) return;
    editorStageOrder = editorNames();
    editorCompletedStages = [...savedCompletedStages];
    settingsStatus("Unsaved board settings changes cancelled.");
    renderStageOrderEditor();
  });
  stageOrderReset.addEventListener("click", () => saveStageOrder([], []));
  stageOrderReload.addEventListener("click", () => {
    if (!settingsBusy) loadSettings();
  });
  filter.addEventListener("input", applyFilter);
  compactControl.addEventListener("change", () => setCompactPreference(compactControl.checked));
  terminalControl.addEventListener("change", () => setTerminalPreference(terminalControl.checked));
  deselectControl.addEventListener("change", () => setDeselectPreference(deselectControl.checked));
  quietCheckoutsControl.addEventListener("change", () =>
    setQuietCheckoutsPreference(quietCheckoutsControl.checked));
  if (typeof ResizeObserver === "function") {
    const updateToolbarHeight = () =>
      document.documentElement.style.setProperty("--toolbar-height", `${toolbar.getBoundingClientRect().height}px`);
    updateToolbarHeight();
    new ResizeObserver(updateToolbarHeight).observe(toolbar);
    let resizeFrame = null;
    new ResizeObserver(() => {
      if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = null;
        drawRails();
      });
    }).observe(board);
  }

  readCompactPreference();
  readTerminalPreference();
  readDeselectPreference();
  readViewPreference();
  readQuietCheckoutsPreference();
  applyView();
  renderCheckRemotesButton();
  renderCheckouts();
  loadSettings();
  request("manual");
  requestCheckouts();
  window.setInterval(() => {
    request("poll");
    requestCheckouts();
  }, POLL_INTERVAL);
})();
