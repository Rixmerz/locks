import { get } from "./net.js";
import { cellChip, h, kindChip, repoName, secs, usd } from "./util.js";

// What locks' runs cost in time and money, and what they got for it: the evidence
// for changing the router, the briefs and the defaults. Read only; no model.

const PHASES = [
  ["advance_s", "advance", "from the step it waited on passing to its being queued"],
  ["pickup_s", "pickup", "from queued to the dispatcher starting it"],
  ["spawn_s", "spawn", "from the dispatcher starting it to its plan"],
  ["queued_s", "queued", "waiting in its lane before it ran (runs that did not split it into pickup and spawn)"],
  ["plan_s", "plan", "classifying and choosing the cell"],
  ["setup_s", "setup", "worktree, waiting for the checkout, servers"],
  ["worker_s", "worker", "the workers' own time"],
  ["check_s", "check", "locks running --verify"],
  ["verifier_s", "verifier", "the independent check"],
  ["wait_s", "wait", "waiting for a network that dropped, or for a machine under load"],
  ["notes_s", "notes", "filing what a passing worker learned"],
  ["lost_s", "lost", "attempts whose worker died: from its start to its time limit at most"],
  ["unnoticed_s", "unnoticed", "after a dead worker's time limit, until locks noticed it had died"],
  ["other_s", "other", "the rest of the run: recording, committing"],
];
const PERIOD_KEY = "locks.analysis.days";
const SCOPE_KEY = "locks.analysis.scope";

function kept(key, fallback, value) {
  try {
    if (value !== undefined) localStorage.setItem(key, value);
    return localStorage.getItem(key) ?? fallback;
  } catch { return value ?? fallback; }
}

const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const rate = (v) => h("td", { class: `num ${v === null || v === undefined ? "" : v >= 0.8 ? "rate-ok" : v < 0.5 ? "rate-bad" : "rate-warn"}` }, pct(v));
const money = (v) => (v === null || v === undefined ? "—" : usd(v));
const num = (v) => h("td", { class: "num" }, v ?? "—");
const head = (...cols) => h("tr", {}, cols.map((c) => (Array.isArray(c) ? h("th", { class: "num" }, c[0]) : h("th", {}, c))));

// The independent check's record per repository, kind and worker cell (`locks.trust`).
function trustSection(rows) {
  if (!rows.length) return [];
  return [h("h2", { class: "section" }, "The independent check's record"),
    h("p", { class: "empty" }, "Each task's first independent check, per cell. Ten clean in a row and a medium change a "
      + "cheap cell made is checked one pass in two, then one in four; one blocker returns it to every pass."),
    h("table", {}, head("project", "kind", "cell", ["checks"], ["clean"], "checking", ["unchecked"], ["repeated blockers"],
      ["re-checks finding new"], ["first try"], ["per point"]),
      rows.map((x) => h("tr", {}, h("td", {}, repoName(x.repo)), h("td", {}, kindChip(x.kind)), h("td", {}, cellChip(x.cell)),
        num(x.checks), rate(x.clean_rate), h("td", {}, x.every === 1 ? `every pass (${x.run}/${x.qualify} clean)` : `1 in ${x.every}`),
        num(x.sampled), num(`${x.repeated}/${x.blockers}`), num(`${x.rechecks_new}/${x.rechecks}`), rate(x.pass_first),
        num(money(x.cost_per_point)))))];
}

// Each capability workers were handed, and whether they called it (observed, never a trial).
function capabilitiesSection(rows) {
  if (!rows.length) return [];
  return [h("h2", { class: "section" }, "Capabilities handed to workers"),
    h("p", { class: "empty" }, "Attempts that had each one, called it, and had a call go through; attempts passed when "
      + "called against when not. Observed, not a trial."),
    h("table", {}, head("capability", ["handed"], ["called"], ["went through"], ["never called"], ["passed called"], ["passed silent"]),
      rows.map((x) => h("tr", {}, h("td", {}, x.capability), num(x.handed), num(`${x.called} (${pct(x.called_rate)})`),
        num(x.through), num(x.never_called), rate(x.pass_when_called), rate(x.pass_when_silent))))];
}

