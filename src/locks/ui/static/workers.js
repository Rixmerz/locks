// The workshop: one small pixel-art scene per running task, so a glance tells whether its
// worker is reading, writing, waiting on locks' check, being inspected, or has gone quiet.
// Every state is read from what the board already says of the task (its phase, its stage,
// `quiet_s` from `progress.of`): the drawing invents nothing, and its label says the same in
// words. The sprites are drawn here, pixel by pixel; no asset is fetched.
import { h } from "./util.js";

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
    (scenes[c.dataset.state] || scenes.starting)(ctx, pal, c.frame);
  }
  if (!live.size) { clearInterval(timer); timer = null; }
}

// One scene per running task, with the same words beside it.
export function workshop(running, open) {
  if (!running.length) return null;
  return h("div", { class: "workshop" }, running.map((t) => {
    const state = stateOf(t);
    const canvas = h("canvas", { width: W * SCALE, height: H * SCALE, role: "img",
      "aria-label": `#${t.id}: ${LABELS[state] || state}`, dataset: { state } });
    live.add(canvas);
    if (!timer) timer = setInterval(tick, 250);
    requestAnimationFrame(tick);
    return h("button", { class: `worker-scene s-${state}`, onclick: () => open(t.id), title: t.title || "" },
      canvas,
      h("span", { class: "worker-id" }, `#${t.id}`),
      h("span", { class: "worker-state" }, LABELS[state] || state),
      t.files_line ? h("span", { class: "worker-files" }, t.files_line) : null);
  }));
}
