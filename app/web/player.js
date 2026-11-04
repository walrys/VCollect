// The pop-up player: one post, its items, and the filtered posts either side of it.
// Left/right (arrows, swipe or the side buttons) step through a post's items, then on
// into the neighbouring post. Edit and Delete act on the post shown. A page shows its
// readable copy, scrolling; a placeholder shows why it waits, with Retry.
// It grows out of the tile that opened it and shrinks back into the tile of the post it
// closes on. On a touch screen the media follows the finger: sideways to page, down to close,
// and anything in motion can be caught and thrown again.

import { renderEditor } from "./editor.js";
import { faceFor, hostname, openLabel, STATUS } from "./faces.js";
import { project, reducedMotion, rubberband, Spring, VelocityTracker } from "./spring.js";
import { tagChip, tagKey } from "./tags.js";
import { errorText, toast } from "./toast.js";

const el = (id) => document.getElementById(id);
const GAP_PX = 24; // between neighbouring items while paging; matches .slot in app.css
const SLOP_PX = 10; // a touch moves this far before it picks a direction
const CONTROLS_PX = 64; // the video's own controls along its bottom edge

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
const sheet = el("player-sheet");
const stage = el("player-stage");
const track = el("player-track");
let posts = [];
let postIndex = 0;
let itemIndex = 0;
let editing = false;
let handlers = {};

// The motion. `presence` runs from 0 (shrunk into its tile) to 1 (open); `pullX`/`pullY` are
// the finger dragging the sheet away; `pager` slides the items sideways.
const presence = new Spring(0, render, 0.001);
const pullX = new Spring(0, render);
const pullY = new Spring(0, render);
const pager = new Spring(0, (x) => (track.style.transform = `translateX(${x}px)`));
let morph = null; // how the open sheet maps onto the tile, from measure()
let closing = false;
let sliding = 0; // the direction of a slide still under way
let pushed = false; // a history entry stands for the open player, so Back closes it
const pageCache = new Map(); // readable copies fetched while the player is open: url -> html

// A post shows at least one thing: a page or a placeholder has no items of its own.
const count = (post) => Math.max(1, post.items.length);

// Leaving the player's history entry would otherwise put the page back where it was when the
// player opened, undoing the scroll that brings the closing post's tile into view.
history.scrollRestoration = "manual";

/** `with`: onTag(key), onChange(post), onDelete(id, waiting), knownTags() -> {notion, ai},
 *  tileFor(id) -> the post's tile scrolled into view, or null. */
export function openPlayer(list, index, withHandlers) {
  posts = list;
  postIndex = index;
  itemIndex = 0;
  editing = false;
  handlers = withHandlers;
  showMedia();
  showInfo();
  if (!dialog.open) dialog.showModal();
  history.pushState({ player: true }, "");
  pushed = true;
  closing = false;
  morph = null;
  pullX.set(0);
  pullY.set(0);
  pager.set(0);
  morph = measure(handlers.tileFor(posts[postIndex].id));
  presence.set(0);
  presence.to(1, { response: reducedMotion.matches ? 0.25 : 0.45 });
}

function closePlayer({ velocity = { x: 0, y: 0 }, fromHistory = false } = {}) {
  if (!dialog.open || closing) return;
  closing = true;
  if (pushed && !fromHistory) {
    pushed = false;
    history.back();
  }
  settlePaging();
  pager.set(0);
  stage.querySelector("video")?.pause();
  morph = measure(posts.length ? handlers.tileFor(posts[postIndex].id) : null);
  render();
  const response = reducedMotion.matches ? 0.25 : 0.4;
  Promise.all([
    presence.to(0, { response }),
    pullX.to(0, { response, velocity: velocity.x }),
    pullY.to(0, { response, velocity: velocity.y }),
  ]).then((settled) => {
    if (closing && settled.every(Boolean)) dialog.close();
  });
}