// A trial in two arms: what each arm measured, and where it stands now.
// The notes briefs carried and the ones workers said they relied on: observed, never a verdict.
function notesUsedSection(n) {
  if (!n) return [];
  return [h("h2", { class: "section" }, "Notes workers relied on"),
    h("p", { class: "empty" }, "What the workers said, observed: never a verdict on a note."),
    h("table", {}, head("measured", "value"),
      h("tr", {}, h("td", {}, "tasks briefed with notes"), h("td", {}, String(n.tasks))),
      h("tr", {}, h("td", {}, "notes carried · relied on"),
        h("td", {}, `${n.carried} · ${n.relied_on} (${pct(n.relied_rate)})`)),
      h("tr", {}, h("td", {}, "first tries passed: a note named · none named"),
        h("td", {}, `${pct(n.pass_first_naming)} · ${pct(n.pass_first_silent)}`)))];
}

const median = (v) => (v === null || v === undefined ? "—" : String(v));

function behaviorSection(b) {
  const rows = (b && b.by_kind) || [];
  if (!rows.length) return [];
  return [h("h2", { class: "section" }, "How workers worked"),
    h("p", { class: "empty" }, "Read from each attempt's own tool calls, observed: medians of calls, calls before the "
      + "first write, checks it ran and fix loops (a check again after a write that followed one); shares of failed "
      + "calls, of attempts that ended on a write no check followed and of attempts that never checked."),
    h("table", {}, head("kind", "outcome", ["attempts"], ["calls"], ["before write"], ["checks"], ["loops"], ["rereads"],
      ["failed calls"], ["unchecked end"], ["never checked"]),
      rows.map((r) => h("tr", {}, h("td", {}, kindChip(r.kind)), h("td", {}, r.outcome), num(r.attempts),
        num(median(r.calls_p50)), num(median(r.calls_before_edit_p50)), num(median(r.check_runs_p50)),
        num(median(r.fix_cycles_p50)), num(median(r.rereads_p50)), num(pct(r.failed_share)),
        num(pct(r.unchecked_end_share)), num(pct(r.never_checked_share)))))];
}

function armsSection(title, about, arms, names, said, now) {
  if (!arms || !names.some((n) => (arms[n] || {}).tasks)) return [];
  return [h("h2", { class: "section" }, title), h("p", { class: "empty" }, about),
    h("table", {}, head("arm", "measured"), names.map((n) => h("tr", {}, h("td", {}, n), h("td", {}, said(arms[n]))))),
    now ? h("p", { class: "empty" }, `Now: ${now}`) : []];
}

