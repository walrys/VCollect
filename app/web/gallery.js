// The grid: tag filters (instant, in the browser), search (on the server), date groups.
// Every Notion row has a tile: its picture, a title card for a page, or a placeholder until
// Sync has fetched it.

import { faceFor, STATUS } from "./faces.js";
import { openPlayer } from "./player.js";
import { tagChip, tagKey } from "./tags.js";

const el = (id) => document.getElementById(id);
const TOP_TAGS = 20;
const SEARCH_DELAY_MS = 300;
const KINDS = { notion: "tags", ai: "ai_tags" }; // tag kind -> post field

const state = {
  loaded: false,
  posts: [],
  selected: new Set(), // tag keys: "notion:<name>" or "ai:<name>", lower-cased
  searchIds: null, // Set of matching post ids, or null with an empty search box
  grouped: false,
  expanded: { notion: false, ai: false },
};

export function initGallery() {
  const groupBox = el("group-by-date");
  state.grouped = readSetting("groupByDate") === "1";
  groupBox.checked = state.grouped;
  groupBox.addEventListener("change", () => {
    state.grouped = groupBox.checked;
    writeSetting("groupByDate", state.grouped ? "1" : "0");
    render();
  });

  let timer;
  el("search").addEventListener("input", () => {
    if (location.hash === "#processing") location.hash = ""; // searching goes back to the grid
    el("search-field").classList.toggle("busy", el("search").value.trim() !== "");
    clearTimeout(timer);
    timer = setTimeout(runSearch, SEARCH_DELAY_MS);
  });
  // "/" jumps to the search box, as on most sites with one.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target.closest("input, textarea, select, dialog")) return;
    event.preventDefault();
    el("search").focus();
  });
  el("clear-filters").addEventListener("click", () => {
    state.selected.clear();
    el("search").value = "";
    runSearch();
  });
}

let lastLoaded = null; // the /api/posts answer on screen, until the page changes a post

export async function loadPosts() {
  try {
    const response = await fetch("/api/posts");
    if (!response.ok) return;
    const body = await response.text();
    if (body === lastLoaded) return; // nothing new: the grid stays as it is
    lastLoaded = body;
    state.posts = JSON.parse(body).posts;
    state.loaded = true;
  } catch {
    return;
  }
  render();
  // An open player swaps in the new copy of its post: a placeholder retried becomes the post.
  document.dispatchEvent(new CustomEvent("vcollect:posts", { detail: state.posts }));
}

// The posts the dialog before Sync asks about.
export function postsDeletedInNotion() {
  return state.posts.filter((post) => post.state === "deleted_in_notion");
}

export function addTagFilter(key) {
  state.selected.add(key);
  render();
}

// After an edit saved in the player.
function updatePost(post) {
  const index = state.posts.findIndex((p) => p.id === post.id);
  if (index >= 0) state.posts[index] = post;
  lastLoaded = null;
  if (state.searchIds) runSearch(); // the edit may change what matches
  else render();
}

// After a delete in the player; `waiting` is the new count of rows for Sync.
function removePost(id, waiting) {
  state.posts = state.posts.filter((post) => post.id !== id);
  lastLoaded = null;
  document.dispatchEvent(new CustomEvent("vcollect:waiting", { detail: waiting }));
  render();
}

// Every tag name in use, for the editor's suggestions.
function knownTags() {
  const names = (field) =>
    [...new Set(state.posts.flatMap((post) => post[field]))].sort((a, b) => a.localeCompare(b));
  return { notion: names("tags"), ai: names("ai_tags") };
}

async function runSearch() {
  const text = el("search").value.trim();
  if (!text) {
    state.searchIds = null;
    el("search-field").classList.remove("busy");
    render();
    return;
  }
  el("search-field").classList.add("busy");
  let ids;
  try {
    const response = await fetch(`/api/search?q=${encodeURIComponent(text)}`);
    if (el("search").value.trim() !== text) return; // a newer search is coming
    if (response.ok) ids = (await response.json()).ids;
  } catch {
    // The server is down; the grid stays as it was.
  }
  if (el("search").value.trim() !== text) return;
  el("search-field").classList.remove("busy");
  if (!ids) return;
  state.searchIds = new Set(ids);
  render();
}

