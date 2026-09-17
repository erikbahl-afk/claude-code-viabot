/* The rig's controller.
 *
 * Deliberately plain: no modules, no build step, no external libraries. This
 * page has to run inside iOS/Android captive-portal webviews, which have no
 * internet access and are fussier than a real browser. Short polling with
 * fetch() works there; long-lived connections often do not.
 *
 * The phone holds no state worth losing. Everything — measuring, recording,
 * analysis — happens on the Pi, so locking the phone, switching apps or
 * closing the page does not interrupt a survey, and reopening it shows the
 * true current state rather than a stale local copy. */

(function () {
  "use strict";

  var STATUS_TEXT = {
    good: "GOOD", degraded: "DEGRADED", bad: "BAD",
    dead: "DEAD ZONE", unknown: "NO DATA"
  };
  var STATUS_COLORS = {
    good: "#22c55e", degraded: "#eab308", bad: "#f97316",
    dead: "#ef4444", unknown: "#64748b"
  };
  var HEALTH_ORDER = ["camera", "link", "power", "disk", "clock"];
  var HEALTH_NAMES = {
    camera: "Camera", link: "Link", power: "Power", disk: "Disk", clock: "Clock"
  };
  var RIBBON_SECONDS = 120;

  var el = function (id) { return document.getElementById(id); };
  var consecutiveFailures = 0;
  var ribbon = [];
  var lastStatus = null;
  var busy = false;

  // ---- helpers ------------------------------------------------------------

  function clock(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    seconds = Math.max(0, Math.round(seconds));
    var m = Math.floor(seconds / 60), s = seconds % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function timeOfDay(epochSeconds) {
    if (!epochSeconds) return "—";
    return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour12: false });
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function toast(message, isError) {
    var node = el("toast");
    node.textContent = message;
    node.className = "show" + (isError ? " error" : "");
    clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.className = ""; }, 3000);
  }

  function api(path, options) {
    return fetch(path, options || {}).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) { throw new Error(body.error || ("HTTP " + response.status)); }
        return body;
      });
    });
  }

  function post(path, payload) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
  }

  // ---- health -------------------------------------------------------------

  function renderHealth(health) {
    if (!health) return;
    var html = "";
    HEALTH_ORDER.forEach(function (key) {
      var item = health[key] || { state: "unknown", detail: "" };
      html += '<div class="chip ' + item.state + '">' +
              '<span class="n">' + HEALTH_NAMES[key] + "</span>" +
              '<span class="s">' + escapeHtml(item.detail || item.state) + "</span></div>";
    });
    el("health").innerHTML = html;

    // A dead camera is the one failure that silently wastes an entire walk —
    // the connection readings keep looking perfectly healthy. So it is not
    // left to a coloured chip to communicate.
    var failures = HEALTH_ORDER.filter(function (key) {
      return health[key] && health[key].state === "fail";
    });
    if (failures.length) {
      var first = failures[0];
      el("alert").hidden = false;
      el("alertTitle").textContent = HEALTH_NAMES[first] + " problem";
      el("alertDetail").textContent = failures.map(function (key) {
        return HEALTH_NAMES[key] + ": " + health[key].detail;
      }).join(" · ");
    } else {
      el("alert").hidden = true;
    }
  }

  // ---- connection ---------------------------------------------------------

  function renderBanner(sample) {
    var status = (sample && sample.status) || "unknown";
    el("banner").className = "banner " + status;
    el("statusLabel").textContent = STATUS_TEXT[status] || status.toUpperCase();
    el("mRtt").textContent = sample.rtt_ms === null || sample.rtt_ms === undefined
      ? "—" : Math.round(sample.rtt_ms);
    el("mLoss").textContent = sample.loss_pct === null || sample.loss_pct === undefined
      ? "—" : Math.round(sample.loss_pct);

    ribbon.push(status);
    while (ribbon.length > RIBBON_SECONDS) { ribbon.shift(); }
    drawRibbon();

    if (status === "dead" && lastStatus !== "dead" && navigator.vibrate) {
      navigator.vibrate([140, 70, 140]);
    }
    lastStatus = status;
  }

  function drawRibbon() {
    var canvas = el("ribbon");
    var ratio = window.devicePixelRatio || 1;
    var width = canvas.clientWidth || 320;
    var height = 10;
    if (canvas.width !== Math.round(width * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }
    var ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "rgba(0,0,0,.22)";
    ctx.fillRect(0, 0, width, height);

    // Right-aligned so "now" is always at the same edge, however little
    // history has accumulated.
    var slot = width / RIBBON_SECONDS;
    ribbon.forEach(function (status, i) {
      var x = width - (ribbon.length - i) * slot;
      ctx.fillStyle = STATUS_COLORS[status] || STATUS_COLORS.unknown;
      ctx.fillRect(x, 0, Math.max(1, slot + 0.6), height);
    });
  }

  // ---- run panes ----------------------------------------------------------

  function renderRun(data) {
    var run = data.run;
    var wrapup = data.wrapup;

    el("paneIdle").hidden = !!run || !!wrapup;
    el("paneRunning").hidden = !run;
    el("paneWrapup").hidden = !!run || !wrapup;

    if (run) {
      el("runLabelShown").textContent = run.label || run.id;
      var paused = !!data.paused;
      el("runPill").textContent = paused ? "paused" : "walking";
      el("runPill").className = "pill" + (paused ? " paused" : "");
      el("btnPause").textContent = paused ? "RESUME" : "PAUSE";
      el("btnPause").className = "btn " + (paused ? "resume" : "pause");
      el("runWalked").textContent = clock(run.walked_s);
      el("runDead").textContent = run.dead_zone_estimate === undefined
        ? "—" : run.dead_zone_estimate;

      // Live percentage uses the same definition as the final report: seconds
      // classified dead over seconds walked.
      var walked = run.walked_s || 0;
      el("runRunnable").textContent = walked > 0
        ? Math.round(100 * (walked - (run.dead_seconds || 0)) / walked)
        : "—";
    }

    if (wrapup && !run) {
      var s = wrapup.summary || {};
      el("wrapPct").textContent = s.runnable_pct === null || s.runnable_pct === undefined
        ? "—" : s.runnable_pct;
      el("wrapZones").textContent = s.dead_zone_count;
      el("wrapLongest").textContent = s.longest_dead_zone_s
        ? s.longest_dead_zone_s + " s" : "none";
      el("wrapWalked").textContent = clock(s.walked_s);
      el("wrapClips").textContent = s.clip_dir || "no clips cut";
    }
  }

  function renderSystem(system) {
    if (!system) return;
    var rows = [
      ["Hostname", system.hostname],
      ["Uplink " + system.uplink.interface,
        (system.uplink.up ? "up" : "DOWN") + " · " + (system.uplink.address || "no address")],
      ["Wi-Fi clients", system.ap.clients === null ? "—" : system.ap.clients],
      ["CPU temp", system.cpu_temp_c === null ? "—" : system.cpu_temp_c + " °C"],
      ["Uptime", clock(system.uptime_s)]
    ];
    if (system.timezone_info) {
      rows.push(["Timezone", (system.timezone_info.configured ||
                              system.timezone_info.name) +
                             " (" + system.timezone_info.utc_offset + ")"]);
    }
    var html = "";
    rows.forEach(function (row) {
      var bad = /DOWN|no address/.test(String(row[1])) ? ' class="err"' : "";
      html += "<dt>" + escapeHtml(row[0]) + "</dt><dd" + bad + ">" +
              escapeHtml(String(row[1])) + "</dd>";
    });
    el("sysList").innerHTML = html;
  }

  function renderEvents(events) {
    var html = "";
    events.slice(0, 10).forEach(function (event) {
      var cls = event.level === "error" ? "err" : (event.level === "warning" ? "warn" : "");
      html += '<li><span class="when">' + timeOfDay(event.ts) + "</span>" +
              '<span class="what ' + cls + '">' + escapeHtml(event.message) + "</span></li>";
    });
    el("eventList").innerHTML = html || '<li class="what">Nothing logged yet.</li>';
  }

  function renderUpdate(state) {
    el("btnApplyUpdate").disabled = !state.update_available;
    el("updateHint").textContent = state.update_available
      ? state.pending_commits.length + " new commit(s). Stop any run first."
      : "Up to date at " + (state.local_commit_short || "unknown") + ".";
  }

  // ---- polling ------------------------------------------------------------

  function pollStatus() {
    return api("/api/status").then(function (data) {
      consecutiveFailures = 0;
      el("offline").hidden = true;
      renderHealth(data.health);
      renderBanner(data.sample || {});
      renderRun(data);
      renderSystem(data.system);
      el("provisionalNote").hidden = !data.thresholds_provisional;
    }).catch(function () {
      consecutiveFailures += 1;
      // One dropped poll is normal on a rig that is, by design, walking into
      // dead zones. Only shout after several in a row.
      if (consecutiveFailures >= 4) { el("offline").hidden = false; }
    });
  }

  function pollSlow() {
    api("/api/events?limit=12").then(function (data) {
      renderEvents(data.events || []);
    }).catch(function () {});
  }

  // Drawn once at load and never again, the update panel would go on claiming
  // an update was available long after one was applied — from this phone or
  // any other. Slower than the rest: each call shells out to git.
  function pollUpdate() {
    api("/api/update/status").then(renderUpdate).catch(function () {});
  }

  // ---- actions ------------------------------------------------------------

  function guard(button, work) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    work().catch(function (error) { toast(error.message, true); })
          .then(function () { busy = false; button.disabled = false; updateStartState(); });
  }

  function updateStartState() {
    var value = el("runLabel").value.trim();
    el("btnStart").disabled = value.length === 0 || busy;
    el("labelHint").textContent = value.length === 0
      ? "Name the location before starting."
      : "Ready.";
  }

  function startRun() {
    guard(el("btnStart"), function () {
      return post("/api/run/start", { label: el("runLabel").value.trim() })
        .then(function () {
          el("runLabel").value = "";
          toast("Survey started");
          return pollStatus();
        });
    });
  }

  function togglePause() {
    var resuming = el("btnPause").textContent === "RESUME";
    guard(el("btnPause"), function () {
      return post(resuming ? "/api/run/resume" : "/api/run/pause")
        .then(function () {
          toast(resuming ? "Resumed" : "Paused — nothing is being recorded");
          return pollStatus();
        });
    });
  }

  function endRun() {
    if (!window.confirm("End the survey and work out the dead zones?")) return;
    guard(el("btnEnd"), function () {
      toast("Finishing — cutting clips…");
      return post("/api/run/stop").then(function () {
        return pollStatus().then(pollSlow);
      });
    });
  }

  function dismissWrapup() {
    guard(el("btnDone"), function () {
      return post("/api/run/wrapup/dismiss").then(pollStatus);
    });
  }

  function checkUpdate() {
    var button = el("btnCheckUpdate");
    button.disabled = true;
    button.textContent = "Checking…";
    post("/api/update/check")
      .then(function (state) {
        renderUpdate(state);
        toast(state.update_available ? "Update available" : "Already up to date");
      })
      .catch(function (error) { toast(error.message, true); })
      .then(function () {
        button.disabled = false;
        button.textContent = "Check for updates";
      });
  }

  function applyUpdate() {
    if (!window.confirm("Apply the update and restart the rig software?")) return;
    el("btnApplyUpdate").disabled = true;
    // Note which process is answering *before* asking for the update. The
    // update fetches and installs before it restarts anything, and the old
    // process keeps answering health checks throughout — so "the rig replied"
    // is not evidence it restarted. Without this the page reloaded about two
    // seconds in, against the old process, still on the old commit, still
    // showing the update as available.
    api("/api/health").then(function (health) {
      return post("/api/update/apply").then(function () {
        toast("Updating — this can take a minute over cellular");
        waitForRestart(health.started_at);
      });
    }).catch(function (error) {
      toast(error.message, true);
      el("btnApplyUpdate").disabled = false;
    });
  }

  function waitForRestart(previousStart) {
    var attempts = 0;
    var timer = setInterval(function () {
      attempts += 1;
      // Five minutes. A cellular fetch and a pip install are both slow, and
      // giving up early on a working update is worse than waiting.
      if (attempts > 150) {
        clearInterval(timer);
        toast("Rig did not come back. Check it over SSH.", true);
        el("btnApplyUpdate").disabled = false;
        return;
      }
      api("/api/health").then(function (health) {
        // A different start time is the only proof the service actually went
        // away and came back. An older rig does not report one, so fall back
        // to the previous behaviour rather than waiting forever.
        if (previousStart && health.started_at === previousStart) return;
        clearInterval(timer);
        window.location.reload();
      }).catch(function () {});
    }, 2000);
  }

  // ---- wiring -------------------------------------------------------------

  function init() {
    el("btnStart").addEventListener("click", startRun);
    el("btnPause").addEventListener("click", togglePause);
    el("btnEnd").addEventListener("click", endRun);
    el("btnDone").addEventListener("click", dismissWrapup);
    el("btnCheckUpdate").addEventListener("click", checkUpdate);
    el("btnApplyUpdate").addEventListener("click", applyUpdate);

    el("runLabel").addEventListener("input", updateStartState);
    el("runLabel").addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !el("btnStart").disabled) { startRun(); }
    });
    updateStartState();

    pollStatus();
    pollSlow();
    pollUpdate();

    setInterval(pollStatus, 1000);
    setInterval(pollSlow, 8000);
    setInterval(pollUpdate, 30000);

    // Phones suspend background tabs aggressively. Refresh on return so the
    // operator never acts on a frozen reading.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { pollStatus(); pollSlow(); pollUpdate(); }
    });
    window.addEventListener("resize", drawRibbon);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
