// The pop-up player: one post, its items, and the filtered posts either side of it.
// Left/right (arrows, swipe or the side buttons) step through a post's items, then on
// into the neighbouring post.

import { tagChip, tagKey } from "./tags.js";

const el = (id) => document.getElementById(id);
const SWIPE_PX = 50;

const dialog = el("player");
let posts = [];
let postIndex = 0;
let itemIndex = 0;
let onTag = () => {};

export function openPlayer(list, index, handlers) {
  posts = list;
  postIndex = index;
  itemIndex = 0;
  onTag = handlers.onTag;
  show();
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
  }
  show();
}

function show() {
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
            onTag(tagKey(kind, name));
          },
        }),
      ),
    );
    el(row).hidden = post[field].length === 0;
  }
  el("player-original").href = post.url;
  el("player-notion").href = post.notion_url;
  el("player-notion").hidden = !post.notion_url;
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
// A click on the backdrop lands on the dialog element itself.
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});
dialog.addEventListener("close", () => el("player-stage").replaceChildren()); // stops playback
dialog.addEventListener(
  "keydown",
  (event) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
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