const tokens = (v) => (v === null || v === undefined ? "—" : v >= 1000 ? `${Math.round(v / 1000)}k` : `${v}`);
const change = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${Math.round(v * 100)}%`);

function livespecUse(use, rows) {
  const used = use && use.given
    ? h("p", { class: "empty" }, `livespec was handed to ${use.given} attempt(s) and called in ${use.used} (${use.calls} call(s)); `
      + `passed ${pct(use.passed_used)} when called, ${pct(use.passed_idle)} when not.`)
    : h("div", { class: "empty" }, "No attempt was handed livespec in this period.");
  if (!rows || !rows.length) return [used];
  return [used, h("table", {}, head("kind", ["with: runs"], ["with: avg"], ["with: p50"], ["without: runs"], ["without: avg"], ["without: p50"], ["change"]),
    rows.map((k) => h("tr", {}, h("td", {}, kindChip(k.kind)), num(k.with.runs), num(tokens(k.with.avg)), num(tokens(k.with.p50)),
      num(k.without.runs), num(tokens(k.without.avg)), num(tokens(k.without.p50)), num(change(k.change)))))];
}

const PICKUP = [
  ["pickup_conflict_s", "work ahead it was judged to wait for"],
  ["pickup_limit_s", "the usage limit"],
  ["pickup_ceiling_s", "your ceiling of workers (chosen)"],
  ["pickup_level_s", "the machine's level"],
];

function pickupLine(told) {
  const seconds = (told && told.seconds) || {};
  const parts = PICKUP.filter(([key]) => seconds[key]).map(([key, words]) =>
    `${words} ${secs(seconds[key])} (${pct(told.share[key])})`);
  if (!parts.length) return null;
  if (told.unknown_s) parts.push(`not told ${secs(told.unknown_s)}`);
  return h("p", { class: "empty" }, `The pickup waited on: ${parts.join(" · ")}.`);
}

function phaseBar(phases) {
  const total = Object.values(phases.seconds).reduce((a, b) => a + b, 0);
  if (!phases.runs || !total) return h("div", { class: "empty" }, "No run has its phases timed yet: runs record them from this version on.");
  const bar = h("div", { class: "phasebar", role: "img", "aria-label": "where the time of the timed runs went" });
  for (const [key, label] of PHASES) {
    const value = phases.seconds[key] || 0;
    if (!value) continue;
    const seg = h("span", { class: `ph-${label}`, title: `${label}: ${secs(value)} (${pct(phases.share[key])})` });
    seg.style.flexGrow = String(value);
    bar.append(seg);
  }
  return h("div", {},
    bar,
    h("div", { class: "legend" }, PHASES.filter(([key]) => phases.seconds[key]).map(([key, label, what]) =>
      h("span", { title: what }, h("i", { class: `ph-${label}` }), `${label} ${secs(phases.seconds[key])} · ${pct(phases.share[key])}`))),
    h("p", { class: "empty" }, `Over the ${phases.runs} run(s) whose phases were timed. Advance, pickup, spawn and queued time come before a run starts; the rest is its wall time.`));
}

function summaryTable(rows, key, label, render) {
  if (!rows.length) return h("div", { class: "empty" }, "Nothing in this period.");
  return h("table", {},
    head(label, ["runs"], ["passed"], ["first try"], ["climbed"], ["p50 time"], ["p90 time"], ["per pass"], ["verified"]),
    rows.map((r) => h("tr", {},
      h("td", {}, render ? render(r[key]) : r[key]), num(r.runs), rate(r.pass_rate), rate(r.first_try_rate),
      num(pct(r.climbed_rate)), num(secs(r.wall_p50_s)), num(secs(r.wall_p90_s)), num(money(r.cost_per_pass_usd)),
      num(pct(r.verified_rate)))));
}

function download(report) {
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
  const link = h("a", { href: URL.createObjectURL(blob), download: `locks-analysis-${report.days || "all"}d.json` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function runList(rows, title) {
  if (!rows.length) return [];
  return [h("h2", { class: "section" }, title),
    h("table", {}, head("task", "kind", "status", ["time"], ["attempts"], ["cost"], "cells"),
      rows.map((r) => h("tr", {},
        h("td", {}, `#${r.task} `, h("span", { class: "muted" }, r.title)), h("td", {}, kindChip(r.kind)),
        h("td", {}, r.stop ? `${r.status} (${r.stop})` : r.status), num(secs(r.wall_s)), num(r.attempts),
        num(money(r.cost_usd)), h("td", {}, h("span", { class: "row" }, (r.cells || []).map((c) => cellChip(c)))))))];
}

