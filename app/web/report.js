// The last Sync's report, behind the ⓘ button beside Sync. A pip on the button marks a report
// with problems this browser has not opened yet.

import { anchorBelow } from "./menu.js";

const el = (id) => document.getElementById(id);
const SEEN_KEY = "vcollect.reportSeen";

let shown = null; // the progress the panel shows
let seen = readSeen(); // finished_at of the last report opened here

export function initReport() {
  const panel = el("report-panel");
  anchorBelow(panel, el("report-button"));
  panel.addEventListener("toggle", (event) => {
    if (event.newState === "open") markSeen();
  });
  panel.addEventListener("click", (event) => {
    if (event.target.closest("a")) panel.hidePopover(); // off to the Processing page
  });
  // Opened in another tab: read there, read here.
  window.addEventListener("storage", (event) => {
    if (event.key !== SEEN_KEY) return;
    seen = event.newValue;
    showPip();
  });
}

export function showReport(progress) {
  shown = progress;
  const parts = [
    ["done", progress.done],
    ["failed", progress.failed],
    ["later", progress.later],
  ];
  for (const [name, count] of parts) {
    el(`report-${name}`).textContent = count;
    el(`report-${name}`).parentElement.classList.toggle("none", count === 0);
  }
  el("report-title").textContent = progress.retry ? "Last retry" : "Last Sync";
  const ended = new Date(progress.finished_at);
  el("report-time").dateTime = progress.finished_at;
  el("report-time").textContent = when(ended);
  el("report-stopped").textContent = progress.stopped;
  el("report-stopped").hidden = !progress.stopped;
  el("report-problems").replaceChildren(
    ...progress.problems.map((problem) => {
      const row = document.createElement("li");
      row.textContent = problem;
      return row;
    }),
  );
  el("report-button").hidden = false;
  if (el("report-panel").matches(":popover-open")) markSeen(); // read as it lands
  else showPip();
}

// Problems worth a look: a Cancel the user pressed is not one.
function hasProblems(progress) {
  const stopped = progress.stopped && progress.stopped !== "Cancelled";
  return Boolean(progress.failed || progress.later || progress.problems.length || stopped);
}

function showPip() {
  const unread = shown !== null && hasProblems(shown) && seen !== shown.finished_at;
  const button = el("report-button");
  button.classList.toggle("unread", unread);
  const label = shown?.retry ? "Last retry" : "Last Sync";
  button.setAttribute("aria-label", unread ? `${label}: new problems` : label);
  button.title = button.getAttribute("aria-label");
}

function markSeen() {
  if (!shown) return;
  seen = shown.finished_at;
  try {
    localStorage.setItem(SEEN_KEY, seen);
  } catch {
    // Storage is off (private window): this tab still remembers it.
  }
  showPip();
}

function readSeen() {
  try {
    return localStorage.getItem(SEEN_KEY);
  } catch {
    return null;
  }
}

// "11:05" today, "3 Nov, 11:05" before.
function when(date) {
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === new Date().toDateString()) return time;
  const day = date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  return `${day}, ${time}`;
}