// Where the open sheet's media sits, and the transform that lays it over `tile`. Without a
// tile on screen (filtered out, deleted) the sheet just fades.
function measure(tile) {
  sheet.style.transform = "";
  sheet.style.clipPath = "";
  const media = track.querySelector(".slot.current > *");
  if (!media) return null;
  const box = media.getBoundingClientRect();
  const frame = sheet.getBoundingClientRect();
  const cx = box.left + box.width / 2;
  const cy = box.top + box.height / 2;
  sheet.style.transformOrigin = `${cx - frame.left}px ${cy - frame.top}px`;
  if (!tile || reducedMotion.matches) {
    return { fade: true, dx: 0, dy: 0, scale: reducedMotion.matches ? 1 : 0.94 };
  }

  const target = tile.getBoundingClientRect();
  // The tile shows the media centre-cropped, so cover it and clip to it.
  const scale = Math.max(target.width / box.width, target.height / box.height);
  const w = target.width / scale;
  const h = target.height / scale;
  return {
    fade: false,
    sheetRadius: parseFloat(getComputedStyle(sheet).borderTopLeftRadius),
    dx: target.left + target.width / 2 - cx,
    dy: target.top + target.height / 2 - cy,
    scale,
    inset: [
      cy - h / 2 - frame.top,
      frame.right - (cx + w / 2),
      frame.bottom - (cy + h / 2),
      cx - w / 2 - frame.left,
    ],
    radius: parseFloat(getComputedStyle(tile).borderTopLeftRadius) / scale,
  };
}

function render() {
  const p = presence.value;
  const q = 1 - p;
  // Pulled down, the sheet shrinks a little and the gallery shows through.
  const pull = Math.min(Math.max(pullY.value, 0) / window.innerHeight, 1);
  const shrink = 1 - pull * 0.3;
  el("player-scrim").style.opacity = Math.max(0, Math.min(1, p * (1 - pull * 1.6)));
  if (!morph) return;
  const scale = (morph.scale + (1 - morph.scale) * p) * shrink;
  const x = morph.dx * q + pullX.value;
  const y = morph.dy * q + pullY.value;
  sheet.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
  sheet.style.opacity = morph.fade ? Math.max(0, Math.min(1, p)) : "";
  if (!morph.fade && q > 0.0005) {
    const [top, right, bottom, left] = morph.inset.map((side) => side * q);
    const corner = morph.radius * q + morph.sheetRadius * p;
    sheet.style.clipPath = `inset(${top}px ${right}px ${bottom}px ${left}px round ${corner}px)`;
  } else {
    sheet.style.clipPath = pull > 0 ? `inset(0 round ${Math.min(pull * 160, 28)}px)` : "";
  }
}

// The item `delta` steps away: the next or previous item of this post, else the first or
// last item of the neighbouring post. Null at either end.
function neighbour(delta) {
  const item = itemIndex + delta;
  if (item >= 0 && item < count(posts[postIndex])) return { postIndex, itemIndex: item };
  const next = postIndex + delta;
  if (next < 0 || next >= posts.length) return null;
  return { postIndex: next, itemIndex: delta > 0 ? 0 : count(posts[next]) - 1 };
}

// Moves to the neighbour at once.
function commit(delta) {
  const to = neighbour(delta);
  if (!to) return;
  const newPost = to.postIndex !== postIndex;
  postIndex = to.postIndex;
  itemIndex = to.itemIndex;
  showMedia();
  if (newPost) {
    editing = false; // moving on drops an unsaved edit
    showInfo();
  }
}

const pageWidth = () => stage.clientWidth + GAP_PX;

// Slides to the neighbour. A press during a slide lands that one and carries on from
// wherever it had got to, so quick presses never queue up or jump.
function page(delta) {
  if (!neighbour(delta)) {
    if (!reducedMotion.matches) pager.to(0, { velocity: -delta * 700, damping: 0.7 }); // the end
    return;
  }
  if (reducedMotion.matches) {
    commit(delta);
    return;
  }
  settlePaging();
  slide(delta, pager.velocity);
}