function visiblePosts() {
  return state.posts.filter((post) => {
    if (state.searchIds && !state.searchIds.has(post.id)) return false;
    const keys = new Set(
      Object.entries(KINDS).flatMap(([kind, field]) => post[field].map((n) => tagKey(kind, n))),
    );
    return [...state.selected].every((key) => keys.has(key));
  });
}

function render() {
  const visible = visiblePosts();
  renderTags(visible);
  renderGrid(visible);
  const filtered = state.selected.size > 0 || state.searchIds !== null;
  el("results").hidden = !filtered;
  el("results-count").textContent = `${visible.length} of ${state.posts.length} ${
    state.posts.length === 1 ? "post" : "posts"
  }`;
  el("empty").hidden = !state.loaded || state.posts.length > 0;
  el("no-match").hidden = state.posts.length === 0 || visible.length > 0;
}

// Counts come from the posts on screen, so a click never leads to an empty grid.
function renderTags(visible) {
  for (const [kind, field] of Object.entries(KINDS)) {
    const counts = new Map();
    for (const post of visible) {
      for (const name of post[field]) {
        const key = tagKey(kind, name);
        const entry = counts.get(key) ?? { name, count: 0 };
        entry.count += 1;
        counts.set(key, entry);
      }
    }
    const sorted = [...counts].sort(
      ([keyA, a], [keyB, b]) =>
        state.selected.has(keyB) - state.selected.has(keyA) ||
        b.count - a.count ||
        a.name.localeCompare(b.name),
    );
    const shown = state.expanded[kind] ? sorted : sorted.slice(0, TOP_TAGS);
    const row = el(`tags-${kind}`);
    row.replaceChildren(
      ...shown.map(([key, { name, count }]) =>
        tagChip(kind, name, {
          count,
          selected: state.selected.has(key),
          onClick: () => {
            if (state.selected.has(key)) state.selected.delete(key);
            else state.selected.add(key);
            render();
          },
        }),
      ),
    );
    if (sorted.length > TOP_TAGS) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "chip tag-more";
      more.textContent = state.expanded[kind] ? "Show fewer" : `Show all ${sorted.length}`;
      more.addEventListener("click", () => {
        state.expanded[kind] = !state.expanded[kind];
        render();
      });
      row.append(more);
    }
    row.classList.toggle("expanded", state.expanded[kind]);
    row.hidden = sorted.length === 0;
  }
}

// Tiles are kept between renders, so a filter change moves them instead of reloading images.
const tiles = new Map(); // post id -> { json, element }
let onScreen = []; // the posts in the grid, in order: the player steps through these

function renderGrid(visible) {
  onScreen = visible;
  const groups = state.grouped ? groupByDate(visible) : [[null, visible]];
  el("grid").replaceChildren(
    ...groups.map(([heading, posts]) => {
      const section = document.createElement("section");
      section.className = "group";
      if (heading) {
        const title = document.createElement("h2");
        title.className = "group-heading";
        const count = document.createElement("span");
        count.className = "group-count";
        count.textContent = posts.length;
        title.append(heading, count);
        section.append(title);
      }
      const grid = document.createElement("div");
      grid.className = "tiles";
      grid.append(...posts.map(tileFor));
      section.append(grid);
      return section;
    }),
  );
  const ids = new Set(state.posts.map((post) => post.id));
  for (const id of tiles.keys()) if (!ids.has(id)) tiles.delete(id);
}

function tileFor(post) {
  const json = JSON.stringify(post);
  const kept = tiles.get(post.id);
  if (kept?.json === json) return kept.element;
  const element = tile(post);
  tiles.set(post.id, { json, element });
  return element;
}