export async function renderAnalysis(ctx) {
  const days = kept(PERIOD_KEY, "30");
  const scope = ctx.project ? kept(SCOPE_KEY, "project") : "all";
  const repo = scope === "project" && ctx.project ? `&repo=${encodeURIComponent(ctx.project)}` : "";
  const r = await get(`/api/analysis?days=${encodeURIComponent(days)}${repo}`);
  const s = r.summary;
  const period = h("select", { "aria-label": "period", onchange: (e) => { kept(PERIOD_KEY, "30", e.target.value); ctx.refresh(); } },
    [["7", "last 7 days"], ["30", "last 30 days"], ["90", "last 90 days"], ["0", "everything"]].map(([v, label]) =>
      h("option", { value: v, ...(v === days ? { selected: "" } : {}) }, label)));
  const where = h("select", { "aria-label": "scope", onchange: (e) => { kept(SCOPE_KEY, "project", e.target.value); ctx.refresh(); } },
    [...(ctx.project ? [["project", `this project (${repoName(ctx.project)})`]] : []), ["all", "every project"]].map(([v, label]) =>
      h("option", { value: v, ...(v === scope ? { selected: "" } : {}) }, label)));
  const turns = r.turns.turns;
  const delegations = r.turns.delegations;
  return h("div", {},
    h("div", { class: "toolbar" }, period, where,
      h("button", { type: "button", onclick: () => download(r), title: "everything on this screen, as JSON" }, "Export JSON")),
    h("p", { class: "empty" }, "Where locks' time and money go and what they buy, read from its own records. "
      + "`locks stats --runs --csv` writes one row per run for a spreadsheet."),
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("b", {}, s.runs ? pct(s.pass_rate) : "—"), h("span", {}, `${s.passed} of ${s.runs} runs passed (${s.tasks} tasks)`)),
      h("div", { class: "stat" }, h("b", {}, pct(s.first_try_rate)), h("span", {}, "passed at the first attempt")),
      h("div", { class: "stat" }, h("b", {}, secs(s.wall_p50_s)), h("span", {}, `median run · p90 ${secs(s.wall_p90_s)}`)),
      h("div", { class: "stat" }, h("b", {}, money(s.cost_per_pass_usd)), h("span", {}, `per pass · ${usd(s.cost_usd)} in all · checks ${usd(s.verifier_cost_usd)}`)),
      h("div", { class: "stat" }, h("b", {}, s.minutes_per_pass === null ? "—" : `${s.minutes_per_pass}m`), h("span", {}, `of runs per pass · ${s.hours}h in all`)),
      h("div", { class: "stat" }, h("b", {}, secs(s.queued_p50_s)), h("span", {}, "median wait in the queue"))),
    h("h2", { class: "section" }, "Where the time went"),
    phaseBar(r.phases),
    pickupLine(r.pickup),
    h("h2", { class: "section" }, "By kind"),
    summaryTable(r.by_kind, "kind", "kind", kindChip),
    h("h2", { class: "section" }, "The session's thread in the brief"),
    h("p", { class: "empty" }, "Runs a session sent, with the scribe's extract of that session in their brief and without it. "
      + "Observed, not a trial: the two groups differ in more than the brief."),
    r.context.length ? h("table", {}, head("brief", ["runs"], ["passed"], ["first try"], ["attempts"], ["p50 time"], ["per pass"]),
      r.context.map((c) => h("tr", {}, h("td", {}, c.group), num(c.runs), rate(c.pass_rate), rate(c.first_try_rate),
        num(c.attempts_avg), num(secs(c.wall_p50_s)), num(money(c.cost_per_pass_usd)))))
      : h("div", { class: "empty" }, "No run a session sent has been briefed yet."),
    h("h2", { class: "section" }, "livespec and tokens"),
    h("p", { class: "empty" }, "Tokens a run spent per kind (input and output, its attempts together) when its workers called livespec "
      + "against when they did not. Observed, not a trial: the two groups differ in more than the calls."),
    livespecUse(r.livespec_use, r.livespec_tokens),
    h("h2", { class: "section" }, "By cell"),
    r.cells.length ? h("table", {}, head("cell", ["attempts"], ["passed"], ["p50 time"], ["p50 turns"], ["p50 cost"], ["cost"], ["checks"], ["cache read"], "failures"),
      r.cells.map((c) => h("tr", {}, h("td", {}, cellChip(c.cell)), num(c.attempts), rate(c.attempts ? c.passed / c.attempts : null),
        num(secs(c.wall_p50_s)), num(c.turns_p50), num(money(c.cost_p50_usd)), num(usd(c.cost_usd)),
        num(usd(c.verifier_cost_usd)), num(pct(c.cache_read_share)),
        h("td", {}, Object.entries(c.failures).map(([f, n]) => `${f} ×${n}`).join(", ") || "—"))))
      : h("div", { class: "empty" }, "No attempt in this period."),
    h("div", { class: "split" },
      h("div", {}, h("h2", { class: "section" }, "How runs stopped"),
        Object.keys(r.stops).length ? h("table", {}, head("stop", ["runs"]),
          Object.entries(r.stops).map(([k, v]) => h("tr", {}, h("td", {}, k), num(v)))) : h("div", { class: "empty" }, "Every run passed.")),
      h("div", {}, h("h2", { class: "section" }, "Moves after a failed attempt"),
        Object.keys(r.moves).length ? h("table", {}, head("move", ["times"]),
          Object.entries(r.moves).map(([k, v]) => h("tr", {}, h("td", {}, k), num(v)))) : h("div", { class: "empty" }, "No run had to move."))),
    h("h2", { class: "section" }, "The session's own turns"),
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("b", {}, turns.count), h("span", {}, `turns · ${turns.interrupted} interrupted`)),
      h("div", { class: "stat" }, h("b", {}, secs(turns.seconds_p50)), h("span", {}, `median turn · p90 ${secs(turns.seconds_p90)}`)),
      h("div", { class: "stat" }, h("b", {}, usd(turns.usd)), h("span", {}, `the turns, at API rates · p50 ${money(turns.usd_p50)}`)),
      h("div", { class: "stat" }, h("b", {}, delegations.count), h("span", {}, `subagent delegations · p50 ${secs(delegations.seconds_p50)}`))),
    r.by_workflow.length ? [h("h2", { class: "section" }, "By workflow"), summaryTable(r.by_workflow, "workflow", "workflow")] : [],
    (r.races || []).length ? [h("h2", { class: "section" }, "Racing a first try"),
      h("p", { class: "empty" }, "Where a start rarely passes its first try, the next rung runs beside it; racing stops where it does not pay."),
      h("table", {}, head("project", "kind", ["races"], ["rung won"], ["start won"], ["both failed"], "racing"),
        r.races.map((x) => h("tr", {}, h("td", {}, repoName(x.repo)), h("td", {}, kindChip(x.kind)), num(x.races),
          num(x.rung_wins), num(x.start_wins), num(x.neither), h("td", {}, `${x.pays ? "on" : "off"}: ${x.why}`))))] : [],
    (r.role_revisions || []).length ? [h("h2", { class: "section" }, "Roles' lessons on trial"),
      h("p", { class: "empty" }, "A role's lessons in a project, proposed from its dead ends and the verifier's blockers, tried against the current ones."),
      h("table", {}, head("project", "role", "current", "on trial", "candidate's arm", "current arm"),
        r.role_revisions.map((x) => {
          const arm = (a) => (a ? `${a.passed_first}/${a.tasks} first tries · ${a.briefed} briefed` : "—");
          const c = x.candidate;
          return h("tr", {}, h("td", {}, repoName(x.repo)), h("td", {}, x.role),
            h("td", {}, x.current ? `#${x.current.id} (${x.current.lessons.length} lessons)` : "none yet"),
            h("td", {}, c ? `#${c.id} (${c.lessons.length} lessons, ${c.trial} first tries each)` : "—"),
            h("td", {}, c ? arm(c.arms.candidate) : "—"), h("td", {}, c ? arm(c.arms.current) : "—"));
        }))] : [],
    trustSection(r.trust || []),
    capabilitiesSection(r.capabilities || []),
    armsSection("Holding work to evidence", "A worker handed layout-inspector for a UI change, or rastro, has to show a "
      + "call of that tool that went through before its pass counts. Tried in turns with free work; kept where its passes "
      + "drew fewer blockers from the independent check.", r.evidence, ["gate", "free"],
      (a) => `${a.tasks} tasks · ${a.blocked}/${a.read} blocked (${pct(a.blocked_rate)})`, r.evidence_now),
    armsSection("The order of a brief's notes", "When the code a task touches and its words find different notes, the "
      + "brief orders them by rank fusion (a note both find comes first) or anchored first, in turns; fusion stays where "
      + "its first tries passed no less often.", r.recall, ["fused", "ordered"],
      (a) => `${a.tasks} tasks · ${a.passed}/${a.tries} first tries (${pct(a.rate)})`, r.recall_now),
    behaviorSection(r.behavior),
    notesUsedSection(r.notes_used),
    Object.entries(r.methods || {}).map(([name, arms]) => armsSection(`The ${name} method`,
      "A method its kind's workers are briefed to follow, tried in turns against its absence.", arms, ["with", "without"],
      (a) => `${a.tasks} tasks · ${a.passed}/${a.tries} first tries (${pct(a.rate)})`, (r.methods_now || {})[name])),
    h("h2", { class: "section" }, "By day"),
    r.daily.length ? h("table", {}, head("day", ["turns"], ["runs"], ["passed"], ["hours"], ["cost"]),
      r.daily.map((d) => h("tr", {}, h("td", {}, d.day), num(d.turns), num(d.runs), num(d.passed), num(d.hours), num(usd(d.cost_usd)))))
      : h("div", { class: "empty" }, "Nothing in this period."),
    runList(r.slowest || [], "The slowest runs"),
    runList(r.costliest || [], "The costliest runs"));
}
