// A short status line at the bottom of the screen.

const SHOW_MS = 4000;
let timer;

export function toast(message, tone = "") {
  const box = document.getElementById("toast");
  box.textContent = message;
  box.className = `toast ${tone}`;
  // As a popover it sits in the top layer, so it shows above the open player too.
  if (box.matches(":popover-open")) box.hidePopover();
  box.showPopover();
  clearTimeout(timer);
  timer = setTimeout(() => box.hidePopover(), tone === "error" ? SHOW_MS * 2 : SHOW_MS);
}

// FastAPI errors carry `detail` as text, as {message}, or as a list of validation errors.
export async function errorText(response) {
  try {
    const { detail } = await response.json();
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((error) => error.msg).join("; ");
    return detail?.message ?? `Request failed (${response.status})`;
  } catch {
    return `Request failed (${response.status})`;
  }
}
