// Gallery page. Talks to the server only through the JSON API (/api/...).

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

  const { ollama, whisper } = health;
  let ollamaState = `${ollama.model} ready`;
  if (!ollama.reachable) ollamaState = "unreachable";
  else if (!ollama.model_ready) ollamaState = `${ollama.model} not pulled yet`;

  list.replaceChildren(
    serviceRow(
      "Notion",
      health.notion_configured ? "configured" : "set NOTION_TOKEN and NOTION_DATA_SOURCE_ID",
      health.notion_configured,
    ),
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

showServices();
