// The pop-up player: one post, its items, and the filtered posts either side of it.
// Left/right (arrows, swipe or the side buttons) step through a post's items, then on
// into the neighbouring post. Edit and Delete act on the post shown.

import { renderEditor } from "./editor.js";
import { tagChip, tagKey } from "./tags.js";
import { errorText, toast } from "./toast.js";

const el = (id) => document.getElementById(id);
const SWIPE_PX = 50;

// What Notion made of a saved edit, as /api/posts/{id} reports it.
const SAVED_MESSAGES = {
  saved: "Saved",
  waiting: "Saved here; Notion gets it on the next Sync",
  local: "Saved (this post is not in Notion)",
  unchanged: "No changes",
};

// Posts outside Notion say so under their tags.
const STATE_NOTES = {
  deleted_in_notion: "Deleted in Notion. The next Sync asks whether to keep it here.",
  local_only: "Not in Notion: kept in this gallery only.",
};

const dialog = el("player");
let posts = [];
let postIndex = 0;
let itemIndex = 0;
let editing = false;
let handlers = {};

/** `with`: onTag(key), onChange(post), onDelete(id, waiting), knownTags() -> {notion, ai}. */
export function openPlayer(list, index, withHandlers) {
  posts = list;
  postIndex = index;
  itemIndex = 0;
  editing = false;
  handlers = withHandlers;
  showMedia();
  showInfo();
  if (!dialog.open) dialog.showModal();
}

function step(delta) {
  const post = posts[postIndex];
  const nextItem = itemIndex + delta;
  if (nextItem >= 0 && nextItem < post.items.length) {
    itemIndex = nextItem;
  } else {
    const nextPost = postIndex + delta;
    if (nextPost < 0 || nextPost >= posts.length) return;
    postIndex = nextPost;
    itemIndex = delta > 0 ? 0 : posts[nextPost].items.length - 1;
    editing = false; // moving on drops an unsaved edit
  }
  showMedia();
  showInfo();
}

function showMedia() {
  const post = posts[postIndex];
  const item = post.items[itemIndex];
  const stage = el("player-stage");
  if (item.kind === "video") {
    const video = document.createElement("video");
    video.src = item.src;
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    if (itemIndex === 0) video.poster = post.thumb;
    stage.replaceChildren(video);
  } else {
    const image = document.createElement("img");
    image.src = item.src;
    image.alt = post.caption || post.title || "";
    stage.replaceChildren(image);
  }

  const atStart = postIndex === 0 && itemIndex === 0;
  const atEnd = postIndex === posts.length - 1 && itemIndex === post.items.length - 1;
  el("player-prev").disabled = atStart;
  el("player-next").disabled = atEnd;
  el("player-count").textContent =
    post.items.length > 1 ? `${itemIndex + 1} / ${post.items.length}` : "";
  el("player-count").hidden = post.items.length < 2;
}

function showInfo() {
  const post = posts[postIndex];
  el("player-view").hidden = editing;
  el("player-editor").hidden = !editing;
  if (editing) {
    renderEditor(el("player-editor"), post, {
      knownTags: handlers.knownTags(),
      onSave: save,
      onCancel: () => {
        editing = false;
        showInfo();
      },
    });
    return;
  }

  el("player-title").textContent = post.title || post.caption || hostname(post.url);
  const saved = new Date(post.saved_at).toLocaleDateString(undefined, { dateStyle: "medium" });
  el("player-meta").textContent = [post.uploader, `Saved ${saved}`].filter(Boolean).join(" · ");
  el("player-summary").textContent = post.summary || "Summary on its way.";
  el("player-summary").classList.toggle("pending", !post.summary);
  // The caption is the user's own note; skip it when it is already the title.
  const caption = post.caption && post.caption !== el("player-title").textContent;
  el("player-caption").textContent = caption ? post.caption : "";
  el("player-caption").hidden = !caption;

  for (const [kind, field, row] of [
    ["notion", "tags", "player-tags"],
    ["ai", "ai_tags", "player-ai-tags"],
  ]) {
    el(row).replaceChildren(
      ...post[field].map((name) =>
        tagChip(kind, name, {
          onClick: () => {
            dialog.close();
            handlers.onTag(tagKey(kind, name));
          },
        }),
      ),
    );
    el(row).hidden = post[field].length === 0;
  }
  el("player-state").textContent = STATE_NOTES[post.state] ?? "";
  el("player-state").hidden = !STATE_NOTES[post.state];
  el("player-readd").hidden = post.state !== "local_only";
  el("player-original").href = post.url;
  el("player-notion").href = post.notion_url;
  el("player-notion").hidden = !post.notion_url;
}