function slide(delta, velocity) {
  sliding = delta;
  pager.to(-delta * pageWidth(), { response: 0.35, velocity }).then((settled) => {
    if (!settled) return;
    sliding = 0;
    commit(delta);
    pager.set(0);
  });
}

// Lands a slide still under way, keeping its position and speed on screen.
function settlePaging() {
  if (!sliding) return;
  const delta = sliding;
  sliding = 0;
  const { value, velocity } = pager;
  commit(delta);
  pager.set(value + delta * pageWidth(), velocity);
}

function showMedia() {
  const post = posts[postIndex];
  track.replaceChildren(
    slot(neighbour(-1), -1),
    slot({ postIndex, itemIndex }, 0),
    slot(neighbour(1), 1),
  );
  el("player-prev").disabled = !neighbour(-1);
  el("player-next").disabled = !neighbour(1);
  el("player-count").textContent =
    post.items.length > 1 ? `${itemIndex + 1} / ${post.items.length}` : "";
  sheet.classList.toggle("is-page", post.kind !== "media");
}

function slot(at, offset) {
  const box = document.createElement("div");
  box.className = offset ? "slot" : "slot current";
  box.style.setProperty("--offset", offset);
  if (at) box.append(media(posts[at.postIndex], at.itemIndex, offset === 0));
  return box;
}

// The playing video or image for the item shown; a still for the ones either side. A page
// shows its readable copy (its title card either side), a placeholder its logo and caption.
function media(post, index, live) {
  if (post.kind === "page" && live) return readerPane(post);
  const face = post.items.length ? null : faceFor(post);
  if (face) {
    face.classList.add("slot-face");
    face.style.setProperty("--ratio", 9 / 16);
    return face;
  }
  const item = post.items[index];
  let node;
  if (item.kind === "video" && live) {
    node = document.createElement("video");
    Object.assign(node, { src: item.src, controls: true, autoplay: true, playsInline: true });
    if (index === 0) node.poster = post.thumb;
  } else if (item.kind === "video" && index > 0) {
    node = document.createElement("video");
    Object.assign(node, { src: `${item.src}#t=0.1`, muted: true, preload: "metadata" });
  } else {
    node = document.createElement("img");
    node.src = item.kind === "video" ? post.thumb : item.src;
    node.alt = live ? post.caption || post.title || "" : "";
  }
  node.style.setProperty("--ratio", item.width && item.height ? item.width / item.height : 9 / 16);
  return node;
}

// The readable copy, scrolling in the media area; a note when the page had nothing to read.
function readerPane(post) {
  const pane = document.createElement("div");
  pane.className = "reader-pane";
  if (!post.page) {
    pane.append(noCopy(post, "No readable copy: this page shows nothing without JavaScript."));
    return pane;
  }
  loadPage(post).then((html) => {
    if (html === null) pane.replaceChildren(noCopy(post, "The readable copy could not be loaded."));
    else pane.innerHTML = html; // sanitised on the server when it was saved
  });
  return pane;
}

async function loadPage(post) {
  if (pageCache.has(post.page)) return pageCache.get(post.page);
  try {
    const response = await fetch(post.page);
    if (!response.ok) return null;
    const { html } = await response.json();
    pageCache.set(post.page, html);
    return html;
  } catch {
    return null;
  }
}

function noCopy(post, note) {
  const article = document.createElement("article");
  article.className = "reader";
  const kicker = document.createElement("p");
  kicker.className = "reader-kicker";
  kicker.textContent = hostname(post.url);
  const title = document.createElement("h1");
  title.textContent = post.title || post.caption || hostname(post.url);
  article.append(kicker, title);
  if (post.description) {
    const description = document.createElement("p");
    description.textContent = post.description;
    article.append(description);
  }
  const notice = document.createElement("p");
  notice.className = "reader-note";
  notice.textContent = note;
  const open = document.createElement("a");
  open.className = "button";
  open.href = post.url;
  open.target = "_blank";
  open.rel = "noopener";
  open.textContent = `${openLabel(post.platform)} ↗`;
  article.append(notice, open);
  return article;
}

