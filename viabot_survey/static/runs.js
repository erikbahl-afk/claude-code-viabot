/* Runs list + per-run report. Same constraints as app.js: no build step,
 * no libraries, must work in a captive-portal webview. */

(function () {
  "use strict";

  var el = function (id) { return document.getElementById(id); };

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function iso(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString([], { hour12: false });
  }

  function clockTime(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
  }

  function duration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    seconds = Math.max(0, Math.round(seconds));
    var m = Math.floor(seconds / 60), s = seconds % 60;
    return m + "m " + (s < 10 ? "0" : "") + s + "s";
  }

  function api(path, options) {
    return fetch(path, options || {}).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) throw new Error(body.error || ("HTTP " + response.status));
        return body;
      });
    });
  }

  function loadRuns() {
    api("/api/runs").then(function (data) {
      if (!data.runs.length) {
        el("runsList").innerHTML = '<p class="muted small">No runs recorded yet.</p>';
        return;
      }
      var html = '<ul class="list">';
      data.runs.forEach(function (run) {
        var live = run.id === data.active;
        html += '<li><span class="what">' +
                '<strong class="mono">' + escapeHtml(run.id) + "</strong>" +
                (live ? ' <span class="pill running">live</span>' : "") +
                '<div class="small muted">' + iso(run.started_at) +
                " · " + run.sample_count + " samples · " + run.mark_count + " marks" +
                (run.label ? " · " + escapeHtml(run.label) : "") + "</div></span>" +
                '<button class="btn small" data-report="' + escapeHtml(run.id) + '">Report</button>' +
                "</li>";
      });
      html += "</ul>";
      el("runsList").innerHTML = html;
    }).catch(function (error) {
      el("runsList").innerHTML = '<p class="err small">' + escapeHtml(error.message) + "</p>";
    });
  }

  function loadReport(runId) {
    el("reportCard").hidden = false;
    el("reportTitle").textContent = "Report — " + runId;
    el("reportBody").innerHTML = '<p class="muted small">Loading…</p>';
    api("/api/runs/" + encodeURIComponent(runId) + "/report").then(function (report) {
      el("reportBody").innerHTML = renderReport(runId, report);
    }).catch(function (error) {
      el("reportBody").innerHTML = '<p class="err small">' + escapeHtml(error.message) + "</p>";
    });
  }

  function renderReport(runId, report) {
    var html = '<dl class="kv">';
    html += "<dt>Duration</dt><dd>" + duration(report.duration_s) + "</dd>";
    html += "<dt>Samples</dt><dd>" + report.sample_count + "</dd>";
    html += "<dt>RTT min/avg/max</dt><dd>" +
            [report.rtt_ms.min, report.rtt_ms.avg, report.rtt_ms.max]
              .map(function (v) { return v === null ? "—" : v; }).join(" / ") + " ms</dd>";
    Object.keys(report.status_pct).forEach(function (key) {
      html += "<dt>Time " + escapeHtml(key) + "</dt><dd>" + report.status_pct[key] + "%</dd>";
    });
    html += "</dl>";

    html += "<h2 style=\"margin-top:16px\">Problem areas</h2>";
    if (!report.problem_areas.length) {
      html += '<p class="small muted">None — the link held up for the whole walk.</p>';
    } else {
      html += '<ul class="list">';
      report.problem_areas.forEach(function (area) {
        // The video pointer is the payload: this is what you open to decide
        // whether the dead spot sits somewhere the robot actually drives.
        var pointer = area.video_file
          ? escapeHtml(area.video_file) + " @ " + area.video_offset_s + "s"
          : "no video";
        html += '<li><span class="what"><span class="pill ' + area.worst_status + '">' +
                area.worst_status + "</span> " +
                escapeHtml(area.start_iso.split("T")[1]) + " for " +
                duration(area.duration_s) +
                '<div class="small muted mono">' + pointer + "</div>" +
                (area.marks.length
                  ? '<div class="small">marked: ' +
                    area.marks.map(function (m) {
                      return escapeHtml(m.category || "mark") +
                             (m.note ? " (" + escapeHtml(m.note) + ")" : "");
                    }).join(", ") + "</div>"
                  : "") +
                "</span></li>";
      });
      html += "</ul>";
    }

    html += "<h2 style=\"margin-top:16px\">Marks</h2>";
    if (!report.marks.length) {
      html += '<p class="small muted">No marks recorded.</p>';
    } else {
      html += '<ul class="list">';
      report.marks.forEach(function (mark) {
        html += '<li><span class="when">' + clockTime(mark.ts) + "</span>" +
                '<span class="what">' + escapeHtml(mark.category || "mark") +
                (mark.note ? " — " + escapeHtml(mark.note) : "") +
                (mark.video_file
                  ? '<div class="small muted mono">' + escapeHtml(mark.video_file) +
                    " @ " + mark.video_offset_s + "s</div>"
                  : "") +
                '</span><span class="pill ' + (mark.status || "unknown") + '">' +
                escapeHtml(mark.status || "?") + "</span></li>";
      });
      html += "</ul>";
    }

    var base = "/api/runs/" + encodeURIComponent(runId);
    html += '<div class="btn-row" style="margin-top:16px">' +
            '<a class="btn small" href="' + base + '/samples.csv">Samples CSV</a>' +
            '<a class="btn small" href="' + base + '/marks.csv">Marks CSV</a></div>';
    return html;
  }

  function init() {
    el("runsList").addEventListener("click", function (event) {
      var button = event.target.closest("[data-report]");
      if (button) { loadReport(button.getAttribute("data-report")); }
    });
    loadRuns();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
