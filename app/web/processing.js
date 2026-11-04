// The Processing page (#processing): every row Sync has not finished, why, and Retry: a failed
// or to-retry row's Retry processes it at once, Retry all leaves the failed ones for the next
// Sync. It shares the header, so the Sync button and progress bar stay in reach.

import { errorText, toast } from "./toast.js";

const el = (id) => document.getElementById(id);
const STAGES = ["failed", "later", "new"];
const PLATFORMS = { instagram: "Instagram", youtube: "YouTube" };

let failedIds = [];

export function initProcessing() {
  window.addEventListener("hashchange", route);
  el("retry-all").addEventListener("click", (event) => retry(failedIds, event.currentTarget));
  route();
}

// Reloads the rows if the page is showing, e.g. as a Sync works through them.
export function refreshProcessing() {
  if (!el("processing").hidden) loadProcessing();
}

function route() {
  const open = location.hash === "#processing";
  el("gallery").hidden = open;
  el("processing").hidden = !open;
  const panel = el("menu-panel");
  if (panel.matches(":popover-open")) panel.hidePopover(); // the link was in it
  window.scrollTo(0, 0);
  if (open) loadProcessing();
}

async function loadProcessing() {
  let result;
  try {
    const response = await fetch("/api/processing");
    if (!response.ok) {
      toast(await errorText(response), "error");
      return;
    }
    result = await response.json();
  } catch {
    return; // the server is down; the page keeps what it last showed
  }

  const byStage = Object.fromEntries(STAGES.map((stage) => [stage, []]));
  for (const row of result.rows) byStage[row.stage].push(row);
  for (const stage of STAGES) {
    el(`stage-${stage}`).hidden = !byStage[stage].length;
    el(`rows-${stage}`).replaceChildren(...byStage[stage].map(rowItem));
  }
  failedIds = byStage.failed.map((row) => row.id);
  el("processing-counts").replaceChildren(
    stat(result.done, "done", "done"),
    stat(byStage.failed.length, "failed", "failed"),
    stat(byStage.later.length, "to retry", "later"),
    stat(byStage.new.length, "new", "new"),
  );
  el("processing-empty").hidden = result.rows.length > 0;
  document.dispatchEvent(new CustomEvent("vcollect:waiting", { detail: result.new }));
}

// A count, coloured by its stage while it has any rows.
function stat(count, label, stage) {
  const box = document.createElement("div");
  box.className = count ? `stat ${stage}` : "stat";
  const number = document.createElement("strong");
  number.textContent = count;
  const text = document.createElement("span");
  text.textContent = label;
  box.append(number, text);
  return box;
}

function rowItem(row) {
  const item = document.createElement("li");
  item.className = "stage-row";

  const text = document.createElement("div");
  text.className = "stage-row-text";
  const name = document.createElement(row.url ? "a" : "span");
  name.className = "stage-row-name";
  name.textContent = row.caption || row.title || row.url || `Row ${row.id}`;
  if (row.url) Object.assign(name, { href: row.url, target: "_blank", rel: "noopener" });
  const meta = document.createElement("p");
  meta.className = "stage-row-meta";
  meta.textContent = [
    PLATFORMS[row.platform],
    `saved ${day(row.saved_at)}`,
    row.tried_at && `tried ${moment(row.tried_at)}`,
  ]
    .filter(Boolean)
    .join(" · ");
  text.append(name, meta);
  if (row.reason) {
    const reason = document.createElement("p");
    reason.className = `stage-row-reason ${row.stage}`;
    reason.textContent = row.reason;
    text.append(reason);
  }

  const actions = document.createElement("div");
  actions.className = "stage-row-actions";
  if (row.notion_url) {
    const notion = document.createElement("a");
    Object.assign(notion, { href: row.notion_url, target: "_blank", rel: "noopener" });
    notion.textContent = "Notion ↗";
    actions.append(notion);
  }
  if (row.stage === "failed" || row.stage === "later") {
    const button = document.createElement("button");
    button.className = "button secondary small";
    button.type = "button";
    button.textContent = "Retry";
    button.title = "Process it again now";
    button.onclick = () => retry([row.id], button, true);
    actions.append(button);
  }
  item.append(text, actions);
  return item;
}

// With `now` the server processes the rows at once (failed ones set back to Not started first),
// and the header shows its progress. Otherwise it sets failed rows back to Not started (in
// Notion too), and the next Sync processes them as new.
async function retry(ids, button, now = false) {
  if (!ids.length) return;
  button.disabled = true;
  try {
    const response = await fetch("/api/processing/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, now }),
    });
    if (!response.ok) {
      toast(await errorText(response), "error");
      return;
    }
    const { reset } = await response.json();
    if (!reset.length) {
      toast(
        now
          ? "Nothing to retry: it is being processed now, or already done."
          : "Nothing to retry: no longer Failed in Notion.",
      );
    } else if (now) {
      document.dispatchEvent(new CustomEvent("vcollect:retrying"));
    } else {
      toast(
        reset.length === 1
          ? "Reset. The next Sync processes it."
          : `${reset.length} rows reset. The next Sync processes them.`,
      );
    }
  } finally {
    button.disabled = false;
    loadProcessing(); // also brings the Sync button's count up to date
  }
}

function day(iso) {
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function moment(iso) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