function showInfo() {
  const post = posts[postIndex];
  const waiting = post.kind === "placeholder";
  el("player-view").hidden = editing;
  el("player-editor").hidden = !editing;
  if (editing) {
    renderEditor(el("player-editor"), post, {
      // Nothing is fetched yet, so there is no summary or AI tag to edit.
      placeholder: waiting,
      knownTags: handlers.knownTags(),
      onSave: save,
      onCancel: () => {
        editing = false;
        showInfo();
      },
    });
    return;
  }

  el("player-title").textContent =
    post.title || post.caption || hostname(post.url) || "Saved link";
  const saved = new Date(post.saved_at).toLocaleDateString(undefined, { dateStyle: "medium" });
  el("player-meta").textContent = [post.uploader, `Saved ${saved}`].filter(Boolean).join(" · ");
  el("player-summary").textContent = post.summary || "Summary on its way.";
  el("player-summary").classList.toggle("pending", !post.summary);
  el("player-summary").hidden = waiting;

  const status = waiting && STATUS[post.status];
  el("player-status").hidden = !status;
  if (status) {
    const label = document.createElement("strong");
    label.textContent = `${status.badge}. `;
    el("player-status").replaceChildren(label, post.reason || status.note);
    el("player-status").className = `player-status ${status.tone}`.trim();
  }
  el("player-retry").hidden = !waiting || post.state !== "synced";
  showCookies(post);
  showInlinePage(post);
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
            handlers.onTag(tagKey(kind, name)); // first, so it closes into the refiltered grid
            closePlayer();
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
  el("player-original").hidden = !post.url;
  el("player-original-label").textContent = openLabel(post.platform);
  el("player-notion").href = post.notion_url;
  el("player-notion").hidden = !post.notion_url;
  sheet.querySelector(".player-info").scrollTop = 0;
}

// Whether cookies.txt has a session for the site, read from the file: the site is not asked.
async function showCookies(post) {
  const line = el("player-cookies");
  line.hidden = true;
  if (post.kind !== "placeholder" || !post.url) return;
  let answer;
  try {
    const response = await fetch(`/api/posts/${post.id}/cookies`);
    if (!response.ok) return;
    answer = await response.json();
  } catch {
    return;
  }
  if (posts[postIndex]?.id !== post.id) return; // moved on meanwhile
  const { domain, state, expires } = answer;
  const day = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  line.textContent = {
    present: `${domain} cookies present${expires ? `, until ${day(expires)}` : ""}`,
    expired: `${domain} cookies expired${expires ? ` ${day(expires)}` : ""}`,
    missing: `No ${domain} cookies in cookies.txt`,
  }[state];
  line.className = `player-cookies ${state}`;
  line.hidden = false;
}

// A post with media and a page too (a Reddit video and its comments) shows the page here.
function showInlinePage(post) {
  const box = el("player-page");
  box.replaceChildren();
  box.hidden = !(post.kind === "media" && post.page);
  if (box.hidden) return;
  loadPage(post).then((html) => {
    if (html !== null && posts[postIndex]?.id === post.id) box.innerHTML = html;
  });
}

// Processes the row at once, as the Processing page's Retry does; the header shows progress,
// and the post updates here when it lands.
async function retry() {
  const button = el("player-retry");
  const post = posts[postIndex];
  button.disabled = true;
  try {
    const response = await fetch("/api/processing/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: [post.id], now: true }),
    });
    if (!response.ok) {
      toast(await errorText(response), "error");
      return;
    }
    const result = await response.json();
    document.dispatchEvent(new CustomEvent("vcollect:waiting", { detail: result.new }));
    if (!result.reset.length) {
      toast("Nothing to retry: it is being processed now, or already done.");
      return;
    }
    toast("Retrying now");
    document.dispatchEvent(new CustomEvent("vcollect:retrying"));
  } finally {
    button.disabled = false;
  }
}

