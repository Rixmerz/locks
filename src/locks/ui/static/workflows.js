// One project's workflow runs: each a graph of its steps — what passed, what runs, what
// waits on the person — and how far it got; then the workflows it can run. Read here:
// runs are started, retried and cancelled from a session (`locks workflow …`).
import { get } from "./net.js";
import { notifyButton, watchEnding } from "./notify.js";
import { curve, label, s } from "./svg.js";
import { cost, h, short, statusBadge, tabTitle, when } from "./util.js";

const COL = 210, ROW = 64, W = 176, BOX = 46, PAD = 12;
const STOPPED_RUN = new Set(["waits on the orchestrator", "stalled"]);
const WAITS = new Set(["failed", "blocked", "replan", "needs_approval", "interrupted", "cancelled", "dismissed"]);
const stateClass = (state) => (state === "done" ? "done" : state === "running" || state === "queued" ? "now"
  : WAITS.has(state) ? "waits" : "pending");

/** Columns by depth (a step sits one column right of the deepest step it needs), rows in order. */
function layout(steps) {
  const depth = {};
  for (const step of steps) depth[step.id] = 1 + Math.max(-1, ...step.needs.map((n) => depth[n] ?? 0));
  const rows = {};
  const at = {};
  for (const step of steps) {
    const d = depth[step.id];
    rows[d] = (rows[d] ?? 0) + 1;
    at[step.id] = { x: PAD + d * COL, y: PAD + (rows[d] - 1) * ROW };
  }
  const width = PAD * 2 + (Math.max(...Object.values(depth)) + 1) * COL - (COL - W);
  const height = PAD * 2 + (Math.max(...Object.values(rows)) - 1) * ROW + BOX;
  return { at, width, height };
}

// What a step is, in words its node carries: a question and its answer, another workflow's run, a skip.
function what(step) {
  if (step.skipped) return "skipped";
  if (step.asks) return step.answer ? `answered ${step.answer}` : `asks: ${step.asks.join(" / ")}`;
  if (step.runs) return `runs ${step.runs}${step.subrun ? ` #${step.subrun}` : ""}`;
  return null;
}

function graph(run, ctx) {
  const { at, width, height } = layout(run.steps);
  const edges = run.steps.flatMap((step) => step.needs.map((need) => {
    const x1 = at[need].x + W, y1 = at[need].y + BOX / 2, x2 = at[step.id].x, y2 = at[step.id].y + BOX / 2;
    const cond = (step.when || {})[need];
    return [s("path", { class: `wf-edge ${stateClass(run.steps.find((x) => x.id === need)?.state)}`,
                        d: curve(x1, y1, x2, y2) }),
      // The outcome a step's `when` waits for, on the edge it reads.
      // Over the step's box, where the gap between two boxes is too narrow for its words.
      cond ? label(`when ${cond}`, 24, { x: x2 + 4, y: y2 - BOX / 2 - 3, class: "wf-cond" }) : []];
  }));
  const nodes = run.steps.map((step) => {
    const { x, y } = at[step.id];
    const open = step.task ? () => ctx.openTask(step.task) : null;
    const sub = [what(step) || step.state, step.parts ? `${step.parts.length} parts` : step.cell,
      step.tries > 1 ? `try ${step.tries}` : null].filter(Boolean).join(" · ");
    const kind = step.skipped ? " skipped" : step.asks ? " asks" : step.runs ? " runs" : "";
    return s("g", {
      class: `wf-node ${stateClass(step.state)}${kind}${open ? " link" : ""}`, transform: `translate(${x},${y})`,
      tabindex: open ? 0 : null, role: open ? "button" : null, onclick: open,
      onkeydown: open ? (e) => { if (e.key === "Enter") open(); } : null,
    }, s("title", {}, `${step.id}: ${step.title}\n${sub}${step.skipped ? `\n\n${step.skipped}` : ""}`
                      + `${step.result ? `\n\n${step.result}` : ""}`),
    s("rect", { width: W, height: BOX, rx: step.asks ? BOX / 2 : 8 }),
    // Another workflow's run: a box inside the box, as a subroutine is drawn.
    step.runs ? s("rect", { class: "wf-inner", x: 4, y: 4, width: W - 8, height: BOX - 8, rx: 6 }) : [],
    label(step.title || step.id, 24, { x: 10, y: 19, class: "wf-title" }),
    label(sub, 30, { x: 10, y: 36, class: "wf-sub" }));
  });
  return s("svg", { class: "wf-graph", width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
                    "aria-label": `steps of run ${run.id}` }, edges, nodes);
}

