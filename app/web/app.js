// Gallery page. Talks to the server only through the JSON API (/api/...).

import { askAboutDeleted } from "./deleted.js";
import { initGallery, loadPosts, postsDeletedInNotion } from "./gallery.js";
import { anchorBelow, autoPullOn, initMenu } from "./menu.js";
import { initProcessing, refreshProcessing } from "./processing.js";
import { errorText, toast } from "./toast.js";

const el = (id) => document.getElementById(id);
const POLL_MS = 1000;
const REFRESH_MS = 30_000;

let following = false; // this tab is showing a running Sync

// Every page load pulls from Notion; the server reuses a pull from the last minute.
// True when the pull went through.
async function pullFromNotion() {
  try {
    const response = await fetch("/api/pull", { method: "POST" });
    if (!response.ok) return false; // /api/health explains why
    const result = await response.json();
    setSyncLabel(result.new);
    await loadPosts(); // the pull may have changed captions and tags
    return true;
  } catch {
    return false; // the server is down; showServices says so
  }
}

function setSyncLabel(waiting) {
  const button = el("sync");
  button.replaceChildren("Sync");
  button.removeAttribute("aria-label");
  if (!waiting) return;
  const count = document.createElement("span");
  count.className = "count";
  count.textContent = `${waiting} new`;
  button.append(count);
  button.setAttribute("aria-label", `Sync (${waiting} new)`);
}

// Sync: pull, ask about posts deleted in Notion, then process the new rows on the server.
async function startSync() {
  el("sync").disabled = true;
  try {
    await pullFromNotion(); // so the question covers rows deleted since the page loaded
    const gone = postsDeletedInNotion();
    if (gone.length) {
      if (!(await askAboutDeleted(gone))) return;
      await loadPosts();
    }
    const response = await fetch("/api/sync", { method: "POST" });
    if (response.ok) followSync(await response.json());
    else toast(await errorText(response), "error");
  } finally {
    if (!following) el("sync").disabled = false;
  }
}

async function cancelSync() {
  el("cancel").disabled = true;
  await fetch("/api/sync/cancel", { method: "POST" });
}

// Every open tab polls, so each shows the same progress bar. Finished posts join the grid
// as they land, and leave the Processing page.
async function followSync(progress) {
  following = true;
  let { done, handled } = progress;
  while (progress.running) {
    showProgress(progress);
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    progress = await (await fetch("/api/sync")).json();
    if (progress.done !== done) {
      done = progress.done;
      loadPosts();
    }
    if (progress.handled !== handled) {
      handled = progress.handled;
      refreshProcessing();
    }
  }
  following = false;
  showProgress(progress);
  loadPosts();
  refreshProcessing();
}

// Keeps a tab in step with what happens elsewhere: a Sync started from another tab, the
// count of new rows, and with auto-pull on, whatever the server's pulls bring in.
async function refresh() {
  if (document.hidden || following) return;
  try {
    const progress = await (await fetch("/api/sync")).json();
    if (progress.running) followSync(progress);
    else setSyncLabel(progress.new);
  } catch {
    return;
  }
  if (autoPullOn()) loadPosts();
  refreshProcessing(); // another tab may have retried rows
}

function showProgress(progress) {
  const running = progress.running;
  el("sync").hidden = running;
  el("progress").hidden = !running;
  if (running) {
    el("progress-bar").max = progress.total || 1;
    el("progress-bar").value = progress.handled;
    const where = progress.total ? `${progress.handled + 1} / ${progress.total}` : "Pulling from Notion";
    el("progress-label").textContent = [where, progress.step, progress.current]
      .filter(Boolean)
      .join(" · ");
    return;
  }
  el("cancel").disabled = false;
  el("sync").disabled = false;
  setSyncLabel(progress.new);
  if (progress.finished_at) showReport(progress);
}