// The grid reloaded: swap in the new copies, redrawing the media only when what it shows
// changed (so a playing video carries on).
document.addEventListener("vcollect:posts", (event) => {
  if (!dialog.open || closing || !posts.length) return;
  const fresh = new Map(event.detail.map((post) => [post.id, post]));
  const before = posts[postIndex];
  posts = posts.map((post) => fresh.get(post.id) ?? post);
  const after = posts[postIndex];
  if (JSON.stringify(after) === JSON.stringify(before)) return;
  pageCache.delete(before.page);
  const shows = (post) => JSON.stringify([post.kind, post.items, post.thumb, post.page, post.title]);
  if (shows(after) !== shows(before) && !sliding) {
    itemIndex = Math.min(itemIndex, count(after) - 1);
    showMedia();
  }
  if (!editing) showInfo();
});

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
    closePlayer();
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

// Touch: the media follows the finger from where it was grabbed. Sideways pages, down closes;
// on release the flick's speed decides, and the motion carries on at that speed.
let gesture = null;

stage.addEventListener("pointerdown", (event) => {
  if (event.pointerType === "mouse" || !event.isPrimary) return;
  const target = event.target;
  if (target.tagName === "VIDEO" && event.clientY > target.getBoundingClientRect().bottom - CONTROLS_PX) {
    return; // scrubbing the video
  }
  const caught = closing;
  if (caught) {
    // Caught on its way out: it stays open unless thrown away again.
    closing = false;
    history.pushState({ player: true }, "");
    pushed = true;
    for (const spring of [pullX, pullY, presence]) spring.stop();
  }
  gesture = {
    id: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    axis: null,
    caught,
    from: null,
    tracker: new VelocityTracker(),
    pane: target.closest(".reader-pane"), // a page scrolls; it pulls down only from its top
  };
  gesture.tracker.add(event);
});

stage.addEventListener("pointermove", (event) => {
  if (gesture?.id !== event.pointerId) return;
  gesture.tracker.add(event);
  if (!gesture.axis) {
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    if (Math.hypot(dx, dy) < SLOP_PX) return;
    // The direction is clear: take hold of whatever was moving, where it is now.
    gesture.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    if (gesture.axis === "y" && gesture.pane && !(gesture.pane.scrollTop <= 0 && dy > 0)) {
      gesture = null; // reading: the page scrolls
      return;
    }
    Object.assign(gesture, { x: event.clientX, y: event.clientY });
    if (gesture.axis === "x") settlePaging();
    for (const spring of [pager, pullX, pullY]) spring.stop();
    gesture.from = { page: pager.value, x: pullX.value, y: pullY.value };
    stage.setPointerCapture(event.pointerId);
  }
  const dx = event.clientX - gesture.x;
  const dy = event.clientY - gesture.y;
  if (gesture.axis === "x") {
    let x = gesture.from.page + dx;
    if ((x > 0 && !neighbour(-1)) || (x < 0 && !neighbour(1))) x = rubberband(x, stage.clientWidth);
    pager.set(x);
  } else {
    const y = gesture.from.y + dy;
    pullX.set(gesture.from.x + dx);
    pullY.set(y > 0 ? y : rubberband(y, window.innerHeight));
  }
});

