// Gallery page. Talks to the server only through the JSON API (/api/...).

import { initGallery, loadPosts } from "./gallery.js";
import { errorText, toast } from "./toast.js";

const el = (id) => document.getElementById(id);
const POLL_MS = 1000;

// Every page load pulls from Notion; the server reuses a pull from the last minute.
async function pullFromNotion() {
  try {
    const response = await fetch("/api/pull", { method: "POST" });
    if (!response.ok) return; // /api/health explains why
    const result = await response.json();
    setSyncLabel(result.new);
    el("sync").disabled = false;
    await loadPosts(); // the pull may have changed captions and tags
  } catch {
    // The server is down; showServices says so.
  }
}

function setSyncLabel(waiting) {
  el("sync").textContent = waiting ? `Sync (${waiting} new)` : "Sync";
}

async function startSync() {
  el("sync").disabled = true;
  const response = await fetch("/api/sync", { method: "POST" });
  if (response.ok) followSync(await response.json());
  else el("sync").disabled = false;
}

async function cancelSync() {
  el("cancel").disabled = true;
  await fetch("/api/sync/cancel", { method: "POST" });
}

// Every open tab polls, so each shows the same progress bar. Finished posts join the grid
// as they land.
async function followSync(progress) {
  let done = progress.done;
  while (progress.running) {
    showProgress(progress);
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    progress = await (await fetch("/api/sync")).json();
    if (progress.done !== done) {
      done = progress.done;
      loadPosts();
    }
  }
  showProgress(progress);
  loadPosts();
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
    `Last Sync (${ended}): ${parts.join(" · ")}` + (progress.stopped ? ` · ${progress.stopped}` : "");
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
  setSyncLabel((await response.json()).new);
  toast("Added to Notion. Press Sync to fetch it.");
}

el("add-form").addEventListener("submit", addLink);
document.addEventListener("vcollect:waiting", (event) => setSyncLabel(event.detail));
el("sync").addEventListener("click", startSync);
el("cancel").addEventListener("click", cancelSync);

initGallery();
loadPosts();

(async () => {
  const progress = await (await fetch("/api/sync")).json();
  if (progress.running) followSync(progress);
  else if (progress.finished_at) showReport(progress);
  await pullFromNotion();
  showServices();
})();
