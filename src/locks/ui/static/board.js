import { workshop } from "./workers.js";
import { get, post } from "./net.js";
import { stopCard } from "./stop.js";
import { cellChip, cost, h, kindChip, pointsChip, short, statusBadge, when } from "./util.js";

// Where a queued task stands with the dispatcher: Haiku's call on whether it may
// start beside the running work, with its reason. The card's reason line names the
// task it waits for (`waits`, from the API) when the dispatcher held it back.
function decision(task) {
  if (task.parallel === null || task.parallel === undefined) return null;
  return h("span", { class: task.parallel ? "chip ok" : "chip wait", title: task.parallel_reason || "" },
    task.parallel ? "runs beside others" : "waits its turn");
}

function card(task, openTask, extra = []) {
  return h("div", { class: `card s-${task.status}`, onclick: () => openTask(task.id), tabindex: 0,
                    onkeydown: (e) => { if (e.key === "Enter") openTask(task.id); } },
    h("div", { class: "title" }, short(task.title, 120)),
    h("div", { class: "meta" },
      kindChip(task.kind),
      cellChip(task.current_cell || task.final_cell || task.start_cell),
      pointsChip(task.points, task.points_why),
      task.cost_usd || task.cost_running ? h("span", {}, cost(task)) : null,
      ...extra),
    task.stop ? stopCard(task.stop) : task.asks ? h("div", { class: "asks" }, task.asks) : null,
    task.status === "queued" && (task.waits || task.parallel_reason)
      ? h("div", { class: "reason" }, short(task.waits || task.parallel_reason, 160)) : null);
}

function column(title, count, children, cls = "") {
  return h("section", { class: `column ${cls}`.trim() },
    h("h2", {}, title, h("span", { class: "count" }, String(count))),
    children.length ? children : h("div", { class: "empty" }, "nothing here"));
}

// The session filter is kept per project while the page is open.
const sessionFilter = new Map();

export async function renderBoard(ctx) {
  const q = encodeURIComponent(ctx.project);
  const [b, sessions] = await Promise.all([get(`/api/board?repo=${q}`), get(`/api/sessions?repo=${q}`).catch(() => [])]);
  const chosen = sessionFilter.get(ctx.project) || "";
  const keep = (t) => !chosen || t.session_id === chosen;
  const picker = h("select", { "aria-label": "session", onchange: (e) => { sessionFilter.set(ctx.project, e.target.value); ctx.refresh(); } },
    h("option", { value: "" }, "every session of this project"),
    ...sessions.map((s) => h("option", { value: s.id, selected: s.id === chosen },
      `${short(s.name || s.last_prompt || s.id, 60)}, ${when(s.last_seen_at)}`)));

  const lanes = b.queued.map((lane) => h("div", { class: "lane" },
    h("div", { class: "lane-head" },
      lane.paused
        ? h("span", { class: "paused" }, "paused ",
            h("button", { onclick: async (e) => { e.stopPropagation(); await post("/api/lanes/unpause", { repo: lane.repo }); ctx.toast("lane unpaused"); ctx.refresh(); } }, "Unpause"))
        : lane.dispatching
          ? h("span", { class: "live-note" }, "dispatcher running")
          : lane.tasks.length
            ? h("button", { onclick: async () => { try { await post("/api/work", { repo: lane.repo }); ctx.toast("dispatcher started"); } catch (err) { ctx.toast(err.message); } } }, "Start")
            : null),
    lane.paused && lane.reason ? h("div", { class: "asks" }, short(lane.reason, 160)) : null,
    // Its share of the machine's workers: what it is reserved, runs, and lends or borrows.
    lane.share ? h("div", { class: "meta" }, `reserve ${lane.share.reserve} · runs ${lane.share.running}`
      + (lane.share.lent ? ` · lends ${lane.share.lent}` : lane.share.borrowed ? ` · borrows ${lane.share.borrowed}` : "")) : null,
    lane.tasks.filter(keep).map((t) => card(t, ctx.openTask, [decision(t)]))));
  const days = new Map();
  for (const t of b.done.filter(keep)) {
    const day = (t.updated_at || "").slice(0, 10);
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(card(t, ctx.openTask, t.branch ? [h("span", { class: "chip" }, t.branch)] : []));
  }
  const diary = [...days.entries()].map(([day, cards]) => [h("div", { class: "lane-head" }, day), cards]);
  // What only the person clears, then what the orchestrating session decides itself.
  const needs = b.needs_you.filter(keep).filter((t) => t.waits_on !== "orchestrator");
  const deciding = b.needs_you.filter(keep).filter((t) => t.waits_on === "orchestrator");
  const running = b.running.filter(keep);
  const answering = chosen ? b.answering.filter(keep).length : b.counts.answering;
  return h("div", {},
    h("div", { class: "toolbar" }, picker),
    workshop(running, ctx.openTask),
    h("div", { class: "columns" },
      // Nothing waits on the person here: the orchestrating session decides, or asks them in its conversation.
      column("For the orchestrator", needs.length + deciding.length, [
        ...(needs.length ? [h("div", { class: "lane-head" }, `it asks you (${needs.length})`),
          ...needs.map((t) => card(t, ctx.openTask, [statusBadge(t.status)]))] : []),
        ...(deciding.length ? [h("div", { class: "lane-head" }, `it decides (${deciding.length})`),
          ...deciding.map((t) => card(t, ctx.openTask, [statusBadge(t.status)]))] : [])],
        needs.length + deciding.length ? "needs" : ""),
      column("Running", running.length, [
        ...running.map((t) => card(t, ctx.openTask, [statusBadge("running"),
          t.phase ? h("span", { class: `chip phase p-${t.phase}` }, t.phase) : null, h("span", {}, `attempt ${t.attempt ?? 1}`),
          // How far it got: its stage, time against its kind's usual here, what it wrote, a quiet worker.
          t.progress_line ? h("span", { class: "progress-line" }, t.progress_line) : h("span", {}, when(t.updated_at))])),
        ...(answering ? [h("div", { class: "empty" }, `${answering} session${answering === 1 ? "" : "s"} answering a prompt now`)] : [])],
        "running"),
      column("Queued", b.queued.reduce((n, lane) => n + lane.tasks.filter(keep).length, 0), [
        // How many workers this machine runs at once, and why it is there now.
        b.capacity ? h("div", { class: "meta", title: b.capacity.ceiling ? `your max_parallel ${b.capacity.ceiling}` : "max_parallel auto" },
          `workers ${b.capacity.line}`) : null,
        ...lanes], "queued"),
      column("Done", b.done.filter(keep).length, diary, "done")));
}
