// Before Sync: the posts whose Notion rows were deleted. Ticked ones are deleted here too;
// the rest stay in the gallery as local-only posts and are never asked about again.

import { faceFor } from "./faces.js";
import { errorText, toast } from "./toast.js";

const el = (id) => document.getElementById(id);

/** Resolves to true once the choice is saved, or false if the user cancels. */
export function askAboutDeleted(posts) {
  const dialog = el("deleted");
  const tiles = posts.map(choiceTile);
  const boxes = tiles.map((tile) => tile.querySelector("input"));
  const update = () => {
    const ticked = boxes.filter((box) => box.checked).length;
    el("deleted-go").textContent = ticked ? `Delete ${ticked} and Sync` : "Keep all and Sync";
    el("deleted-go").classList.toggle("destructive", ticked > 0);
    el("deleted-all").textContent = ticked === boxes.length ? "Untick all" : "Tick all";
  };

  el("deleted-title").textContent =
    posts.length === 1 ? "1 post was deleted in Notion" : `${posts.length} posts were deleted in Notion`;
  el("deleted-tiles").replaceChildren(...tiles);
  el("deleted-tiles").onchange = update;
  el("deleted-all").onclick = () => {
    const tick = !boxes.every((box) => box.checked);
    boxes.forEach((box) => (box.checked = tick));
    update();
  };
  update();
  dialog.returnValue = "";
  dialog.showModal();

  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      async () => {
        if (dialog.returnValue !== "go") {
          resolve(false);
          return;
        }
        const choice = { delete: [], keep: [] };
        posts.forEach((post, index) => choice[boxes[index].checked ? "delete" : "keep"].push(post.id));
        const response = await fetch("/api/posts/deleted-in-notion", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(choice),
        });
        if (!response.ok) toast(await errorText(response), "error");
        resolve(response.ok);
      },
      { once: true },
    );
  });
}

function choiceTile(post) {
  const tile = document.createElement("label");
  tile.className = "deleted-tile";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = true;
  box.setAttribute("aria-label", `Delete ${post.caption || post.title || post.url}`);
  // A page or a link never fetched has no picture: its title card or placeholder, which
  // carries its own words.
  const face = faceFor(post, { badge: false });
  if (face) {
    tile.append(face, box);
    return tile;
  }
  const image = document.createElement("img");
  image.src = post.thumb;
  image.alt = "";
  image.loading = "lazy";
  const caption = document.createElement("span");
  caption.className = "tile-caption";
  caption.textContent = post.caption || post.title || post.url;
  tile.append(image, box, caption);
  return tile;
}
