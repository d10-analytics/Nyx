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
  // These fields remain part of the wire contract and searchable index. They
  // are intentionally not all ordinary visible details.
  const DECLARED_FIELDS = [
    ["title", "Title"], ["target_project", "Target project"], ["status", "Status"],
    ["closure", "Closure"], ["sanity_recommendation", "Sanity recommendation"],
    ["human_sanity_decision", "Human sanity decision"],
  ];
  const SAFE_CATEGORIES = [
    "producer_unavailable", "producer_timeout", "producer_failed",
    "producer_output_too_large", "producer_protocol_error",
  ];
  const SVG_NS = "http://www.w3.org/2000/svg";
  const RAIL_PITCH = 14;
  const RAIL_INSET = 20;
  const POLL_INTERVAL = 10000;
  const CATALOG_ROUTE = "/api/catalog";
  const SETTINGS_ROUTE = "/api/settings";
  const COMPACT_STORAGE_KEY = "spec-tracker-compact-view";
  // The terminal-row preference mirrors the compact-view preference: it is a
  // browser-local display choice applied to the catalog already fetched.  It
  // never changes setup, the workspace, or the catalog, and it is unrelated to
  // the account's hidden-stage policy, which removes a row from the board
  // before the browser ever sees it.  A finished stage that is also configured
  // hidden therefore stays hidden even when this box is unchecked.
  const TERMINAL_STORAGE_KEY = "spec-tracker-hide-terminal-rows";
  const DESELECT_STORAGE_KEY = "spec-tracker-deselect-on-empty-click";

  const board = document.querySelector("#board");
  const toolbar = document.querySelector(".toolbar");
  const filter = document.querySelector("#filter");
  const compactControl = document.querySelector("#compact-view");
  const terminalControl = document.querySelector("#hide-terminal-rows");
  const deselectControl = document.querySelector("#deselect-on-empty-click");
  const refreshButton = document.querySelector("#refresh");
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
  let railPlan = new Map();
  let railColumns = [];
  let railEdges = [];
  let compactView = true;
  let hideTerminalRows = true;
  let deselectOnEmptyClick = false;
  let refreshFailure = null;
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

  function text(value) {
    const raw = value === null || value === undefined || value === "" ? "Unknown" : String(value);
    return raw.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
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
      if (byId.get(entry.package_id) !== entry) return;
      (entry.relationship.prerequisites || []).forEach((edge) => {
        if (!prerequisiteTarget(edge, byId)) return;
        if (!index.has(edge.target_package_id)) index.set(edge.target_package_id, []);
        const dependents = index.get(edge.target_package_id);
        if (!dependents.includes(entry)) dependents.push(entry);
      });
    });
    return index;
  }

  function prerequisiteTargets(entry) {
    const seen = new Set();
    const targets = [];
    (entry.relationship.prerequisites || []).forEach((edge) => {
      const key = edge.target_package_id || "";
      if (seen.has(key)) return;
      seen.add(key);
      targets.push(edge);
    });
    return targets;
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

  function rowKeyOf(entry) {
    return entry.board_visible ? rowDataKeyOf(entry.stage) : null;
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

  function canonicalStageNames(snapshot = displayed) {
    if (!snapshot) return [];
    const hidden = new Set(snapshot.visibility.hidden_stages || []);
    const names = [];
    const seen = new Set();
    (snapshot.inventory.stages || []).forEach((record) => {
      if (hidden.has(record.stage) || seen.has(record.stage)) return;
      seen.add(record.stage);
      names.push(record.stage);
    });
    return names;
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
    const eligible = canonicalStageNames(snapshot);
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

  function renderStageOrderEditor({focusStage = null} = {}) {
    stageOrderControls.hidden = !settingsAvailable && !settingsReloadAvailable;
    if (stageOrderControls.hidden) return;
    if (settingsAvailable && !editorStageOrder.length) editorStageOrder = editorNames();
    const controlsDisabled = !settingsAvailable || settingsBusy;
    stageOrderList.innerHTML = editorStageOrder.map((stage, index) => {
      const label = stageLabelOf(stage);
      const dormant = inventoryStageNames().includes(stage) ? "" :
        '<span class="stage-order-dormant">(not currently available)</span>';
      const completed = editorCompletedStages.includes(stage);
      return `<li class="stage-order-item" data-stage="${text(stage)}">` +
        `<span class="stage-order-name">${text(label)}${dormant}</span>` +
        `<label class="stage-completed"><input type="checkbox" class="stage-completed-toggle" ` +
        `aria-label="Counts as finished: ${text(label)}" data-stage="${text(stage)}"` +
        `${completed ? " checked" : ""}${controlsDisabled ? " disabled" : ""}>` +
        `Counts as finished</label>` +
        `<button type="button" class="stage-order-move" data-stage-move="up" ` +
        `aria-label="Move ${text(label)} up"${controlsDisabled || index === 0 ? " disabled" : ""}>Move up</button>` +
        `<button type="button" class="stage-order-move" data-stage-move="down" ` +
        `aria-label="Move ${text(label)} down"${controlsDisabled || index === editorStageOrder.length - 1 ? " disabled" : ""}>Move down</button>` +
        `</li>`;
    }).join("");
    stageOrderSave.disabled = controlsDisabled || !hasUnsavedStageOrder();
    stageOrderCancel.disabled = controlsDisabled || !hasUnsavedStageOrder();
    stageOrderReset.disabled = controlsDisabled;
    stageOrderReload.hidden = !settingsReloadAvailable;
    stageOrderReload.disabled = settingsBusy;
    if (focusStage && settingsAvailable) {
      const item = [...stageOrderList.children].find((candidate) =>
        candidate.dataset.stage === focusStage);
      item?.querySelector("[data-stage-move]:not(:disabled)")?.focus();
    }
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
          row: rowKeyOf(source),
        });
      });
    });
    edges.sort((a, b) => a.source.localeCompare(b.source) || a.dependent.localeCompare(b.dependent));
    return edges;
  }

  function columnsFor(entries) {
    const columns = new Map();
    entries.forEach((entry) => {
      const key = columnKeyOf(entry);
      if (!columns.has(key)) columns.set(key, projectOf(entry));
    });
    return [...columns.entries()].sort((a, b) => {
      if (a[0] === "" || b[0] === "") return a[0] === b[0] ? 0 : a[0] === "" ? 1 : -1;
      return a[1].localeCompare(b[1]);
    });
  }

  function orderCell(cell, depth) {
    return [...cell].sort((a, b) => {
      const depthA = a.package_id ? depth.get(a.package_id) ?? 0 : 0;
      const depthB = b.package_id ? depth.get(b.package_id) ?? 0 : 0;
      if (depthA !== depthB) return depthA - depthB;
      return titleOf(a).localeCompare(titleOf(b));
    });
  }

  // Keep cross-project names readable without requiring users to trace a long arrow.
  function needsHtml(entry, byId) {
    const parts = prerequisiteTargets(entry).map((edge) => {
      const target = prerequisiteTarget(edge, byId);
      if (!target) {
        // A valid edge may point to a hidden context entry. It is intentionally
        // absent from the interactive index, but it is not an unresolved target.
        if (edge.target_package_id && !UNRESOLVED_EDGE_REASONS.has(edge.reason)) return "";
        return '<span class="link unresolved">unresolved target</span>';
      }
      if (columnKeyOf(target) === columnKeyOf(entry)) return "";
      return `<span class="link cross">${text(titleOf(target))} ` +
        `<em>${text(projectOf(target))}</em></span>`;
    }).filter(Boolean);
    return parts.length ? `<span class="card-links">needs: ${parts.join(" · ")}</span>` : "";
  }

  function blocksHtml(entry, dependentsOf) {
    const parts = (dependentsOf.get(entry.package_id) || [])
      .filter((dependent) => columnKeyOf(dependent) !== columnKeyOf(entry))
      .map((dependent) => `<span class="link cross">${text(titleOf(dependent))} ` +
        `<em>${text(projectOf(dependent))}</em></span>`);
    return parts.length ? `<span class="card-links">blocks: ${parts.join(" · ")}</span>` : "";
  }

  function searchText(entry) {
    return [
      entry.package_path, entry.package_id, entry.stage, entry.relationship.program.title, entry.state,
      ...DECLARED_FIELDS.map(([field]) => entry.declared[field]),
    ].filter((value) => value !== null && value !== undefined).join(" ").toLowerCase();
  }

  function cardHtml(entry, byId, dependentsOf) {
    const selected = selectedPath === entry.package_path;
    const idAttribute = entry.package_id
      ? ` data-package-id="${text(entry.package_id)}"` : "";
    const targetProject = entry.declared.target_project &&
      entry.declared.target_project !== entry.project ? entry.declared.target_project : "";
    return `<button class="card${selected ? " selected" : ""}" ` +
      'type="button" ' +
      `aria-pressed="${selected}" data-package-path="${text(entry.package_path)}"` +
      `${idAttribute} data-search="${text(searchText(entry))}">` +
      `<span class="card-title">${text(titleOf(entry))}</span>` +
      (targetProject ? `<span class="card-project">Target Folder: ${text(targetProject)}</span>` : "") +
      `<span class="card-filepath">Filepath: ${text(entry.package_path)}</span>` +
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
  // so finished rows are treated like any other row.  Hidden stages are not
  // removed here; callers apply the hidden policy first, which keeps hidden
  // stages un-revealable even when a hidden stage is also marked finished.
  function gatedTerminalStages() {
    if (!hideTerminalRows) return new Set();
    return new Set(displayed?.visibility.terminal_stages || []);
  }

  function inventoryAxes(entries) {
    const hiddenStages = new Set(displayed?.visibility.hidden_stages || []);
    // Terminal gating is purely a browser display choice: it removes finished
    // rows from this render and touches neither the catalog nor the saved
    // policy.  It is deliberately separate from the hidden-stage policy, which
    // removes a row entirely; see the note on ``terminal_stages`` in the
    // catalog builder for why the two concepts are not merged.
    const gatedStages = gatedTerminalStages();
    const inventory = displayed?.inventory || { projects: [], stages: [] };
    const eligibleStages = [];
    const stages = [];
    const stageByKey = new Map();
    inventory.stages.forEach((record) => {
      if (hiddenStages.has(record.stage)) return;
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
    // Every admissible row is a finished row the reader chose to hide.  The
    // board uses this to explain the empty result instead of reporting that no
    // stage directories were found.
    const terminalHidden = hasEligibleStages && stages.length === 0;
    const projects = [];
    inventory.projects.forEach((record) => {
      const projectStages = inventory.stages.filter((stage) => stage.project === record.name);
      const hasStage = projectStages.some((stage) => !hiddenStages.has(stage.stage));
      const incompleteStage = projectStages.some((stage) =>
        !hiddenStages.has(stage.stage) && stage.availability === "incomplete");
      if (projectStages.length && !hasStage) return;
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

  function boardIssueHtml(snapshot) {
    const diagnostics = workspaceDiagnostics(snapshot);
    if (!diagnostics.length && !refreshFailure) return "";
    // Each part is its own element so one issue stays addressable when both
    // kinds are present; the separator keeps the summary text unchanged.
    const summary = [
      diagnostics.length
        ? `<span class="workspace-issue-summary">${text(`Workspace issues: ${
          diagnostics.map((item) => item.code).join(", ")} (${diagnostics.length})`)}</span>` : "",
      refreshFailure
        ? `<span class="refresh-issue-summary">${text(`Latest refresh issue: ${refreshFailure}`)}</span>` : "",
    ].filter(Boolean).join(" · ");
    const issueItems = diagnostics.map((item) =>
      `<li><code>${text(item.code)}</code> ${text(item.message)}</li>`).join("");
    const refreshItem = refreshFailure
      ? `<li><code>refresh</code> ${text(refreshFailure)}</li>` : "";
    return `<details class="board-issues" id="board-issues"><summary>${summary}</summary>` +
      `<ul class="diagnostics">${issueItems}${refreshItem}</ul></details>`;
  }

  function emptyBoardHtml(axes, entries) {
    if (!displayed) return '<p class="empty board-empty">No work items in the catalog.</p>';
    if (!displayed.entries.length && !displayed.inventory.projects.length && !displayed.inventory.stages.length) {
      return '<p class="empty board-empty">No work items in the catalog.</p>';
    }
    const hiddenStages = new Set(displayed.visibility.hidden_stages || []);
    // The reader hid finished rows and those were the only admissible rows, so
    // name that cause before the generic empty-board explanations below.
    if (axes.terminalHidden) {
      return '<p class="empty board-empty terminal-hidden">Finished rows are hidden. ' +
        'Uncheck “Hide terminal rows” to show them.</p>';
    }
    const incomplete = displayed.inventory.projects.some((project) => project.availability === "incomplete") ||
      displayed.inventory.stages.some((stage) =>
        !hiddenStages.has(stage.stage) && stage.availability === "incomplete");
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
      ? displayed.entries.filter((entry) => entry.board_visible && !gatedStages.has(entry.stage))
      : [];
    const byId = indexByPackageId(entries);
    const dependentsOf = indexDependents(entries, byId);
    const axes = inventoryAxes(entries);
    const issues = boardIssueHtml(displayed);
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
      board.innerHTML = issues + '<h2 class="board-corner" aria-hidden="true"></h2>' +
        axes.projects.map((project) => `<h2 class="column-head">${text(project.project)}${dimensionNotice(project)}</h2>`).join("") +
        '<p class="empty board-empty no-eligible-stages">No eligible stage directories were found.</p>';
      return;
    }
    if (!axes.stages.length || !axes.projects.length) {
      setBoardColumns([]);
      board.innerHTML = issues + emptyBoardHtml(axes, entries);
      return;
    }
    railEdges = planEdges(entries, byId);
    const bands = new Map();
    railEdges.forEach((edge) => {
      if (edge.sourceColumn === edge.dependentColumn) return;
      edge.bandLane = bands.get(edge.row) || 0;
      bands.set(edge.row, edge.bandLane + 1);
    });
    const plans = new Map(columns.map(([key]) => {
      const columnEntries = entries.filter((entry) => columnKeyOf(entry) === key);
      const depth = depthForColumn(columnEntries, railEdges);
      const edges = railEdges.filter((edge) =>
        edge.sourceColumn === key || edge.dependentColumn === key);
      // Reserve distinct lanes for local arrows and both ends of cross-project arrows.
      railPlan.set(key, { edges });
      return [key, { depth, reserved: edges.length }];
    }));
    setBoardColumns(columns.map(([key]) => {
      const reserved = plans.get(key).reserved;
      return reserved ? reserved * RAIL_PITCH + RAIL_INSET : 0;
    }));
    let html = issues + (!entries.length
      ? `<p class="empty board-empty admitted-empty">${axes.stages.some((stage) => stage.availability === "incomplete")
        ? "The catalog has incomplete dimensions; no work items are currently available."
        : "The catalog contains admitted folders but no work items."}</p>` : "") +
      '<h2 class="board-corner" aria-hidden="true"></h2>' +
      axes.projects.map((project) => `<h2 class="column-head">${text(project.project)}${dimensionNotice(project)}</h2>`).join("");
    axes.stages.forEach((stage) => {
      const key = stage.key;
      const rowKey = rowDataKeyOf(stage.stage);
      const rowEntries = entries.filter((entry) => stageKeyOf(entry.stage) === key);
      const cells = columns.map(([columnKey]) => {
        const { depth, reserved } = plans.get(columnKey);
        const gutter = reserved ? reserved * RAIL_PITCH + RAIL_INSET : 0;
        return cellHtml(
          orderCell(rowEntries.filter((entry) => columnKeyOf(entry) === columnKey), depth),
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
    applyFilter();
  }

  function applyFilter() {
    const query = filter.value.trim().toLowerCase();
    board.querySelectorAll(".card").forEach((card) => {
      card.hidden = Boolean(query) && !card.dataset.search.includes(query);
    });
    board.querySelectorAll(".cell").forEach((cell) => {
      const cards = [...cell.querySelectorAll(".card")];
      cell.classList.toggle("filtered-empty", cards.length > 0 && cards.every((card) => card.hidden));
    });
    drawRails();
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
    const incoming = new Set(visibleEdges.map((edge) => edge.dependent));
    board.querySelectorAll(".card").forEach((card) => {
      card.classList.toggle("no-incoming-arrow", !card.hidden && !incoming.has(card.dataset.packageId));
    });
    if (!visibleEdges.length) return;
    const selected = [...cards.values()].find((card) => card.dataset.packagePath === selectedPath);
    const selectedId = selected?.dataset.packageId;
    const related = (edge) => edge.source === selectedId || edge.dependent === selectedId;
    const bands = new Map([...board.querySelectorAll(".board-row")].map((row) =>
      [row.dataset.lifecycle, row.querySelector(".connection-band")]));
    const laneX = (key, edge) => {
      const columnLeft = heads[railColumns.indexOf(key)].offsetLeft;
      return columnLeft + (railPlan.get(key).edges.indexOf(edge) + .5) * RAIL_PITCH;
    };
    const port = (id, edge) => {
      const rect = cards.get(id).getBoundingClientRect();
      const incident = visibleEdges.filter((item) => item.source === id || item.dependent === id);
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
    [...visibleEdges].sort((a, b) => Number(related(a)) - Number(related(b))).forEach((edge) => {
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
      protocol(exactKeys(claim, ["diagnostics", "evidence_ref", "name", "state"]));
      protocol(/^[a-z][a-z0-9-]{0,63}$/.test(claim.name));
      protocol(["satisfied", "unsatisfied", "unknown"].includes(claim.state));
      provenance(claim.evidence_ref, true);
      diagnostics(claim.diagnostics);
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
    protocol(exactKeys(value.program, ["diagnostics", "program_id", "resolution", "title"]));
    uuid(value.program.program_id, true);
    protocol(["not_declared", "resolved", "unknown"].includes(value.program.resolution));
    protocol(value.program.title === null || safeText(value.program.title));
    diagnostics(value.program.diagnostics);
    protocol(exactKeys(value.superseded_by, ["diagnostics", "package_id", "resolution"]));
    uuid(value.superseded_by.package_id, true);
    protocol(["not_declared", "resolved", "unknown"].includes(value.superseded_by.resolution));
    diagnostics(value.superseded_by.diagnostics);
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

  async function authenticate(snapshot) {
    const unsigned = {...snapshot};
    delete unsigned.catalog_digest;
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonicalJson(unsigned)));
    const actual = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    protocol(actual === snapshot.catalog_digest);
    return snapshot;
  }

  function validateSnapshot(snapshot) {
    const top = ["catalog_digest", "configuration_revision", "discovery_diagnostics", "entries", "identity_coverage",
      "inventory", "program_coverage", "programs", "schema_version", "visibility"];
    protocol(exactKeys(snapshot, top) && snapshot.schema_version === 6 &&
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
    if (!exactKeys(snapshot.visibility, ["hidden_stages", "terminal_stages",
      "visible_entry_count", "hidden_entry_count"]) ||
        !Array.isArray(snapshot.visibility.hidden_stages) ||
        !Array.isArray(snapshot.visibility.terminal_stages) ||
        !Number.isInteger(snapshot.visibility.visible_entry_count) || snapshot.visibility.visible_entry_count < 0 ||
        !Number.isInteger(snapshot.visibility.hidden_entry_count) || snapshot.visibility.hidden_entry_count < 0) {
      throw new Error("producer_protocol_error");
    }
    snapshot.visibility.hidden_stages.forEach((stage) => protocol(component(stage)));
    protocol(snapshot.visibility.hidden_stages.every((stage, index) =>
      index === 0 || scalarCompare(stage, snapshot.visibility.hidden_stages[index - 1]) > 0));
    // Terminal stages are the account's completion policy, carried for the
    // "Hide terminal rows" grouping.  Overlap with hidden_stages is legal: the
    // hidden policy is applied first, so a stage that is both stays hidden.
    snapshot.visibility.terminal_stages.forEach((stage) => protocol(component(stage)));
    protocol(snapshot.visibility.terminal_stages.every((stage, index) =>
      index === 0 || scalarCompare(stage, snapshot.visibility.terminal_stages[index - 1]) > 0));
    diagnostics(snapshot.discovery_diagnostics);
    [snapshot.identity_coverage, snapshot.program_coverage].forEach((coverage) => {
      protocol(exactKeys(coverage, ["diagnostics", "state"]) && ["complete", "incomplete"].includes(coverage.state));
      diagnostics(coverage.diagnostics);
    });
    snapshot.programs.forEach((program) => {
      protocol(exactKeys(program, ["diagnostics", "member_package_ids", "program_id", "title"]));
      uuid(program.program_id);
      protocol(safeText(program.title));
      protocol(Array.isArray(program.member_package_ids));
      program.member_package_ids.forEach((member) => uuid(member));
      diagnostics(program.diagnostics);
    });
    const hidden = new Set(snapshot.visibility.hidden_stages);
    let visible = 0;
    const paths = [];
    snapshot.entries.forEach((entry) => {
      protocol(exactKeys(entry, ["board_visible", "declared", "diagnostics", "package_id", "package_path",
        "project", "relationship", "stage", "state", "transitive_diagnostics"]));
      uuid(entry.package_id, true);
      protocol(component(entry.project) && component(entry.stage) &&
        inventoryStages.has(`${entry.project}\u0000${entry.stage}`) && safeText(entry.package_path) &&
        !(entry.package_path.startsWith("/") || entry.package_path.includes("\\") ||
          entry.package_path.split("/").some((part) => !part || part === "." || part === "..")) &&
        (entry.package_path === `${entry.project}/${entry.stage}` ||
          entry.package_path.startsWith(`${entry.project}/${entry.stage}/`)) &&
        typeof entry.board_visible === "boolean" && entry.board_visible === !hidden.has(entry.stage) &&
        ["complete", "partial"].includes(entry.state));
      protocol(exactKeys(entry.declared, ["closure", "human_sanity_decision", "sanity_recommendation",
        "status", "target_project", "title"]));
      Object.values(entry.declared).forEach((value) => protocol(declaredText(value)));
      diagnostics(entry.diagnostics);
      relationship(entry.relationship);
      protocol(Array.isArray(entry.transitive_diagnostics) && entry.transitive_diagnostics.length === 0);
      paths.push(entry.package_path);
      if (entry.board_visible) visible += 1;
    });
    protocol(paths.every((path, index) => index === 0 ||
      scalarCompare(path, paths[index - 1]) >= 0) &&
      new Set(paths).size === paths.length);
    if (visible !== snapshot.visibility.visible_entry_count ||
        snapshot.visibility.hidden_entry_count < snapshot.entries.length - visible) {
      throw new Error("producer_protocol_error");
    }
    return authenticate(snapshot);
  }

  function setPending(available) {
    refreshButton.dataset.pending = String(available);
    refreshButton.classList.toggle("pending", available);
    refreshButton.textContent = available ? "Apply update" : "Refresh view";
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
        settingsStatus("Current board row order loaded.");
        renderStageOrderEditor();
        if (displayed) renderBoard();
        if (refreshCatalog) request("manual");
      })
      .catch((error) => {
        if (serial !== settingsRequestSerial) return;
        settingsAvailable = false;
        settingsReloadAvailable = true;
        settingsRevision = null;
        settingsStatus(`Could not load board row order: ${settingsErrorMessage(error)}`, true);
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
    settingsStatus("Saving board row order…");
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
          ? `Save not applied: ${payload.outcome}. Reload the current board row order to replace this unsaved draft and try again.`
          : "Save failed: the board row order was not persisted.";
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
      settingsStatus("Board row order saved.");
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
    setPending(false);
    editorStageOrder = [...new Set([
      ...(retainDraft ? priorEditor : editorNames(snapshot)),
      ...inventoryStageNames(snapshot),
    ])];
    if (!retainDraft) editorCompletedStages = [...savedCompletedStages];
    if (selectedPath && !snapshot.entries.some((entry) =>
      entry.board_visible && entry.package_path === selectedPath)) {
      selectedPath = null;
    }
    renderBoard();
    renderStageOrderEditor();
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

  function safeCategory(error) {
    return SAFE_CATEGORIES.includes(error.message) ? error.message : "producer_unavailable";
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
        if (kind === "manual" || kind === "settings" || !displayed) { apply(snapshot); return; }
        if (digestOf(snapshot) !== digestOf(displayed)) {
          pending = snapshot;
          setPending(true);
        } else {
          pending = null;
          setPending(false);
        }
        if (hadRefreshFailure) {
          renderBoard();
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
        refreshFailure = safeCategory(error);
        renderBoard();
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
  refreshButton.addEventListener("click", () => {
    if (pending) apply(pending);
    else request("manual");
  });
  stageOrderList.addEventListener("click", (event) => {
    const move = event.target.closest("[data-stage-move]");
    const item = event.target.closest("[data-stage]");
    if (!settingsAvailable || settingsBusy || !move || !item) return;
    const stage = item.dataset.stage;
    const index = editorStageOrder.indexOf(stage);
    const target = move.dataset.stageMove === "up" ? index - 1 : index + 1;
    if (index < 0 || target < 0 || target >= editorStageOrder.length) return;
    [editorStageOrder[index], editorStageOrder[target]] =
      [editorStageOrder[target], editorStageOrder[index]];
    renderStageOrderEditor({focusStage: stage});
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
    settingsStatus("Unsaved board row order changes cancelled.");
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
  loadSettings();
  request("manual");
  window.setInterval(() => request("poll"), POLL_INTERVAL);
})();
