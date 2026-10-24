// The menu panel: group by date (gallery.js keeps that one), auto-pull, and disk use.

import { errorText, toast } from "./toast.js";

const el = (id) => document.getElementById(id);
const MB = 1024 ** 2;
const GB = 1024 ** 3;

let autoPull = false;

export function autoPullOn() {
  return autoPull;
}

export function initMenu() {
  const panel = el("menu-panel");
  // A popover opens in the middle of the screen; this one hangs below its button.
  panel.addEventListener("beforetoggle", (event) => {
    if (event.newState !== "open") return;
    const button = el("menu-button").getBoundingClientRect();
    panel.style.top = `${button.bottom + 8}px`;
    panel.style.right = `${Math.max(8, window.innerWidth - button.right)}px`;
    loadSettings(); // the data folder may have grown since
  });
  el("auto-pull").addEventListener("change", saveAutoPull);
  loadSettings();
}

async function loadSettings() {
  try {
    const response = await fetch("/api/settings");
    if (response.ok) show(await response.json());
  } catch {
    // The server is down; the menu keeps what it last knew.
  }
}

async function saveAutoPull() {
  const box = el("auto-pull");
  const response = await fetch("/api/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto_pull: box.checked }),
  });
  if (!response.ok) {
    box.checked = !box.checked;
    toast(await errorText(response), "error");
    return;
  }
  show(await response.json());
}

function show(settings) {
  autoPull = settings.auto_pull;
  el("auto-pull").checked = autoPull;
  el("auto-pull-label").textContent = `Pull from Notion every ${settings.auto_pull_minutes} min`;

  const { used_bytes: used, cap_bytes: cap, sync_stops_at_bytes: stop } = settings.disk;
  el("disk-text").textContent = `${size(used)} of ${size(cap)}`;
  const meter = el("disk-meter");
  Object.assign(meter, { max: cap, low: stop * 0.8, high: stop, optimum: 0, value: used });
  const full = used >= stop;
  el("disk-note").textContent = full
    ? `Sync has stopped downloading: the data folder is past ${size(stop)}.`
    : `Sync stops downloading at ${size(stop)}.`;
  el("disk-note").classList.toggle("warn", full);
}

function size(bytes) {
  if (bytes < GB) return `${Math.round(bytes / MB)} MB`;
  const gb = bytes / GB;
  return `${gb < 10 ? gb.toFixed(1) : Math.round(gb)} GB`;
}