async function save(changes) {
  const post = posts[postIndex];
  const response = await fetch(`/api/posts/${post.id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  if (!response.ok) {
    toast(await errorText(response), "error");
    return;
  }
  const result = await response.json();
  posts[postIndex] = result.post;
  handlers.onChange(result.post);
  editing = false;
  showInfo();
  if (result.notion === "rejected") toast(`Saved here, but Notion refused it: ${result.message}`, "error");
  else toast(SAVED_MESSAGES[result.notion] ?? "Saved", result.notion === "waiting" ? "warn" : "");
}

async function remove() {
  const post = posts[postIndex];
  const where = post.state === "synced" ? " and its Notion row goes to Notion's trash" : "";
  if (!confirm(`Delete this post? Its files are removed${where}.`)) return;
  const response = await fetch(`/api/posts/${post.id}`, { method: "DELETE" });
  if (!response.ok) {
    toast(await errorText(response), "error");
    return;
  }
  const result = await response.json();
  handlers.onDelete(post.id, result.new);
  posts.splice(postIndex, 1);
  toast("Deleted");
  if (posts.length === 0) {
    dialog.close();
    return;
  }
  postIndex = Math.min(postIndex, posts.length - 1);
  itemIndex = 0;
  showMedia();
  showInfo();
}

// A fresh Notion row for a local-only post, holding its link, caption, summary and tags.
async function readd() {
  const button = el("player-readd");
  const post = posts[postIndex];
  button.disabled = true;
  try {
    const response = await fetch(`/api/posts/${post.id}/readd`, { method: "POST" });
    if (!response.ok) {
      toast(await errorText(response), "error");
      return;
    }
    const result = await response.json();
    posts[postIndex] = result.post;
    handlers.onChange(result.post);
    document.dispatchEvent(new CustomEvent("vcollect:waiting", { detail: result.new }));
    showInfo();
    toast("Added back to Notion");
  } finally {
    button.disabled = false;
  }
}

function hostname(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "Saved post";
  }
}

el("player-prev").addEventListener("click", () => step(-1));
el("player-next").addEventListener("click", () => step(1));
el("player-close").addEventListener("click", () => dialog.close());
el("player-edit").addEventListener("click", () => {
  editing = true;
  showInfo();
});
el("player-delete").addEventListener("click", remove);
el("player-readd").addEventListener("click", readd);
// A click on the backdrop lands on the dialog element itself.
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});
dialog.addEventListener("close", () => el("player-stage").replaceChildren()); // stops playback
dialog.addEventListener(
  "keydown",
  (event) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    if (event.target.closest("input, textarea")) return; // moving the text cursor
    event.preventDefault(); // the video would otherwise seek
    step(event.key === "ArrowRight" ? 1 : -1);
  },
  true,
);

let touchX = null;
el("player-stage").addEventListener("touchstart", (event) => {
  touchX = event.touches[0].clientX;
});
el("player-stage").addEventListener("touchend", (event) => {
  if (touchX === null) return;
  const dx = event.changedTouches[0].clientX - touchX;
  touchX = null;
  if (Math.abs(dx) > SWIPE_PX) step(dx < 0 ? 1 : -1);
});
