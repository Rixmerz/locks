// A desktop notification when the work a tab was opened for ends or stops (`#task=N`,
// `#run=N`). Asked for only from a person's click, never on load; the choice is kept per
// viewer in this browser, and nothing is said of the state the tab opened with.
import { h } from "./util.js";

const KEY = "locks.notify";
const GOING = new Set(["running", "queued"]);
const last = new Map();

const supported = () => typeof Notification !== "undefined";
function wanted() {
  try { return localStorage.getItem(KEY) === "on"; } catch { return false; }
}
function keep(on) {
  try { localStorage.setItem(KEY, on ? "on" : "off"); } catch { /* storage may be off: the choice lasts this page */ }
}
let fallback = null;  // the choice when storage is off
const on = () => supported() && Notification.permission === "granted" && (fallback ?? wanted());

/** A notice of its own (the workshop: a worker that went quiet, or ended), when the person turned them on. */
export function notify(title, body, tag) {
  if (!on()) return;
  try { new Notification(title, { body, tag: `locks-${tag}` }); } catch { /* see watchEnding */ }
}

/** Say a state of the work `key` names; a change from going on to anything else notifies. */
export function watchEnding(key, status, title, how) {
  const before = last.get(key);
  last.set(key, status);
  if (before === undefined || before === status || !GOING.has(before) || GOING.has(status) || !on()) return;
  try {
    new Notification(title, { body: how, tag: `locks-${key}` });
  } catch { /* a browser that takes notifications only from a service worker: the title still says it */ }
}

/** The header's button: asks for permission on its click, and turns the notice off again. */
export function notifyButton() {
  if (!supported()) return null;
  const button = h("button", { class: "notify", type: "button" });
  const label = () => {
    const blocked = Notification.permission === "denied";
    button.textContent = blocked ? "Notifications blocked in this browser" : on() ? "Notifying when it ends" : "Notify me when it ends";
    button.disabled = blocked;
    button.setAttribute("aria-pressed", String(on()));
    button.title = on() ? "click to stop the notice" : "a desktop notice when this work ends or stops";
  };
  button.addEventListener("click", async () => {
    if (on()) {
      fallback = false;
      keep(false);
    } else {
      const answer = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      fallback = answer === "granted";
      keep(fallback);
    }
    label();
  });
  label();
  return button;
}
