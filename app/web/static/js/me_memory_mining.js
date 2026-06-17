// User-facing memory-mining consent toggle.

const $ = (id) => document.getElementById(id);

async function api(path, init = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...init,
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function render(optedIn) {
  const badge = $("mm-badge");
  const toggle = $("mm-toggle");
  badge.textContent = optedIn ? "Opted in" : "Opted out";
  badge.className = "mm-badge " + (optedIn ? "mm-on" : "mm-off");
  toggle.textContent = optedIn ? "Opt out" : "Opt in";
  toggle.disabled = false;
  toggle.dataset.optedIn = optedIn ? "1" : "";
}

async function init() {
  const toggle = $("mm-toggle");
  try {
    const s = await api("/api/studio/memory-mining/consent");
    render(!!s.opted_in);
  } catch (e) {
    $("mm-badge").textContent = "unavailable";
  }
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    const next = !toggle.dataset.optedIn;
    try {
      const s = await api("/api/studio/memory-mining/consent", {
        method: "POST",
        body: JSON.stringify({ opt_in: next }),
      });
      render(!!s.opted_in);
      if (window.appToast) window.appToast(s.opted_in ? "Opted in to memory mining" : "Opted out");
    } catch (e) {
      toggle.disabled = false;
      if (window.appToast) window.appToast(`Failed: ${e.message}`);
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
