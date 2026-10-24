// Gallery page. Talks to the server only through the JSON API (/api/...).

// Every page load pulls from Notion; the server reuses a pull from the last minute.
async function pullFromNotion() {
  let result;
  try {
    const response = await fetch("/api/pull", { method: "POST" });
    if (!response.ok) return; // /api/health explains why
    result = await response.json();
  } catch {
    return;
  }
  document.getElementById("sync").textContent = result.new ? `Sync (${result.new} new)` : "Sync";
}

async function showServices() {
  const list = document.getElementById("services");
  let health;
  try {
    const response = await fetch("/api/health");
    health = await response.json();
  } catch {
    list.replaceChildren(serviceRow("Server", "unreachable", false));
    return;
  }

  const { notion, ollama, whisper } = health;
  let notionState = "connected";
  if (!notion.configured) notionState = "set NOTION_TOKEN and NOTION_DATA_SOURCE_ID";
  else if (notion.error) notionState = notion.error;

  let ollamaState = `${ollama.model} ready`;
  if (!ollama.reachable) ollamaState = "unreachable";
  else if (!ollama.model_ready) ollamaState = `${ollama.model} not pulled yet`;

  list.replaceChildren(
    serviceRow("Notion", notionState, notion.configured && !notion.error),
    serviceRow("Ollama", ollamaState, ollama.reachable && ollama.model_ready),
    serviceRow("Whisper", whisper.reachable ? "ready" : "unreachable", whisper.reachable),
  );
}

function serviceRow(name, state, ok) {
  const row = document.createElement("li");
  row.className = ok ? "ok" : "warn";
  row.textContent = `${name}: ${state}`;
  return row;
}

pullFromNotion().finally(showServices);
