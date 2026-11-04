// The workshop: one small pixel-art scene per running task, so a glance tells whether its
// worker is reading, writing, waiting on locks' check, being inspected, or has gone quiet.
// Every state is read from what the board already says of the task (its phase, its stage,
// `quiet_s` from `progress.of`): the drawing invents nothing, and its label says the same in
// words. The sprites are drawn here, pixel by pixel; no asset is fetched.
import { h } from "./util.js";
import { get } from "./net.js";
import { notify, notifyButton } from "./notify.js";

const W = 48, H = 32, SCALE = 3;
const QUIET_S = 5 * 60;  // progress.IDLE_S

// What the scene shows, from the task's own records.
export function stateOf(t) {
  const p = t.progress || {};
  if (p.stage === "verifier") return "verifier";
  if (p.stage === "check") return "check";
  if (p.quiet_s != null && p.quiet_s >= QUIET_S) return "quiet";
  return t.phase || "starting";
}

export const LABELS = {
  starting: "starting up", exploring: "reading the code", implementing: "writing changes",
  checking: "running its own checks", check: "locks runs the check", verifier: "independent check reads it",
  quiet: "no tool call for a while (a long command, or stuck)",
};

// Sprites: one char per pixel. h hair, s skin, b body, l legs, k ink, . nothing.
const WORKER = [
  "..hhhh..",
  ".hhhhhh.",
  ".hsssss.",
  ".sskssk.",
  "..ssss..",
  ".bbbbbb.",
  "bbbbbbbb",
  "sbbbbbbs",
  ".bbbbbb.",
  ".ll..ll.",
  ".ll..ll.",
  ".kk..kk.",
];
const INSPECTOR = WORKER.map((row, i) => (i < 2 ? row.replace(/h/g, "v") : row.replace(/b/g, "w")));

function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  return {
    floor: v("--sunk", "#dfe4dd"), wall: v("--panel", "#f7f8f5"), line: v("--line", "#d3d9d2"),
    k: v("--ink", "#1d2622"), b: v("--accent", "#0f6155"), w: v("--warn", "#875207"),
    h: "#5a3a22", v: v("--k-plan", "#6250d6"), s: "#f0c8a0", l: v("--queue", "#4f6a86"),
    screen: v("--accent-soft", "#d5e6e0"), ok: v("--ok", "#25663a"), muted: v("--muted", "#4f5b55"),
  };
}

// Each worker wears its own colour, so two scenes side by side never read as one.
const SHIRTS = ["--accent", "--k-read", "--k-plan", "--k-debug", "--k-ui", "--k-docs"];
const shirt = (id) => getComputedStyle(document.documentElement).getPropertyValue(SHIRTS[id % SHIRTS.length]).trim();

function sprite(ctx, rows, x, y, pal, flip = false) {
  rows.forEach((row, j) => [...row].forEach((c, i) => {
    if (c === ".") return;
    ctx.fillStyle = pal[c];
    ctx.fillRect(x + (flip ? row.length - 1 - i : i), y + j, 1, 1);
  }));
}

function desk(ctx, pal, f, typing) {
  ctx.fillStyle = pal.muted; ctx.fillRect(22, 20, 20, 2); ctx.fillRect(24, 22, 1, 6); ctx.fillRect(39, 22, 1, 6);
  ctx.fillStyle = pal.k; ctx.fillRect(28, 10, 12, 9); ctx.fillRect(33, 19, 2, 1);
  ctx.fillStyle = pal.screen; ctx.fillRect(29, 11, 10, 7);
  ctx.fillStyle = pal.b;
  const lines = typing ? 1 + (f % 6) : 3;
  for (let i = 0; i < lines; i++) ctx.fillRect(30, 12 + i, 2 + ((i * 5 + 3) % 6), 1);
}

