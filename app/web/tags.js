// Tag chips, shared by the tag bar and the player.

// Tags match case-insensitively, as in Notion; a kind keeps Notion tags and AI tags apart.
export function tagKey(kind, name) {
  return `${kind}:${name.toLowerCase()}`;
}

export function tagChip(kind, name, { count, selected = false, onClick }) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = `chip chip-${kind}${selected ? " selected" : ""}`;
  chip.setAttribute("aria-pressed", String(selected));
  chip.textContent = name;
  if (count !== undefined) {
    const badge = document.createElement("span");
    badge.className = "chip-count";
    badge.textContent = count;
    chip.append(badge);
  }
  chip.addEventListener("click", onClick);
  return chip;
}