function showReport(progress) {
  const parts = [`${progress.done} done`, `${progress.failed} failed`, `${progress.later} to retry`];
  const ended = new Date(progress.finished_at).toLocaleTimeString();
  el("report-summary").textContent =
    `Last ${progress.retry ? "retry" : "Sync"} (${ended}): ${parts.join(" · ")}` +
    (progress.stopped ? ` · ${progress.stopped}` : "");
  el("report-problems").replaceChildren(
    ...progress.problems.map((problem) => {
      const row = document.createElement("li");
      row.textContent = problem;
      return row;
    }),
  );
  el("report").hidden = false;
}

async function showServices() {
  const list = el("services");
  let health;
  try {
    const response = await fetch("/api/health");
    health = await response.json();
  } catch {
    list.replaceChildren(serviceRow("Server", "unreachable", false));
    return;
  }

  const { notion, ollama, whisper } = health;
  let notionState = "connected";
  if (!notion.configured) notionState = "set NOTION_TOKEN and NOTION_DATA_SOURCE_ID";
  else if (notion.error) notionState = notion.error;

  let ollamaState = `${ollama.model} ready`;
  if (!ollama.reachable) ollamaState = "unreachable";
  else if (!ollama.model_ready) ollamaState = `${ollama.model} not pulled yet`;

  list.replaceChildren(
    serviceRow("Notion", notionState, notion.configured && !notion.error),
    serviceRow("Ollama", ollamaState, ollama.reachable && ollama.model_ready),
    serviceRow("Whisper", whisper.reachable ? "ready" : "unreachable", whisper.reachable),
  );
}

function serviceRow(name, state, ok) {
  const row = document.createElement("li");
  row.className = ok ? "ok" : "warn";
  row.textContent = `${name}: ${state}`;
  return row;
}

// Adds a Notion row for the link; the next Sync fetches it.
async function addLink(event) {
  event.preventDefault();
  const input = el("add-url");
  const url = input.value.trim();
  if (!url) return;
  const send = (force) =>
    fetch("/api/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, force }),
    });
  let response = await send(false);
  if (response.status === 409) {
    const { detail } = await response.clone().json();
    const duplicate = detail?.duplicate;
    if (duplicate) {
      const name = duplicate.caption || duplicate.url;
      if (!confirm(`This link is already saved as "${name}". Add it again anyway?`)) return;
      response = await send(true);
    }
  }
  if (!response.ok) {
    toast(await errorText(response), "error");
    return;
  }
  input.value = "";
  el("add-form").hidePopover();
  setSyncLabel((await response.json()).new);
  toast("Added to Notion. Press Sync to fetch it.");
}

// The header stays on screen; date headings stick just below it, and a hairline appears
// under it once the page scrolls beneath.
function trackHeader() {
  const bar = el("topbar");
  new ResizeObserver(() => {
    document.documentElement.style.setProperty("--header-h", `${bar.offsetHeight}px`);
  }).observe(bar);
  const edge = () => bar.classList.toggle("scrolled", window.scrollY > 0);
  window.addEventListener("scroll", edge, { passive: true });
  edge();
}

anchorBelow(el("add-form"), el("add-button"));
el("add-form").addEventListener("toggle", (event) => {
  if (event.newState === "open") el("add-url").focus();
});
el("add-form").addEventListener("submit", addLink);
document.addEventListener("vcollect:waiting", (event) => setSyncLabel(event.detail));
// A row's Retry on the Processing page: the server is processing it now, in a running Sync
// (which this tab already follows) or in a run of its own.
document.addEventListener("vcollect:retrying", async () => {
  if (!following) followSync(await (await fetch("/api/sync")).json());
});
el("sync").addEventListener("click", startSync);
el("cancel").addEventListener("click", cancelSync);
document.addEventListener("visibilitychange", refresh);
setInterval(refresh, REFRESH_MS);

trackHeader();
initGallery();
initMenu();
initProcessing();
loadPosts();

(async () => {
  const progress = await (await fetch("/api/sync")).json();
  if (progress.running) followSync(progress);
  else if (progress.finished_at) showReport(progress);
  if (await pullFromNotion()) el("sync").disabled = following;
  showServices();
})();
