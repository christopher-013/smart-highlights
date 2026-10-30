(() => {
  const API = (window.SMART_HIGHLIGHTS_API || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);
  const PLAYER_RE = /web\.gc\.com\/teams\/[A-Za-z0-9]+(\/[^/?#]+)?\/players\/[0-9a-fA-F-]{36}/;
  const KEEP = ["player_url", "player_name", "season", "sport", "caption", "privacy", "top_n"];

  const store = {
    get(k) { try { return sessionStorage.getItem("sh_" + k); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem("sh_" + k, v); } catch { /* private window: fine */ } },
    del(k) { try { sessionStorage.removeItem("sh_" + k); } catch { /* ignore */ } },
  };

  // ---- coming back from Google (connect YouTube) or to a job's progress page
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get("yt")) {
    store.set("yt", hash.get("yt"));
    store.set("channel", hash.get("channel") || "");
    store.set("channel_title", hash.get("channel_title") || "");
    history.replaceState(null, "", location.pathname + "#maker");
  }
  if (hash.get("yt_error")) {
    $("yt-status").textContent = hash.get("yt_error") === "no_channel"
      ? "That Google account has no YouTube channel. Connect the account that owns the channel."
      : "YouTube wasn't connected. Try again.";
    history.replaceState(null, "", location.pathname + "#maker");
  }

  // ---- restore what was typed before going to Google (never the password)
  for (const k of KEEP) { const v = store.get(k); if (v !== null && $(k)) $(k).value = v; }
  const savedReels = store.get("reels");
  if (savedReels) document.querySelectorAll("input[name=reel]").forEach((c) => { c.checked = savedReels.split(",").includes(c.value); });
  const remember = () => {
    for (const k of KEEP) if ($(k)) store.set(k, $(k).value);
    store.set("reels", [...document.querySelectorAll("input[name=reel]:checked")].map((c) => c.value).join(","));
  };
  $("form").addEventListener("input", remember);

  function showYouTube() {
    const sid = store.get("yt");
    const ch = store.get("channel"), title = store.get("channel_title");
    if (sid) {
      $("yt-status").className = "status ok";
      $("yt-status").textContent = "Connected: " + (ch ? "@" + ch : title || "your channel");
      $("yt-connect").textContent = "Use a different channel";
    }
  }
  showYouTube();

  if (!API) {
    $("offline").hidden = false;
    $("submit").disabled = true;
    $("yt-connect").disabled = true;
  }

  $("yt-connect").addEventListener("click", () => {
    remember();
    location.href = API + "/auth/google/start?return=" + encodeURIComponent(location.origin + location.pathname);
  });

  // ---- start a reel
  $("player_url").addEventListener("blur", () => {
    const v = $("player_url").value.trim();
    $("player_url-error").textContent = v && !PLAYER_RE.test(v)
      ? "This should be a player's page on web.gc.com, like https://web.gc.com/teams/…/players/…" : "";
  });

  $("form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("form-error");
    err.textContent = "";
    const reels = [...document.querySelectorAll("input[name=reel]:checked")].map((c) => c.value);
    const problems = [];
    if (!store.get("yt")) problems.push("Connect your YouTube channel (step 1).");
    if (!PLAYER_RE.test($("player_url").value)) problems.push("Paste the player's GameChanger link (step 2).");
    if (!$("player_name").value.trim()) problems.push("Enter the name for the captions (step 2).");
    if (!reels.length) problems.push("Pick at least one reel (step 3).");
    if (!$("gc_email").value || !$("gc_password").value) problems.push("Enter your GameChanger email and password (step 4).");
    if (!$("rights").checked) problems.push("Confirm you're allowed to use and post these videos (step 4).");
    if (problems.length) { err.textContent = problems.join(" "); return; }

    $("submit").disabled = true;
    $("submit").textContent = "Starting…";
    try {
      const r = await fetch(API + "/api/jobs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          player_url: $("player_url").value.trim(), player_name: $("player_name").value.trim(),
          season: $("season").value.trim(), sport: $("sport").value, reels,
          top_n: Number($("top_n").value) || 10, caption: $("caption").value, privacy: $("privacy").value,
          yt_session: store.get("yt"), gc_email: $("gc_email").value.trim(), gc_password: $("gc_password").value,
        }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The reel couldn't be started. Check the details above.");
      $("gc_password").value = "";
      location.hash = "job=" + data.id;
      watch(data.id);
    } catch (ex) {
      err.textContent = ex.message || "The service couldn't be reached. Try again in a minute.";
      if (/YouTube/.test(err.textContent)) { store.del("yt"); $("yt-status").textContent = ""; }
    } finally {
      $("submit").disabled = false;
      $("submit").textContent = "Make my highlight reel";
    }
  });

  // ---- progress
  const NAMES = { smart: "Smart Highlights", top: "Top plays", full: "Full clips" };
  let timer = null;

  function render(j) {
    $("form").hidden = true;
    $("job").hidden = false;
    $("job-title").textContent = j.status === "done" ? "Your reel is on YouTube"
      : j.status === "failed" ? "The reel couldn't be finished" : "Making " + (j.player_name || "your") + "'s reel";
    $("job-stage").textContent = j.status === "queued"
      ? `Waiting for its turn (number ${j.position || 1} in line)` : j.stage || "";
    const items = [...document.querySelectorAll("#track li")];
    const at = items.findIndex((li) => li.dataset.k === j.stage);
    items.forEach((li, i) => {
      li.className = j.status === "done" || (at >= 0 && i < at) ? "done" : i === at ? "now" : "";
    });
    $("code-form").hidden = !j.needs_code;
    $("results").innerHTML = "";
    for (const res of j.results || []) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = res.url; a.textContent = (NAMES[res.reel] || "Reel") + ": " + res.url; a.target = "_blank"; a.rel = "noopener";
      li.append(a); $("results").append(li);
    }
    $("log").textContent = (j.log || []).join("\n");
    const over = j.status === "done" || j.status === "failed";
    $("again").hidden = !over;
    $("job-wait").hidden = over;
    if (j.status === "done") $("job-stage").textContent = "Here are the links:";
  }

  async function poll(id) {
    try {
      const r = await fetch(API + "/api/jobs/" + encodeURIComponent(id));
      if (r.status === 404) { $("job").hidden = false; $("job-title").textContent = "That reel isn't here any more"; $("job-stage").textContent = "Progress pages are kept for 7 days."; return; }
      const j = await r.json();
      render(j);
      if (j.status === "done" || j.status === "failed") return;
    } catch { $("job-stage").textContent = "Can't reach the service right now - still trying…"; }
    timer = setTimeout(() => poll(id), 4000);
  }

  function watch(id) { clearTimeout(timer); $("maker").scrollIntoView({ behavior: "smooth" }); poll(id); }

  $("code-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = new URLSearchParams(location.hash.slice(1)).get("job");
    const r = await fetch(API + "/api/jobs/" + encodeURIComponent(id) + "/code", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: $("code").value.trim() }),
    });
    if (r.ok) { $("code").value = ""; $("code-form").hidden = true; }
  });

  $("again").addEventListener("click", () => {
    clearTimeout(timer);
    history.replaceState(null, "", location.pathname + "#maker");
    $("job").hidden = true; $("form").hidden = false;
  });

  const jobId = hash.get("job");
  if (jobId && API) watch(jobId);
})();
