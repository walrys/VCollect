// What a post without a picture of its own shows: a title card for a page (the page's title,
// with its site's logo and domain), a placeholder for a link Sync has yet to fetch (the logo,
// the caption, and why it waits). Drawn here rather than baked into images, so they stay
// sharp and follow the theme; the grid, the player and the dialog before Sync share them.

const PLATFORMS = new Set(["instagram", "youtube", "reddit"]);
const SITE_NAMES = { instagram: "Instagram", youtube: "YouTube", reddit: "Reddit" };

// The badge on a placeholder, and what the player says about it.
export const STATUS = {
  new: { badge: "New", tone: "", note: "No Sync has tried this link yet." },
  login: { badge: "Needs login", tone: "warn", note: "" },
  waiting: { badge: "Waiting", tone: "", note: "The next Sync tries it again." },
  failed: { badge: "Failed", tone: "danger", note: "Sync skips it until it is retried." },
};

/** A site's logo, coloured by CSS (see .logo in app.css). */
export function logo(platform, className = "") {
  const mark = document.createElement("span");
  mark.className = `logo ${className}`.trim();
  mark.dataset.platform = PLATFORMS.has(platform) ? platform : "other";
  mark.setAttribute("aria-hidden", "true");
  return mark;
}

export function hostname(url) {
  try {
    return new URL(url).hostname.replace(/^(www|m)\./, "");
  } catch {
    return "";
  }
}

/** "Open on Reddit", or "Open link" for a site without a name here. */
export function openLabel(platform) {
  return SITE_NAMES[platform] ? `Open on ${SITE_NAMES[platform]}` : "Open link";
}

/** The page's title, large, over its site. */
export function titleCard(post) {
  const card = document.createElement("span");
  card.className = "face title-card";
  card.dataset.platform = post.platform ?? "other";
  const title = document.createElement("span");
  title.className = "face-title";
  title.textContent = post.title || post.caption || hostname(post.url) || "Saved link";
  const domain = document.createElement("span");
  domain.className = "face-domain";
  domain.textContent = hostname(post.url);
  const site = document.createElement("span");
  site.className = "face-site";
  site.append(logo(post.platform), domain);
  card.append(body(title, site));
  return card;
}

/** The site's logo over the user's caption (or the link's domain), and the status badge. */
export function placeholder(post, { badge = true } = {}) {
  const card = document.createElement("span");
  card.className = "face placeholder";
  card.dataset.platform = post.platform ?? "other";
  const name = document.createElement("span");
  name.className = "face-name";
  name.textContent = post.caption || hostname(post.url) || "No link";
  card.append(body(logo(post.platform, "face-logo"), name));
  const status = STATUS[post.status];
  if (badge && status) {
    const chip = document.createElement("span");
    chip.className = `badge ${status.tone}`.trim();
    chip.textContent = status.badge;
    const badges = document.createElement("span");
    badges.className = "badges";
    badges.append(chip);
    card.append(badges);
  }
  return card;
}

// The face is the container its sizes are measured against (cqi), so they can't apply to the
// face itself: its layout lives on this inner box.
function body(...children) {
  const box = document.createElement("span");
  box.className = "face-body";
  box.append(...children);
  return box;
}

/** The face a post shows without its own picture, or null when it has one. */
export function faceFor(post, options) {
  if (post.kind === "placeholder") return placeholder(post, options);
  if (post.kind === "page" || !post.thumb) return titleCard(post);
  return null;
}
