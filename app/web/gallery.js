// The grid: tag filters (instant, in the browser), search (on the server), date groups.

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
    clearTimeout(timer);
    timer = setTimeout(runSearch, SEARCH_DELAY_MS);
  });
}

export async function loadPosts() {
  try {
    const response = await fetch("/api/posts");
    if (!response.ok) return;
    state.posts = (await response.json()).posts;
    state.loaded = true;
  } catch {
    return;
  }
  render();
}

export function addTagFilter(key) {
  state.selected.add(key);
  render();
}

async function runSearch() {
  const text = el("search").value.trim();
  if (!text) {
    state.searchIds = null;
    render();
    return;
  }
  const response = await fetch(`/api/search?q=${encodeURIComponent(text)}`);
  if (!response.ok || el("search").value.trim() !== text) return; // a newer search is coming
  state.searchIds = new Set((await response.json()).ids);
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
      more.className = "link-button";
      more.textContent = state.expanded[kind] ? "Show fewer" : `Show all ${sorted.length}`;
      more.addEventListener("click", () => {
        state.expanded[kind] = !state.expanded[kind];
        render();
      });
      row.append(more);
    }
    row.hidden = sorted.length === 0;
  }
}

function renderGrid(visible) {
  const grid = el("grid");
  const groups = state.grouped ? groupByDate(visible) : [[null, visible]];
  grid.replaceChildren(
    ...groups.map(([heading, posts]) => {
      const section = document.createElement("section");
      section.className = "group";
      if (heading) {
        const title = document.createElement("h2");
        title.className = "group-heading";
        title.textContent = heading;
        section.append(title);
      }
      const tiles = document.createElement("div");
      tiles.className = "tiles";
      tiles.append(...posts.map((post) => tile(post, visible)));
      section.append(tiles);
      return section;
    }),
  );
}

function tile(post, visible) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tile";
  const image = document.createElement("img");
  image.src = post.thumb;
  image.loading = "lazy";
  image.alt = post.caption || post.title || "";
  button.append(image);

  const badges = document.createElement("div");
  badges.className = "badges";
  if (post.items.length > 1) badges.append(badge(`1/${post.items.length}`));
  else if (post.items[0]?.kind === "image") badges.append(badge("Photo"));
  if (post.state === "deleted_in_notion") badges.append(badge("Deleted in Notion", "warn"));
  if (post.state === "local_only") badges.append(badge("Not in Notion", "warn"));
  button.append(badges);

  const label = post.caption || post.title;
  if (label) {
    const caption = document.createElement("span");
    caption.className = "tile-caption";
    caption.textContent = label;
    button.append(caption);
  }
  button.addEventListener("click", () =>
    openPlayer(visible, visible.indexOf(post), { onTag: addTagFilter }),
  );
  return button;
}

function badge(text, tone = "") {
  const span = document.createElement("span");
  span.className = `badge ${tone}`;
  span.textContent = text;
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
