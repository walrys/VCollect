// The player's edit mode: link, caption, summary and both kinds of tags (a placeholder has only
// its link, caption and Notion tags). Save hands the draft back; the player sends it to the
// server, which passes it on to Notion. A post whose link changes then offers Resync.

export function renderEditor(form, post, { knownTags, onSave, onCancel, placeholder = false }) {
  const draft = { tags: [...post.tags], ai_tags: [...post.ai_tags] };
  const link = document.createElement("input");
  Object.assign(link, {
    className: "editor-link",
    type: "text", // type=url would refuse a link typed without https://
    inputMode: "url",
    autocomplete: "off",
    spellcheck: false,
    value: post.url,
    placeholder: "https://…",
  });
  link.setAttribute("autocapitalize", "off");
  const caption = textarea(post.caption, 2);
  const summary = textarea(post.summary, 6);
  const tagRows = {
    tags: tagEditor("tags", "notion", draft, knownTags.notion, () => redraw()),
    ai_tags: tagEditor("ai_tags", "ai", draft, knownTags.ai, () => redraw()),
  };
  const redraw = () => Object.values(tagRows).forEach((row) => row.redraw());

  const save = button("Save", "button");
  save.type = "submit";
  const cancel = button("Cancel", "link-button");
  cancel.addEventListener("click", onCancel);
  const buttons = document.createElement("div");
  buttons.className = "player-actions";
  buttons.append(save, cancel);

  form.onsubmit = async (event) => {
    event.preventDefault();
    tagRows.tags.addTyped();
    tagRows.ai_tags.addTyped();
    save.disabled = true;
    try {
      const changes = { url: link.value, caption: caption.value, tags: draft.tags };
      if (!placeholder) Object.assign(changes, { summary: summary.value, ai_tags: draft.ai_tags });
      await onSave(changes);
    } finally {
      save.disabled = false;
    }
  };
  form.replaceChildren(
    ...(placeholder
      ? [field("Link", link), field("Caption", caption), field("Tags", tagRows.tags.element)]
      : [
          field("Link", link),
          field("Caption", caption),
          field("Summary", summary),
          field("Tags", tagRows.tags.element),
          field("AI tags", tagRows.ai_tags.element),
        ]),
    buttons,
  );
  caption.focus();
}

let lists = 0; // each tag box suggests from a datalist of its own

// Chips with a remove button, and a box that adds a tag on Enter or comma; `draft[field]`
// holds the tags. AI tag chips also offer "Promote", which copies the tag into the Notion tags.
// Also the Add a link panel's tag box.
export function tagEditor(field, kind, draft, known, onChange) {
  const element = document.createElement("div");
  const chips = document.createElement("div");
  chips.className = "tags";
  const input = document.createElement("input");
  input.className = "tag-input";
  input.placeholder = kind === "ai" ? "Add an AI tag" : "Add a tag";
  input.setAttribute("aria-label", kind === "ai" ? "Add an AI tag" : "Add a tag");
  const list = document.createElement("datalist");
  list.id = `known-${field}-${++lists}`;
  list.append(...known.map((name) => Object.assign(document.createElement("option"), { value: name })));
  input.setAttribute("list", list.id);
  element.append(chips, input, list);

  const add = (name) => {
    const clean = name.replace(/,/g, " ").trim().replace(/\s+/g, " ");
    const lower = (tag) => tag.toLowerCase();
    if (clean && !draft[field].map(lower).includes(lower(clean))) {
      draft[field].push(kind === "ai" ? lower(clean) : clean);
    }
  };
  const addTyped = () => {
    add(input.value);
    input.value = "";
    onChange();
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      addTyped();
    }
  });

  const redraw = () => {
    chips.replaceChildren(
      ...draft[field].map((name) => {
        const chip = document.createElement("span");
        chip.className = `chip chip-${kind}`;
        chip.append(name);
        if (kind === "ai") {
          const promote = button("Promote", "chip-action");
          promote.title = "Copy into the Notion tags";
          promote.disabled = draft.tags.map((t) => t.toLowerCase()).includes(name.toLowerCase());
          promote.addEventListener("click", () => {
            draft.tags.push(name);
            onChange();
          });
          chip.append(promote);
        }
        const remove = button("×", "chip-action");
        remove.setAttribute("aria-label", `Remove ${name}`);
        remove.addEventListener("click", () => {
          draft[field] = draft[field].filter((tag) => tag !== name);
          onChange();
        });
        chip.append(remove);
        return chip;
      }),
    );
  };
  redraw();
  return { element, redraw, addTyped };
}

// A <label> only around a single text box: clicking anywhere in a label activates its first
// control, which in a tag editor would be a chip's remove button.
function field(label, control) {
  const single = control.tagName === "TEXTAREA" || control.tagName === "INPUT";
  const wrapper = document.createElement(single ? "label" : "div");
  wrapper.className = "editor-field";
  const text = document.createElement("span");
  text.textContent = label;
  wrapper.append(text, control);
  return wrapper;
}

function textarea(value, rows) {
  const box = document.createElement("textarea");
  box.value = value;
  box.rows = rows;
  return box;
}

function button(text, className) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = className;
  element.textContent = text;
  return element;
}
