/* Dashboard logic.
 *
 * Deliberately plain: no modules, no build step, no external libraries. This
 * page has to run inside iOS/Android captive-portal webviews, which have no
 * internet access and are fussier than a real browser. Short polling with
 * fetch() works there; long-lived connections often do not. */

(function () {
  "use strict";

  var STATUS_TEXT = {
    good: "GOOD",
    degraded: "DEGRADED",
    bad: "BAD",
    dead: "DEAD ZONE",
    unknown: "NO DATA"
  };
  var STATUS_COLORS = {
    good: "#22c55e", degraded: "#eab308", bad: "#f97316",
    dead: "#ef4444", unknown: "#64748b"
  };
  var HISTORY_SECONDS = 180;

  var el = function (id) { return document.getElementById(id); };
  var selectedCategory = "";
  var consecutiveFailures = 0;
  var lastStatus = null;
  var updateState = null;

  // ---- helpers ------------------------------------------------------------

  function fmt(value, digits, suffix) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "number") {
      return value.toFixed(digits === undefined ? 0 : digits) + (suffix || "");
    }
    return String(value) + (suffix || "");
  }

  function duration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    seconds = Math.max(0, Math.round(seconds));
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    var s = seconds % 60;
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return (h > 0 ? h + ":" + pad(m) : m) + ":" + pad(s);
  }

  function clockTime(epochSeconds) {
    if (!epochSeconds) return "—";
    var d = new Date(epochSeconds * 1000);
    return d.toLocaleTimeString([], { hour12: false });
  }

  function toast(message, isError) {
    var node = el("toast");
    node.textContent = message;
    node.className = "show" + (isError ? " error" : "");
    clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.className = ""; }, 2800);
  }

  function api(path, options) {
    return fetch(path, options || {}).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          throw new Error(body.error || ("HTTP " + response.status));
        }
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

  // ---- rendering ----------------------------------------------------------

  function renderBanner(sample, workers) {
    var status = (sample && sample.status) || "unknown";
    el("banner").className = "banner " + status;
    el("statusLabel").textContent = STATUS_TEXT[status] || status.toUpperCase();

    var detail;
    if (status === "dead") {
      detail = "No connectivity for " + fmt(sample.dead_streak_s, 0, " s");
    } else if (status === "unknown") {
      var ping = workers && workers.ping;
      detail = ping && ping.error ? ping.error : "Waiting for the first measurement";
    } else {
      detail = "Jitter " + fmt(sample.jitter_ms, 1, " ms") +
               " · DNS " + fmt(sample.dns_ms, 0, " ms");
    }
    el("statusDetail").textContent = detail;

    el("mRtt").textContent = fmt(sample.rtt_ms, 0);
    el("mLoss").textContent = fmt(sample.loss_pct, 0);

    // Prefer a real modem metric when the router client is configured;
    // otherwise show jitter so the third tile is never dead space.
    if (sample.rsrp !== null && sample.rsrp !== undefined) {
      el("mSignalKey").textContent = "RSRP dBm";
      el("mSignal").textContent = fmt(sample.rsrp, 0);
    } else if (sample.sinr !== null && sample.sinr !== undefined) {
      el("mSignalKey").textContent = "SINR dB";
      el("mSignal").textContent = fmt(sample.sinr, 0);
    } else {
      el("mSignalKey").textContent = "Jitter ms";
      el("mSignal").textContent = fmt(sample.jitter_ms, 0);
    }

    if (status === "dead" && lastStatus !== "dead") {
      if (navigator.vibrate) { navigator.vibrate([120, 60, 120]); }
    }
    lastStatus = status;
  }

  function renderRun(run) {
    var active = !!run;
    el("runIdle").hidden = active;
    el("runActive").hidden = !active;
    el("markCard").hidden = !active;
    if (!active) return;

    el("runId").textContent = run.id;
    el("runElapsed").textContent = duration(run.duration_s);
    el("runMarks").textContent = run.mark_count;
    el("runDead").textContent = fmt(run.dead_seconds, 0, " s");
    el("runData").textContent = fmt(run.data_used_mb, 1, " MB");
  }

  function renderWorkers(workers) {
    var list = el("workerList");
    var html = "";
    Object.keys(workers).forEach(function (name) {
      var worker = workers[name];
      var detail = worker.error ? worker.error : workerDetail(name, worker);
      html += "<dt>" + name + "</dt><dd>" +
              '<span class="pill ' + worker.state + '">' + worker.state + "</span>" +
              (detail ? '<div class="small muted">' + escapeHtml(detail) + "</div>" : "") +
              "</dd>";
    });
    list.innerHTML = html;
  }

  function workerDetail(name, worker) {
    if (name === "ping") {
      return worker.target + " · " + fmt(worker.avg_rtt_ms, 0, " ms avg");
    }
    if (name === "camera") {
      if (!worker.enabled) return "disabled in config";
      return (worker.recording ? "recording · " : "idle · ") +
             worker.resolution + " · " + worker.segments + " segments";
    }
    if (name === "iperf3") {
      return worker.enabled ? "server " + worker.server : "no server configured";
    }
    if (name === "router") {
      return worker.enabled ? "client " + worker.client : "not configured";
    }
    if (name === "dns") {
      return worker.hostname + " · " + fmt(worker.resolve_ms, 0, " ms");
    }
    return "";
  }

  function renderSystem(system) {
    if (!system) return;
    var rows = [
      ["Hostname", system.hostname],
      ["Clock synced", system.clock_synced === null ? "unknown" :
        (system.clock_synced ? "yes" : "NO — timestamps unreliable")],
      ["Uplink " + system.uplink.interface,
        (system.uplink.up ? "up" : "DOWN") + " · " + (system.uplink.address || "no address")],
      ["Wi-Fi " + system.ap.interface,
        (system.ap.address || "—") + " · " + fmt(system.ap.clients, 0) + " client(s)"],
      ["Default route", system.default_route_dev || "none"],
      ["CPU temp", fmt(system.cpu_temp_c, 1, " °C")],
      ["Uptime", duration(system.uptime_s)]
    ];
    if (system.disk) {
      rows.push(["Disk free", fmt(system.disk.free_mb / 1000, 1, " GB") +
                 " (" + fmt(system.disk.used_pct, 0, "% used") + ")"]);
    }
    var html = "";
    rows.forEach(function (row) {
      var warn = /NO —|DOWN|none/.test(String(row[1])) ? ' class="err"' : "";
      html += "<dt>" + escapeHtml(row[0]) + "</dt><dd" + warn + ">" +
              escapeHtml(String(row[1])) + "</dd>";
    });
    el("sysList").innerHTML = html;
  }

  function renderMarks(marks) {
    var html = "";
    marks.slice().reverse().slice(0, 12).forEach(function (mark) {
      html += '<li><span class="when">' + clockTime(mark.ts) + "</span>" +
              '<span class="what">' + escapeHtml(mark.category || "mark") +
              (mark.note ? " — " + escapeHtml(mark.note) : "") + "</span>" +
              '<span class="pill ' + (mark.status || "unknown") + '">' +
              (mark.status || "?") + "</span></li>";
    });
    el("markList").innerHTML = html;
  }

  function renderEvents(events) {
    var html = "";
    events.slice(0, 15).forEach(function (event) {
      var cls = event.level === "error" ? "err" : (event.level === "warning" ? "warn" : "");
      html += '<li><span class="when">' + clockTime(event.ts) + "</span>" +
              '<span class="what ' + cls + '">' + escapeHtml(event.message) + "</span></li>";
    });
    el("eventList").innerHTML = html || '<li class="muted">Nothing logged yet.</li>';
  }

  function renderUpdate(state) {
    updateState = state;
    var rows = [
      ["Installed", state.local_describe || state.local_commit_short || "unknown"],
      ["Tracking", state.remote + "/" + state.branch],
      ["Last checked", state.last_fetch ? clockTime(state.last_fetch) : "never"]
    ];
    if (state.update_available) {
      rows.push(["Available", state.pending_commits.length + " new commit(s)"]);
    }
    if (state.dirty) {
      rows.push(["Warning", "local edits present — an update will discard them"]);
    }
    if (state.last_error) { rows.push(["Error", state.last_error]); }

    var html = "";
    rows.forEach(function (row) {
      html += "<dt>" + escapeHtml(row[0]) + "</dt><dd>" + escapeHtml(String(row[1])) + "</dd>";
    });
    if (state.update_available) {
      html += '<dt>Changes</dt><dd class="small mono">' +
              state.pending_commits.map(escapeHtml).join("<br>") + "</dd>";
    }
    el("updateList").innerHTML = html;
    el("btnApplyUpdate").disabled = !state.update_available;
    el("updateHint").textContent = state.update_available
      ? "Applying restarts the rig software. Stop any run first."
      : "Merge the pull request on GitHub first, then check here.";
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  // ---- sparkline ----------------------------------------------------------

  function drawChart(samples) {
    var canvas = el("spark");
    var ratio = window.devicePixelRatio || 1;
    var width = canvas.clientWidth || 700;
    var height = 96;
    if (canvas.width !== Math.round(width * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }
    var ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!samples.length) {
      ctx.fillStyle = "#64748b";
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillText("waiting for samples…", 8, height / 2);
      return;
    }

    var now = samples[samples.length - 1].ts;
    var span = HISTORY_SECONDS;
    var rtts = samples.map(function (s) { return s.rtt_ms; })
                      .filter(function (v) { return v !== null && v !== undefined; });
    // Clamp the scale so a single 2-second outlier does not flatten the trace.
    var maxRtt = Math.max(120, Math.min(1000, rtts.length ? Math.max.apply(null, rtts) * 1.25 : 200));

    var x = function (ts) { return width * (1 - (now - ts) / span); };
    var y = function (rtt) { return height - 6 - (Math.min(rtt, maxRtt) / maxRtt) * (height - 14); };

    // Status bands along the bottom: this is what you actually read while
    // walking — a solid red strip means you just crossed a dead zone.
    samples.forEach(function (sample, index) {
      var next = samples[index + 1];
      var x0 = x(sample.ts);
      var x1 = next ? x(next.ts) : x0 + width / span;
      ctx.fillStyle = STATUS_COLORS[sample.status] || STATUS_COLORS.unknown;
      ctx.globalAlpha = 0.85;
      ctx.fillRect(x0, height - 5, Math.max(1, x1 - x0), 5);
    });
    ctx.globalAlpha = 1;

    ctx.strokeStyle = "#2a3643";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, y(maxRtt / 2));
    ctx.lineTo(width, y(maxRtt / 2));
    ctx.stroke();

    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2;
    ctx.beginPath();
    var drawing = false;
    samples.forEach(function (sample) {
      if (sample.rtt_ms === null || sample.rtt_ms === undefined) {
        drawing = false;   // break the line across a gap rather than bridging it
        return;
      }
      var px = x(sample.ts), py = y(sample.rtt_ms);
      if (drawing) { ctx.lineTo(px, py); } else { ctx.moveTo(px, py); drawing = true; }
    });
    ctx.stroke();

    ctx.fillStyle = "#93a4b5";
    ctx.font = "10px system-ui, sans-serif";
    ctx.fillText(Math.round(maxRtt) + " ms", 4, 12);
  }

  // ---- polling ------------------------------------------------------------

  function pollStatus() {
    api("/api/status").then(function (data) {
      consecutiveFailures = 0;
      el("offline").hidden = true;
      renderBanner(data.sample || {}, data.workers || {});
      renderRun(data.run);
      renderWorkers(data.workers || {});
      renderSystem(data.system);
    }).catch(function () {
      consecutiveFailures += 1;
      // One dropped poll is normal on a rig that is, by design, walking into
      // dead zones. Only shout after several in a row.
      if (consecutiveFailures >= 3) { el("offline").hidden = false; }
    });
  }

  function pollHistory() {
    api("/api/history?seconds=" + HISTORY_SECONDS).then(function (data) {
      drawChart(data.samples || []);
    }).catch(function () { /* the status poll already reports connectivity */ });
  }

  function pollSlow() {
    api("/api/marks").then(function (data) { renderMarks(data.marks || []); })
                     .catch(function () {});
    api("/api/events?limit=20").then(function (data) { renderEvents(data.events || []); })
                               .catch(function () {});
  }

  // ---- actions ------------------------------------------------------------

  function startRun() {
    el("btnStart").disabled = true;
    post("/api/run/start", { label: el("runLabel").value })
      .then(function (data) { toast("Run " + data.run.id + " started"); pollStatus(); })
      .catch(function (error) { toast(error.message, true); })
      .then(function () { el("btnStart").disabled = false; });
  }

  function stopRun() {
    if (!window.confirm("Stop the run and end recording?")) return;
    el("btnStop").disabled = true;
    post("/api/run/stop")
      .then(function () { toast("Run stopped"); pollStatus(); pollSlow(); })
      .catch(function (error) { toast(error.message, true); })
      .then(function () { el("btnStop").disabled = false; });
  }

  function addMark() {
    post("/api/mark", { category: selectedCategory, note: el("markNote").value })
      .then(function (data) {
        toast("Marked " + (data.mark.category || "spot") + " at " + clockTime(data.mark.ts));
        if (navigator.vibrate) { navigator.vibrate(40); }
        el("markNote").value = "";
        pollSlow();
        pollStatus();
      })
      .catch(function (error) { toast(error.message, true); });
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
    post("/api/update/apply")
      .then(function () {
        toast("Updating — the rig will restart in a moment");
        waitForRestart();
      })
      .catch(function (error) {
        toast(error.message, true);
        el("btnApplyUpdate").disabled = false;
      });
  }

  function waitForRestart() {
    // The service is being replaced underneath us; poll until it answers again.
    var attempts = 0;
    var timer = setInterval(function () {
      attempts += 1;
      api("/api/health").then(function () {
        clearInterval(timer);
        toast("Update applied — reloading");
        setTimeout(function () { window.location.reload(); }, 800);
      }).catch(function () {
        if (attempts > 60) {
          clearInterval(timer);
          toast("Rig did not come back. Check it over SSH.", true);
        }
      });
    }, 2000);
  }

  function runIperf() {
    post("/api/iperf/test")
      .then(function () { toast("Throughput test queued"); })
      .catch(function (error) { toast(error.message, true); });
  }

  // ---- wiring -------------------------------------------------------------

  function init() {
    el("btnStart").addEventListener("click", startRun);
    el("btnStop").addEventListener("click", stopRun);
    el("btnMark").addEventListener("click", addMark);
    el("btnIperf").addEventListener("click", runIperf);
    el("btnCheckUpdate").addEventListener("click", checkUpdate);
    el("btnApplyUpdate").addEventListener("click", applyUpdate);

    el("chips").addEventListener("click", function (event) {
      var chip = event.target.closest(".chip");
      if (!chip) return;
      var category = chip.getAttribute("data-category");
      var alreadyOn = chip.classList.contains("active");
      Array.prototype.forEach.call(el("chips").children, function (node) {
        node.classList.remove("active");
      });
      if (alreadyOn) {
        selectedCategory = "";
      } else {
        chip.classList.add("active");
        selectedCategory = category;
      }
    });

    el("runLabel").addEventListener("keydown", function (event) {
      if (event.key === "Enter") { startRun(); }
    });

    pollStatus();
    pollHistory();
    pollSlow();
    api("/api/update/status").then(renderUpdate).catch(function () {});

    setInterval(pollStatus, 1000);
    setInterval(pollHistory, 3000);
    setInterval(pollSlow, 5000);

    // Phones aggressively suspend background tabs; refresh on return so the
    // operator never acts on a frozen reading.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { pollStatus(); pollHistory(); pollSlow(); }
    });
    window.addEventListener("resize", function () { pollHistory(); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