const scenes = {
  starting(ctx, pal, f) { desk(ctx, pal, f, false); sprite(ctx, WORKER, 6 + Math.min(f, 10), 16, pal); },
  exploring(ctx, pal, f) {
    desk(ctx, pal, f, false);
    const step = f % 20, x = 4 + (step < 10 ? step : 20 - step);
    sprite(ctx, WORKER, x, 16 + (f % 2), pal, step >= 10);
    ctx.fillStyle = pal.k; ctx.fillRect(x + (step >= 10 ? -2 : 9), 22, 2, 2);  // a magnifier
    ctx.fillStyle = pal.screen; ctx.fillRect(x + (step >= 10 ? -2 : 9), 22, 1, 1);
  },
  implementing(ctx, pal, f) {
    desk(ctx, pal, f, true);
    sprite(ctx, WORKER, 16, 16, pal);
    ctx.fillStyle = pal.s; ctx.fillRect(f % 2 ? 23 : 24, 19, 2, 1);  // hands on the keys
  },
  checking(ctx, pal, f) { scenes.check(ctx, pal, f); },
  check(ctx, pal, f) {
    desk(ctx, pal, f, false);
    sprite(ctx, WORKER, 10, 16, pal);
    ctx.fillStyle = pal.line; ctx.fillRect(4, 6, 40, 3);   // the check's progress bar
    ctx.fillStyle = pal.ok; ctx.fillRect(4, 6, 2 + (f % 38), 3);
  },
  verifier(ctx, pal, f) {
    desk(ctx, pal, f, false);
    sprite(ctx, WORKER, 6, 16, pal);
    sprite(ctx, INSPECTOR, 16 + (f % 8 < 4 ? 0 : 1), 16, pal, true);
    ctx.fillStyle = pal.wall; ctx.fillRect(14, 21, 3, 4);  // a clipboard
    ctx.fillStyle = pal.k; ctx.fillRect(15, 22 + (f % 3), 1, 1);
  },
  quiet(ctx, pal, f) {
    desk(ctx, pal, f, false);
    sprite(ctx, WORKER, 16, 17, pal);
    ctx.fillStyle = pal.muted;
    const z = f % 12;
    ctx.fillRect(20 + (z >> 2), 12 - (z >> 1), 2, 1); ctx.fillRect(21 + (z >> 2), 13 - (z >> 1), 1, 1);
    ctx.fillRect(20 + (z >> 2), 14 - (z >> 1), 2, 1);
  },
};

let timer = null;
const live = new Set();

