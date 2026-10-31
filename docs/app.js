(() => {
  const $ = (id) => document.getElementById(id);
  const trial = Number(new URLSearchParams(location.search).get("limit")) || 0;     // ?limit=4 makes a short trial reel
  const ORDER = ["Reading the team schedule", "Finding the player's clips", "Downloading and cutting clips", "Joining the reel"];
  let dismissed = false;
  let timer = null;
  let menus = "";           // ready, signin, loading, closed or failed
  let askingTeams = false;
  let pageReady = false;
  let lastPlayer = "";

  async function post(path, body) {
    const r = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(typeof data.detail === "string" ? data.detail : "That didn't work. Please try again.");
    }
  }

  // ---- the team and player menus
  function option(value, text) {
    const o = document.createElement("option");
    o.value = value; o.textContent = text;
    return o;
  }

  function fillTeams(teams, last) {
    const sel = $("team");
    sel.replaceChildren(option("", "Choose a team"));
    $("who").disabled = true;
    $("who").replaceChildren(option("", "Choose a team first"));
    const years = new Map();
    for (const t of teams) {
      const label = t.year || "Other";
      if (!years.has(label)) {
        const g = document.createElement("optgroup");
        g.label = label;
        years.set(label, g);
        sel.append(g);
      }
      years.get(label).append(option(t.id, t.season ? t.name + " · " + t.season : t.name));
    }
    $("menus").hidden = false;
    lastPlayer = (last && last.player_id) || "";
    if (last && teams.some((t) => t.id === last.team_id)) {
      sel.value = last.team_id;
      loadPlayers();
    }
  }

  async function loadPlayers() {
    const team = $("team").value;
    const who = $("who");
    $("menu-error").textContent = "";
    who.disabled = true;
    who.replaceChildren(option("", team ? "Loading players…" : "Choose a team first"));
    updateMake();
    if (!team) return;
    try {
      const r = await fetch("api/teams/" + encodeURIComponent(team) + "/players");
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Couldn't load that team's players.");
      if ($("team").value !== team) return;       // a different team was chosen in the meantime
      who.replaceChildren(option("", data.players.length ? "Choose a player" : "No players on this team"));
      for (const p of data.players) who.append(option(p.id, p.number ? p.name + " · #" + p.number : p.name));
      who.disabled = !data.players.length;
      if (data.players.some((p) => p.id === lastPlayer)) who.value = lastPlayer;
    } catch (ex) {
      if ($("team").value !== team) return;
      who.replaceChildren(option("", "Couldn't load players"));
      $("menu-error").textContent = ex.message;
    }
    updateMake();
  }

  function updateMake() {
    const usingPage = menus === "failed" && pageReady;
    $("make").disabled = !(usingPage || (menus === "ready" && $("team").value && $("who").value));
  }

  async function checkTeams() {
    if (askingTeams) return;
    askingTeams = true;
    try {
      const data = await (await fetch("api/teams")).json();
      menus = data.state;
      if (menus === "ready") fillTeams(data.teams, data.last);
    } catch { /* tried again on the next refresh */ }
    askingTeams = false;
  }

  function showPlayer(s) {
    if (s.chrome && s.opened && menus !== "ready" && menus !== "failed") checkTeams();
    const p = s.player;
    pageReady = Boolean(p && !p.reading && !p.signed_out);
    const onPage = menus === "failed" && pageReady;
    const nameKnown = onPage && Boolean(p.name);
    $("player").hidden = !nameKnown;
    $("name-row").hidden = !onPage || nameKnown;
    if (nameKnown) {
      $("player-name").textContent = p.name;
      $("player-team").textContent = [p.team, p.season].filter(Boolean).join(" · ");
      $("player-clips").textContent = p.clips ? p.clips + " clips" : "";
    }
    updateMake();
    const st = $("look");
    st.className = "status";
    if (!s.chrome && s.opened) {
      st.textContent = menus === "ready" ? "" : "The GameChanger window was closed. Open it again to continue.";
    } else if (menus === "ready" || !s.opened) {
      st.textContent = "";
    } else if (menus === "failed") {
      st.textContent = onPage ? "" : "Your teams didn't load. Go to your player's page in the GameChanger window instead.";
    } else if (menus === "signin") {
      st.textContent = "Sign in to GameChanger in the window that opened.";
    } else {
      st.textContent = "Loading your teams…";
    }
    $("open").textContent = s.chrome && s.opened ? "Open GameChanger again" : "Open GameChanger";
    $("open").className = "btn " + (s.chrome && s.opened ? "ghost" : "go");
  }

  function showJob(j) {
    const over = j.status !== "running";
    $("start").hidden = true;
    $("job").hidden = false;
    $("job-title").textContent = j.status === "done" ? j.player + "'s reel is ready"
      : j.status === "failed" ? "The reel couldn't be finished" : "Making " + j.player + "'s reel";
    $("job-stage").textContent = j.status === "done"
      ? "Saved on this computer" + (j.length ? " · " + j.length : "") : j.stage;
    $("job-stage").className = "stage" + (j.status === "failed" ? " error" : "");
    const at = ORDER.indexOf(j.stage);
    document.querySelectorAll("#track li").forEach((li, i) => {
      li.className = j.status === "done" || (at >= 0 && i < at) ? "done" : i === at ? "now" : "";
    });
    $("track").hidden = j.status === "failed";
    const counting = !over && j.total > 0;
    $("count").hidden = !counting;
    if (counting) {
      $("bar").max = j.total; $("bar").value = j.done;
      $("count-text").textContent = j.done + " of " + j.total + " clips";
    }
    $("job-wait").hidden = over;
    $("done").hidden = j.status !== "done";
    $("unsure").hidden = !(j.status === "done" && j.unsure);
    $("unsure").textContent = j.unsure + (j.unsure === 1 ? " clip was" : " clips were")
      + " harder to cut and may show the wrong moment. They're listed in “check these clips.txt” next to the reel.";
    $("log").textContent = (j.log || []).join("\n");
    $("again").hidden = !over;
  }

  function showStart() { $("start").hidden = false; $("job").hidden = true; }

  async function refresh() {
    clearTimeout(timer);
    try {
      const s = await (await fetch("api/status")).json();
      $("offline").hidden = true;
      $("maker").hidden = false;
      if (s.job && !(dismissed && s.job.status !== "running")) showJob(s.job);
      else showStart();
      showPlayer(s);
    } catch {
      $("offline").hidden = false;
      $("maker").hidden = true;
    }
    timer = setTimeout(refresh, 2000);
  }

  $("open").addEventListener("click", async () => {
    $("error").textContent = "";
    $("open").disabled = true;
    menus = "";
    $("menus").hidden = true;
    try { await post("api/open"); } catch (ex) { $("error").textContent = ex.message; }
    $("open").disabled = false;
    refresh();
  });

  $("make").addEventListener("click", async () => {
    $("error").textContent = "";
    $("make").disabled = true;
    try {
      await post("api/reels", menus === "ready"
        ? { team_id: $("team").value, player_id: $("who").value, limit: trial }
        : { player_name: $("name-row").hidden ? "" : $("typed-name").value.trim(), limit: trial });
      dismissed = false;
    } catch (ex) { $("error").textContent = ex.message; }
    refresh();
  });

  $("team").addEventListener("change", loadPlayers);
  $("who").addEventListener("change", updateMake);
  $("watch").addEventListener("click", () => post("api/reel/open").catch((ex) => { $("unsure").hidden = false; $("unsure").textContent = ex.message; }));
  $("show").addEventListener("click", () => post("api/reel/show").catch((ex) => { $("unsure").hidden = false; $("unsure").textContent = ex.message; }));
  $("again").addEventListener("click", () => { dismissed = true; refresh(); });

  refresh();
})();