function tile(post) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `tile tile-${post.kind}`;
  const face = faceFor(post, { badge: false });
  if (face) {
    button.append(face);
    button.setAttribute("aria-label", post.title || post.caption || post.url);
  } else {
    const image = document.createElement("img");
    image.loading = "lazy";
    image.decoding = "async";
    image.alt = post.caption || post.title || "";
    image.src = post.thumb;
    // Fades in once loaded, rather than popping in line by line.
    if (!image.complete) {
      image.classList.add("loading");
      const shown = () => image.classList.remove("loading");
      image.addEventListener("load", shown, { once: true });
      image.addEventListener("error", shown, { once: true });
    }
    button.append(image);
  }

  const badges = document.createElement("div");
  badges.className = "badges";
  const status = post.kind === "placeholder" && STATUS[post.status];
  if (status) badges.append(badge(status.badge, status.tone));
  if (post.items.length > 1) badges.append(badge(`1/${post.items.length}`, "", ICONS.stack));
  else if (post.items[0]?.kind === "image") badges.append(badge("Photo", "", ICONS.photo));
  if (post.state === "deleted_in_notion") badges.append(badge("Deleted in Notion", "warn"));
  if (post.state === "local_only") badges.append(badge("Not in Notion", "warn"));
  button.append(badges);

  // A title card and a placeholder carry their own words.
  const label = !face && (post.caption || post.title);
  if (label) {
    const caption = document.createElement("span");
    caption.className = "tile-caption";
    caption.textContent = label;
    button.append(caption);
  }
  button.addEventListener("click", () => {
    const list = [...onScreen];
    openPlayer(list, list.findIndex((p) => p.id === post.id), {
      onTag: addTagFilter,
      onChange: updatePost,
      onDelete: removePost,
      knownTags,
      tileFor: revealTile,
    });
  });
  return button;
}

// The tile the player opens from and closes into, scrolled into view behind it if the player
// has moved on to a post further along. Null when the post is not in the grid.
function revealTile(id) {
  const element = tiles.get(id)?.element;
  if (!element?.isConnected) return null;
  const rect = element.getBoundingClientRect();
  const top = el("topbar").getBoundingClientRect().bottom;
  if (rect.bottom < top || rect.top > window.innerHeight) {
    element.scrollIntoView({ block: "center", behavior: "instant" });
  }
  return element;
}

const ICONS = {
  stack: '<rect x="3" y="6" width="11" height="11" rx="2"/><path d="M7 3h8a2 2 0 0 1 2 2v8"/>',
  photo: '<rect x="3" y="4" width="14" height="12" rx="2"/><circle cx="8" cy="9" r="1.5"/><path d="m17 13-4-4-7 7"/>',
};

function badge(text, tone = "", icon = "") {
  const span = document.createElement("span");
  span.className = `badge ${tone}`;
  if (icon) {
    span.insertAdjacentHTML("afterbegin", `<svg viewBox="0 0 20 20" aria-hidden="true">${icon}</svg>`);
  }
  span.append(text);
  return span;
}

// Today, Yesterday, This Week (from Monday), Last Week, then one group per month.
// Posts arrive newest first, so groups come out in order.
export function groupByDate(posts, now = new Date()) {
  const groups = [];
  for (const post of posts) {
    const heading = dateGroup(new Date(post.saved_at), now);
    if (groups.at(-1)?.[0] !== heading) groups.push([heading, []]);
    groups.at(-1)[1].push(post);
  }
  return groups;
}

export function dateGroup(date, now) {
  const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const today = startOfDay(now);
  const day = startOfDay(date);
  const daysAgo = Math.round((today - day) / 86_400_000);
  if (daysAgo <= 0) return "Today";
  if (daysAgo === 1) return "Yesterday";
  const thisWeek = new Date(today);
  thisWeek.setDate(today.getDate() - ((today.getDay() + 6) % 7));
  if (day >= thisWeek) return "This Week";
  const lastWeek = new Date(thisWeek);
  lastWeek.setDate(thisWeek.getDate() - 7);
  if (day >= lastWeek) return "Last Week";
  return date.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function readSetting(key) {
  try {
    return localStorage.getItem(`vcollect.${key}`);
  } catch {
    return null;
  }
}

function writeSetting(key, value) {
  try {
    localStorage.setItem(`vcollect.${key}`, value);
  } catch {
    // Storage is off (private window); the setting just isn't remembered.
  }
}
