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
  ];
  const SVG_NS = "http://www.w3.org/2000/svg";
  const RAIL_PITCH = 14;
  const RAIL_INSET = 20;
  const RAIL_LANE_CAP = 8;
  const POLL_INTERVAL = 10000;
  const CATALOG_ROUTE = "/api/catalog";
  const SETTINGS_ROUTE = "/api/settings";
  const WORKSPACE_ROUTE = "/api/workspace";
  const COMPACT_STORAGE_KEY = "spec-tracker-compact-view";
  // The terminal-row preference mirrors the compact-view preference: it is a
  // browser-local display choice applied to the catalog already fetched.  It
  // never changes setup, the workspace, or the catalog.  Unchecking the box
  // makes finished stages eligible for display again.
  const TERMINAL_STORAGE_KEY = "spec-tracker-hide-terminal-rows";
  const DESELECT_STORAGE_KEY = "spec-tracker-deselect-on-empty-click";

  const board = document.querySelector("#board");
  const toolbar = document.querySelector(".toolbar");
  const filter = document.querySelector("#filter");
  const compactControl = document.querySelector("#compact-view");
  const terminalControl = document.querySelector("#hide-terminal-rows");
  const deselectControl = document.querySelector("#deselect-on-empty-click");
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

  function searchText(entry) {
    return [
      entry.package_path, entry.package_id, entry.stage, entry.relationship.program.title,
      ...DECLARED_FIELDS.map((field) => entry.declared[field]),
      ...entry.reported_fields.map((field) => field.value),
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
    if (!diagnostics.length && !refreshFailure) {
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
    ].filter(Boolean).join(" · ");
    const issueItems = diagnostics.map((item) =>
      `<li><code>${text(item.code)}</code> ${text(item.message)}</li>`).join("");
    const refreshItem = refreshFailure
      ? `<li><code>refresh</code> ${text(refreshFailure)}</li>` : "";
    if (!panel) {
      panel = document.createElement("details");
      panel.id = "board-issues";
      panel.className = "board-issues";
      panel.innerHTML = '<summary></summary><ul class="diagnostics"></ul>';
    }
    panel.querySelector("summary").innerHTML = summary;
    panel.querySelector("ul").innerHTML = issueItems + refreshItem;
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
        inventoryStages.has(`${entry.project}\u0000${entry.stage}`) && safeText(entry.package_path) &&
        !(entry.package_path.startsWith("/") || entry.package_path.includes("\\") ||
          entry.package_path.split("/").some((part) => !part || part === "." || part === "..")) &&
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
    renderStageOrderEditor();
    if (!workspace || workspace.revision !== snapshot.configuration_revision) loadWorkspace();
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
        const failure = safeCategory(error);
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
  refreshButton.addEventListener("click", () => {
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
