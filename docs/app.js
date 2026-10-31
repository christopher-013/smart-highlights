(() => {
  const $ = (id) => document.getElementById(id);
  const trial = Number(new URLSearchParams(location.search).get("limit")) || 0;     // ?limit=4 makes a short trial reel
  const ORDER = ["Reading the team schedule", "Finding the player's clips", "Downloading and cutting clips", "Joining the reel"];
  let dismissed = false;
  let timer = null;

  async function post(path, body) {
    const r = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(typeof data.detail === "string" ? data.detail : "That didn't work. Please try again.");
    }
  }

  function showPlayer(s) {
    const p = s.player;
    const ready = Boolean(p && !p.reading && !p.signed_out);
    const nameKnown = ready && Boolean(p.name);
    $("player").hidden = !nameKnown;
    $("name-row").hidden = !ready || nameKnown;
    if (nameKnown) {
      $("player-name").textContent = p.name;
      $("player-team").textContent = [p.team, p.season].filter(Boolean).join(" · ");
      $("player-clips").textContent = p.clips ? p.clips + " clips" : "";
    }
    $("make").disabled = !ready;
    const st = $("look");
    st.className = "status";
    if (ready) {
      st.textContent = "";
    } else if (p && p.signed_out) {
      st.textContent = "Sign in to GameChanger in the window that opened.";
    } else if (!s.chrome && s.opened) {
      st.textContent = "The GameChanger window was closed. Open it again to continue.";
    } else if (s.chrome && s.opened) {
      st.textContent = "Waiting for you to open your player's page…";
    } else {
      st.textContent = "";
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
    try { await post("api/open"); } catch (ex) { $("error").textContent = ex.message; }
    $("open").disabled = false;
    refresh();
  });

  $("make").addEventListener("click", async () => {
    $("error").textContent = "";
    $("make").disabled = true;
    try {
      await post("api/reels", { player_name: $("name-row").hidden ? "" : $("typed-name").value.trim(), limit: trial });
      dismissed = false;
    } catch (ex) { $("error").textContent = ex.message; }
    refresh();
  });

  $("watch").addEventListener("click", () => post("api/reel/open").catch((ex) => { $("unsure").hidden = false; $("unsure").textContent = ex.message; }));
  $("show").addEventListener("click", () => post("api/reel/show").catch((ex) => { $("unsure").hidden = false; $("unsure").textContent = ex.message; }));
  $("again").addEventListener("click", () => { dismissed = true; refresh(); });

  refresh();
})();