function release(event) {
  if (gesture?.id !== event.pointerId) return;
  const { axis, caught, tracker } = gesture;
  tracker.add(event);
  gesture = null;
  const velocity = tracker.velocity;
  if (axis === "x") {
    const landing = pager.value + project(velocity.x);
    const half = pageWidth() / 2;
    if (landing < -half && neighbour(1)) slide(1, velocity.x);
    else if (landing > half && neighbour(-1)) slide(-1, velocity.x);
    else pager.to(0, { response: 0.35, damping: 0.85, velocity: velocity.x });
  }
  if (axis === "y" && pullY.value + project(velocity.y) > window.innerHeight * 0.25) {
    closePlayer({ velocity });
  } else if (axis === "y" || caught) {
    // Back to rest, carrying the flick's speed; a little bounce, since it was thrown.
    const settle = { response: 0.35, damping: 0.85 };
    pullX.to(0, { ...settle, velocity: axis === "y" ? velocity.x : undefined });
    pullY.to(0, { ...settle, velocity: axis === "y" ? velocity.y : undefined });
    presence.to(1, { response: 0.35 });
  }
}

stage.addEventListener("pointerup", release);
stage.addEventListener("pointercancel", release);

// A page at its top pulls down instead of scrolling (the browser would bounce it otherwise);
// anywhere else, or upwards, it scrolls as usual.
let pageTouch = null;
stage.addEventListener(
  "touchstart",
  (event) => {
    const pane = event.target.closest(".reader-pane");
    pageTouch = pane && pane.scrollTop <= 0 ? event.touches[0].clientY : null;
  },
  { passive: true },
);
stage.addEventListener(
  "touchmove",
  (event) => {
    if (pageTouch !== null && event.touches[0].clientY > pageTouch && event.cancelable) {
      event.preventDefault();
    }
  },
  { passive: false },
);

el("player-prev").addEventListener("click", () => page(-1));
el("player-next").addEventListener("click", () => page(1));
el("player-close").addEventListener("click", () => closePlayer());
el("player-scrim").addEventListener("click", () => closePlayer());
el("player-edit").addEventListener("click", () => {
  editing = true;
  showInfo();
});
el("player-delete").addEventListener("click", remove);
el("player-readd").addEventListener("click", readd);
el("player-retry").addEventListener("click", retry);
// Escape closes the same way as the close button, shrinking back into the tile.
dialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closePlayer();
});
dialog.addEventListener("close", () => {
  closing = false;
  sliding = 0;
  for (const spring of [pager, pullX, pullY, presence]) spring.stop();
  track.replaceChildren(); // stops playback
  pageCache.clear();
  sheet.style.transform = sheet.style.clipPath = sheet.style.opacity = "";
  if (pushed) {
    pushed = false;
    history.back();
  }
});
window.addEventListener("popstate", () => {
  if (!pushed) return;
  pushed = false;
  closePlayer({ fromHistory: true });
});
// On the document, not the dialog: paging replaces the video, and focus can go with it.
document.addEventListener(
  "keydown",
  (event) => {
    if (!dialog.open || closing || event.target.closest("input, textarea")) return; // typing
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault(); // the video would otherwise seek
      page(event.key === "ArrowRight" ? 1 : -1);
    } else if (event.key === " " && !event.target.closest("button, a, video")) {
      const video = track.querySelector(".slot.current video");
      if (video) {
        event.preventDefault();
        if (video.paused) video.play();
        else video.pause();
      } else scrollPage(event, event.shiftKey ? -0.9 : 0.9);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      scrollPage(event, event.key === "ArrowDown" ? 0.12 : -0.12);
    } else if (event.key === "PageDown" || event.key === "PageUp") {
      scrollPage(event, event.key === "PageDown" ? 0.9 : -0.9);
    }
  },
  true,
);

// The keys that scroll a page, while the sheet (not the page) has focus. `share` is of a screen.
function scrollPage(event, share) {
  const pane = track.querySelector(".slot.current .reader-pane");
  if (!pane) return;
  event.preventDefault();
  const behavior = reducedMotion.matches ? "auto" : "smooth";
  pane.scrollBy({ top: pane.clientHeight * share, behavior });
}