function tick() {
  const still = !matchMedia("(prefers-reduced-motion: reduce)").matches;
  const pal = palette();
  for (const c of live) {
    if (!c.isConnected) { if (c.frame) live.delete(c); continue; }
    const ctx = c.getContext("2d");
    ctx.setTransform(SCALE, 0, 0, SCALE, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = pal.wall; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = pal.floor; ctx.fillRect(0, 28, W, 4);
    c.frame = still ? (c.frame || 0) + 1 : 3;
    if (c.dataset.office) {
      c.workers.forEach((w, i) => {
        ctx.setTransform(SCALE, 0, 0, SCALE, i * W * SCALE, 0);
        ctx.fillStyle = pal.wall; ctx.fillRect(0, 0, W, H);
        ctx.fillStyle = pal.floor; ctx.fillRect(0, 28, W, 4);
        (scenes[w.state] || scenes.starting)(ctx, { ...pal, b: shirt(w.id) || pal.b }, c.frame + i * 3);
      });
      ctx.setTransform(SCALE, 0, 0, SCALE, c.workers.length * W * SCALE, 0);
      ctx.fillStyle = pal.wall; ctx.fillRect(0, 0, 16, H);
      ctx.fillStyle = pal.floor; ctx.fillRect(0, 28, 16, 4);
      ctx.fillStyle = pal.muted; ctx.fillRect(2, 8, 12, 20);   // the door, the queue's count beside it
      ctx.fillStyle = pal.wall; ctx.fillRect(4, 10, 8, 18);
      const q = Math.min(Number(c.dataset.queued) || 0, 6);
      ctx.fillStyle = pal.l;
      for (let i = 0; i < q; i++) ctx.fillRect(6, 26 - i * 3, 4, 2);
    } else {
      (scenes[c.dataset.state] || scenes.starting)(ctx, { ...pal, b: shirt(Number(c.dataset.id)) || pal.b }, c.frame);
    }
  }
  if (!live.size) { clearInterval(timer); timer = null; }
}

// The terminal under the workshop: the chosen worker's tool calls as they happen (what
// `phase.calls` keeps: when, the tool, its signature, whether it went through; never an
// argument) and the diff its worktree holds so far. Read every 5 s while it is open.
let watching = null;

// A signature in words, as `progress.words` says it: it keeps no argument, so no file name.
export function words(sig) {
  const [kind, ...more] = String(sig || "").split(":");
  const rest = more.join(":");
  if (kind === "bash") return rest && rest !== "unknown" ? `running ${rest.replace(/-/g, " ")}` : "running a command";
  if (["read", "edit", "write"].includes(kind)) {
    const what = rest && rest !== "noext" ? `a .${rest} file` : "a file";
    return `${{ read: "reading", edit: "editing", write: "writing" }[kind]} ${what}`;
  }
  if (kind === "mcp") return rest ? `calling ${rest}` : "calling a tool";
  if (kind === "grep" || kind === "glob") return "searching the code";
  return kind ? `using ${kind}` : "";
}

const mins = (s) => (s < 60 ? `${Math.round(s)}s` : `${Math.round(s / 60)}m`);

// Time spent against how long tasks like it took here, when there are enough of them.
function timeBar(p) {
  const est = p.estimate;
  if (!p.elapsed_s || !est?.p90_s) return null;
  const fill = h("span", { class: `time-fill${p.elapsed_s > est.p90_s ? " over" : ""}` });
  fill.style.width = `${Math.min(100, (100 * p.elapsed_s) / est.p90_s)}%`;
  return h("span", { class: "time-bar", title: `${mins(p.elapsed_s)} of usually ${mins(est.p50_s)} (90% within ${mins(est.p90_s)})` }, fill);
}  // { id, timer }

function line(c) {
  const at = c.ts ? new Date(c.ts * (c.ts < 1e12 ? 1000 : 1)).toLocaleTimeString() : "";
  return h("div", { class: `term-line${c.ok ? "" : " bad"}` },
    h("span", { class: "term-at" }, at), h("span", { class: "term-tool" }, c.tool),
    h("span", { class: "term-sig" }, words(c.sig) || c.sig), h("span", { class: "term-ok" }, c.ok ? "✓" : "✗ refused or failed"));
}

function traced(c) {
  const at = c.ts ? new Date(c.ts).toLocaleTimeString() : "";
  return h("div", { class: "term-run" },
    h("div", {}, h("span", { class: "term-at" }, at), " ", h("span", { class: "term-tool" }, c.tool), " ",
      h("span", { class: "term-cmd" }, c.input ? (c.tool === "Bash" ? `$ ${c.input}` : c.input) : "")),
    c.output ? h("pre", { class: "term-out" }, c.output) : null);
}

function diffLines(text) {
  return String(text || "").split("\n").map((l) => h("div", {
    class: l.startsWith("+") && !l.startsWith("+++") ? "d-add" : l.startsWith("-") && !l.startsWith("---") ? "d-del"
      : l.startsWith("@@") ? "d-hunk" : l.startsWith("diff ") ? "d-file" : "" }, l || " "));
}

async function fill(pane, id) {
  const [live, diff] = await Promise.all([get(`/api/tasks/${id}/live`).catch(() => null),
    get(`/api/tasks/${id}/diff`).catch(() => null)]);
  if (!pane.isConnected) return stop();
  const calls = live?.recent_calls || [];
  pane.replaceChildren(...[
    h("div", { class: "term-head" }, h("span", { class: "live-dot", "aria-hidden": "true" }),
      `#${id} · attempt ${live?.attempt_now ?? "?"} · ${live?.progress_line || "no progress yet"}`,
      h("button", { class: "term-close", onclick: () => { stop(); pane.replaceChildren(); } }, "close")),
    (live?.progress?.criteria || []).length ? [h("div", { class: "term-title" }, "what it was asked"),
      live.progress.criteria.map((c) => h("div", { class: "term-sig" }, `${c.id} ${c.text}`))] : [],
    h("div", { class: "term-title" }, "tool calls"),
    h("div", { class: "term-calls" }, (live?.traced || []).length ? live.traced.map(traced)
      : calls.length ? calls.map(line) : h("div", { class: "term-sig" }, "no tool call yet")),
    live && !live.trace_on ? h("div", { class: "term-hint" },
      "commands and their output show here with the trace on: locks config trace on (kept in your locks home only)") : null,
    h("div", { class: "term-title" }, "code changed so far"),
    diff?.diff ? h("pre", { class: "term-diff" }, diffLines(diff.diff))
      : h("div", { class: "term-sig" }, diff?.why || "nothing written yet"),
    diff?.cut ? h("div", { class: "term-sig" }, "the diff is cut here; the task page has the rest") : null,
  ].flat(Infinity).filter(Boolean));
  const log = pane.querySelector(".term-calls");
  if (log) log.scrollTop = log.scrollHeight;
}

function stop() { if (watching) clearInterval(watching.timer); watching = null; }

function watch(pane, id) {
  stop();
  watching = { id, timer: setInterval(() => fill(pane, id), 5000) };
  fill(pane, id);
}

// What the person hears when they turned notices on: a worker that went quiet, one that ended.
const seen = new Map();
function notices(running) {
  const now = new Map(running.map((t) => [t.id, stateOf(t)]));
  for (const [id, was] of seen) {
    if (!now.has(id)) notify(`#${id} finished its run`, "open the board to see how it ended", `end-${id}`);
    else if (was !== "quiet" && now.get(id) === "quiet") notify(`#${id} went quiet`, LABELS.quiet, `quiet-${id}`);
  }
  seen.clear();
  for (const [id, state] of now) seen.set(id, state);
}

const VIEW = "locks.workshop";
const view = () => { try { return localStorage.getItem(VIEW) || "cards"; } catch { return "cards"; } };

// The office: every worker at its desk in one room, the queue waiting at the door.
function office(running, queued, pick) {
  const width = W * Math.max(1, running.length) + 16;
  const c = h("canvas", { width: width * SCALE, height: H * SCALE, class: "office", role: "img",
    "aria-label": `${running.length} at work, ${queued} waiting`, dataset: { office: "1", queued: String(queued) } });
  c.workers = running.map((t) => ({ id: t.id, state: stateOf(t) }));
  c.addEventListener("click", (e) => {
    const slot = Math.floor((e.offsetX / c.clientWidth) * width / W);
    if (running[slot]) pick(running[slot].id);
  });
  live.add(c);
  const ids = h("div", { class: "office-ids" }, running.map((t) =>
    h("button", { onclick: () => pick(t.id) }, `#${t.id} ${LABELS[stateOf(t)] || ""}`)));
  return h("div", {}, c, ids);
}

// One scene per running task, with the same words beside it; a click opens its terminal.
export function workshop(running, open, queued = 0) {
  notices(running);
  if (!running.length) { stop(); return null; }
  const pane = h("div", { class: "terminal", "aria-live": "polite" });
  const keep = watching && running.some((t) => t.id === watching.id) ? watching.id : null;
  const scenes = h("div", { class: "workshop" }, running.map((t) => {
    const state = stateOf(t);
    const canvas = h("canvas", { width: W * SCALE, height: H * SCALE, role: "img",
      "aria-label": `#${t.id}: ${LABELS[state] || state}`, dataset: { state, id: String(t.id) } });
    live.add(canvas);
    if (!timer) timer = setInterval(tick, 250);
    requestAnimationFrame(tick);
    return h("div", { class: `worker-scene s-${state}`, title: t.title || "" },
      h("button", { class: "scene-pick", onclick: () => watch(pane, t.id), "aria-label": `show #${t.id}'s terminal` }, canvas),
      h("span", { class: "worker-id" }, `#${t.id}`),
      h("span", { class: "worker-state" }, LABELS[state] || state),
      t.progress?.doing ? h("span", { class: "bubble" }, t.progress.doing) : null,
      timeBar(t.progress || {}),
      t.files_line ? h("span", { class: "worker-files" }, t.files_line) : null,
      h("span", {}, h("button", { onclick: () => watch(pane, t.id) }, "terminal"), " ",
        h("button", { onclick: () => open(t.id) }, "task")));
  }));
  if (keep) watch(pane, keep); else stop();
  const count = {};
  for (const t of running) count[stateOf(t)] = (count[stateOf(t)] || 0) + 1;
  const sum = Object.entries(count).map(([k, n]) => `${n} ${LABELS[k] || k}`).join(" · ");
  const box = h("div", {});
  const draw = () => {
    const mode = view();
    box.replaceChildren(
      h("div", { class: "workshop-sum" }, `${running.length} at work: ${sum}${queued ? ` · ${queued} waiting` : ""}`,
        h("button", { class: "view-toggle", "aria-pressed": String(mode === "office"), onclick: () => {
          try { localStorage.setItem(VIEW, mode === "office" ? "cards" : "office"); } catch { /* this page only */ }
          draw();
        } }, mode === "office" ? "cards" : "office"),
        notifyButton()),
      mode === "office" ? office(running, queued, (id) => watch(pane, id)) : scenes, pane);
    if (!timer) timer = setInterval(tick, 250);
    requestAnimationFrame(tick);
  };
  draw();
  return box;
}