function runCard(run, ctx) {
  const { done, total } = run.progress;
  const goal = Object.entries(run.inputs).map(([k, v]) => `${k}: ${v}`).join(" · ");
  // Set through the CSSOM: the page's policy refuses inline style attributes.
  const bar = h("span", {});
  bar.style.width = `${total ? (100 * done) / total : 0}%`;
  const focus = run.id === ctx.focusRun;
  if (focus) {
    // A run that waits or stalled wears a task's waiting state in the tab: its own words say which.
    tabTitle(STOPPED_RUN.has(run.state) ? "blocked" : run.state, `${run.name} run #${run.id}`, run.state);
    watchEnding(`run:${run.id}`, run.state, `${run.name} run #${run.id}`,
      run.waiting.length ? `${run.state} at ${run.waiting.join(", ")}` : run.state);
  }
  return h("div", { class: `card wf-run${focus ? " focus" : ""}`, id: `run-${run.id}` },
    h("div", { class: "row" },
      h("b", {}, `#${run.id} ${run.name}`), h("span", { class: "chip" }, `v${run.version} · ${run.scope}`),
      h("span", { class: `tag ${stateClass(run.state === "waits on the orchestrator" ? "failed" : run.state)}` }, run.state),
      h("span", { class: "meta" }, `${cost(run)}${run.points ? ` · ${run.points} points` : ""} · ${when(run.updated_at)}`),
      focus ? notifyButton() : null),
    goal ? h("div", { class: "text" }, short(goal, 300)) : null,
    h("div", { class: "progress", title: `${done} of ${total} steps passed` },
      bar, h("em", {}, `${done}/${total}`)),
    h("div", { class: "wf-scroll" }, graph(run, ctx)),
    // A step run in parts (a plan's, or a large review's areas): each part under it, with its own task.
    run.steps.filter((st) => st.parts).map((st) => h("div", { class: "row wf-parts" },
      h("span", { class: "meta" }, `${st.id} in parts:`),
      st.parts.map((p) => h("button", { class: "chip", title: p.result || "", onclick: () => ctx.openTask(p.task) },
        `${p.id} `, statusBadge(p.state))))),
    run.waiting.length ? h("div", { class: "asks" },
      `waits on the orchestrator at ${run.waiting.join(", ")}: open the step for why; go on with locks resume <task> `
      + `or locks workflow retry ${run.id} <step> in a session`) : null,
    // Queued after other work (`--after`): it starts when that work passes, on what it kept.
    run.waits ? h("div", { class: "reason" }, `It ${run.waits}`) : null,
    run.branch ? h("div", { class: "meta" }, "work so far on ", h("code", {}, run.branch)) : null,
    // How far a step's work is behind the branch the run started from: said, never merged.
    run.steps.filter((st) => st.behind).map((st) => h("div", { class: "meta" }, `${st.id}: ${st.behind}`)));
}

function definitions(list) {
  if (!list.length) return h("div", { class: "empty" }, "No workflow to run here.");
  return h("table", { class: "grid" },
    h("thead", {}, h("tr", {}, ["Workflow", "Scope", "Steps", "Runs here", "In use"].map((t) => h("th", {}, t)))),
    h("tbody", {}, list.map((d) => h("tr", {},
      h("td", {}, h("b", {}, d.name), d.version ? ` v${d.version}` : "", h("div", { class: "meta" }, d.error || d.description || "")),
      h("td", {}, d.scope),
      h("td", {}, (d.steps || []).map((st, i) => [i ? " → " : "", st.id,
        st.memory?.length ? h("span", { class: "chip", title: st.memory.join(", ") }, `+${st.memory.length} memory`) : null])),
      // Its record here (`api.workflow_record`): observed, never a budget.
      h("td", {}, d.record ? `${d.record.done} of ${d.record.runs} done`
        + (d.record.cost_p50 != null ? `, $${d.record.cost_p50.toFixed(2)} a run` : "") : "\u2014"),
      h("td", {}, d.held_by?.length ? `run ${d.held_by.map((r) => `#${r}`).join(", ")}` : "—",
        d.pending ? h("span", { class: "tag review" }, "change waits") : null)))));
}

export async function renderWorkflows(ctx) {
  const data = await get(`/api/workflows?repo=${encodeURIComponent(ctx.project)}`);
  const active = data.runs.filter((r) => !["done", "cancelled"].includes(r.state));
  const ended = data.runs.filter((r) => ["done", "cancelled"].includes(r.state));
  return h("div", {},
    h("h2", {}, "Runs going on"),
    active.length ? active.map((r) => runCard(r, ctx))
      : h("div", { class: "empty" }, "No workflow runs here now. A session starts one with locks workflow run <name> \"<goal>\"."),
    ended.length ? [h("h2", { class: "spaced" }, "Ended"), ended.map((r) => runCard(r, ctx))] : null,
    h("h2", { class: "spaced" }, "Workflows this project can run"),
    definitions(data.definitions));
}
