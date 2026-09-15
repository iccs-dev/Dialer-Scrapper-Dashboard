/* Dashboard front-end.
 *
 * Stage 4 keeps the live updates on a short poll. Stage 5 replaces
 * startLiveUpdates() with a WebSocket - every other function here already
 * takes a state object, so nothing else has to change. The connection pill
 * and refreshAll() are deliberately written now so the WebSocket swap only
 * touches one function (requirement 8).
 */
(function () {
  "use strict";

  var POLL_MS = 3000;
  var $ = function (id) { return document.getElementById(id); };
  var csrf = window.DASHBOARD.csrf;

  /* ---------------- connection indicator ---------------- */
  function setConnection(state) {
    var dot = $("conn-dot"), text = $("conn-text");
    dot.className = "dot " + state;
    text.textContent = state === "live" ? "LIVE"
                     : state === "polling" ? "LIVE (polling)"
                     : "DISCONNECTED";
  }

  /* ---------------- date mode ---------------- */
  function selectedDates() {
    var mode = document.querySelector('input[name=date_mode]:checked').value;
    if (mode === "single") return { mode: mode, single: $("single_date").value };
    if (mode === "range")  return { mode: mode, start: $("start_date").value, end: $("end_date").value };
    return { mode: mode };
  }

  function describeDates() {
    var d = selectedDates(), msg;
    if (d.mode === "yesterday") msg = "Will scrape for: " + window.DASHBOARD.yesterday;
    else if (d.mode === "single") msg = "Will scrape for: " + d.single;
    else {
      if (!d.start || !d.end || d.start > d.end) { showDateInfo("Start date must be before end date!", true); return; }
      var days = Math.round((new Date(d.end) - new Date(d.start)) / 86400000) + 1;
      msg = "Will scrape " + days + " day(s): " + d.start + " to " + d.end;
    }
    showDateInfo(msg, false);
  }

  function showDateInfo(msg, isError) {
    var el = $("date-info");
    el.textContent = msg;
    el.className = "info" + (isError ? " err" : "");
  }

  function syncDateRows() {
    var mode = document.querySelector('input[name=date_mode]:checked').value;
    $("single-date-row").classList.toggle("hidden", mode !== "single");
    $("range-date-row").classList.toggle("hidden", mode !== "range");
    describeDates();
  }

  /* ---------------- process selection ---------------- */
  function checkedProcesses() {
    return Array.prototype.slice
      .call(document.querySelectorAll(".proc-check:not(:disabled)"))
      .filter(function (c) { return c.checked; })
      .map(function (c) { return c.value; });
  }

  /* Times arrive as ISO strings with an offset, e.g.
     "2026-09-15T02:50:57.658681+00:00". Slicing characters 11-19 out of that
     throws the offset away and prints UTC no matter where the viewer is, so
     the clock on screen disagreed with the wall clock. Parse it and let the
     browser render it in its own zone. */
  function clockTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(11, 19);   // never blank
    return d.toLocaleTimeString([], {
      hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }

  /* 2026-09-14 -> 14/09/2026, for messages meant to be read rather than parsed. */
  function ddmmyyyy(iso) {
    var parts = String(iso).split("-");
    return parts.length === 3 ? parts[2] + "/" + parts[1] + "/" + parts[0] : iso;
  }

  /* ---------------- rendering ---------------- */
  var STATUS_ORDER = ["RUNNING", "NOT_RESPONDING", "FAILED", "WARNING", "SUCCESS", "PENDING"];

  function renderState(data) {
    var tiles = $("status-tiles");
    // Only show processes that have actually run, plus anything running now -
    // 45 permanently-pending tiles would bury the signal.
    var rows = data.processes.filter(function (p) {
      return p.run || p.status === "RUNNING";
    });
    rows.sort(function (a, b) {
      return STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status)
          || a.process.localeCompare(b.process);
    });

    if (!rows.length) {
      tiles.innerHTML = '<div class="muted">No runs recorded for ' + data.date + '.</div>';
    } else {
      tiles.innerHTML = rows.map(function (p) {
        var r = p.run || {}, bits = [];
        if (r.records_scraped) bits.push(r.records_scraped + " rows");
        if (r.duration && r.duration !== "-") bits.push(r.duration);
        if (p.status === "FAILED" && r.error) bits.push(String(r.error).slice(0, 40));
        return '<div class="tile ' + p.status + '">' +
               '<div class="tname">' + escapeHtml(p.process) + "</div>" +
               '<div class="tmeta">' + escapeHtml(bits.join(" · ") || p.status) + "</div></div>";
      }).join("");
    }

    var s = data.summary || {};
    $("m-success").textContent = s.SUCCESS || 0;
    $("m-failed").textContent  = s.FAILED || 0;
    $("m-warning").textContent = s.WARNING || 0;
    $("m-pending").textContent = (s.PENDING || 0) + (s.RUNNING || 0);
    $("m-stale").textContent   = s.NOT_RESPONDING || 0;

    var banner = $("status-banner");
    if (s.RUNNING) {
      banner.className = "banner run";
      banner.textContent = "Scraping in progress… (" + s.RUNNING + " running)";
    } else if (s.FAILED || s.NOT_RESPONDING) {
      banner.className = "banner bad";
      banner.textContent = "Some tasks failed. Check the logs below.";
    } else if (s.SUCCESS) {
      // Green is reserved for data that actually reached HRMS. Scraping and
      // combining can both succeed while the upload never happens, and
      // calling that "completed successfully" in green is what hides a
      // missing upload until somebody checks HRMS by hand.
      if (data.hrms_pushed) {
        banner.className = "banner ok";
        banner.textContent = "All recorded tasks completed successfully \u2014 data pushed to HRMS.";
      } else {
        banner.className = "banner warn";
        banner.textContent = "Scraped and combined, but nothing has reached HRMS for "
                           + data.date + " yet.";
      }
    } else {
      banner.className = "banner info";
      banner.textContent = "Ready. Configure settings and click Run.";
    }
  }

  function renderLogs(data) {
    var box = $("logs");
    if (!data.logs.length) {
      // Say which process and date came back empty; "No logs yet" reads like a
      // page that has not loaded when you have in fact filtered to a quiet day.
      box.textContent = currentProcess
        ? "No logs available for " + currentProcess + " on " + ddmmyyyy(currentDate) + "."
        : "No logs yet. Click Run to start.";
      return;
    }
    // Follow the tail unless the viewer has deliberately scrolled up.
    //
    // This used to be measured fresh on every render, which reads a brand-new
    // panel (scrollTop 0, hundreds of lines tall) as "scrolled up" and so
    // never scrolls. The result was that a run's live lines appended correctly
    // but arrived below the fold, and the panel sat showing hours-old lines -
    // indistinguishable from live logs not working at all.
    var pinned = box.dataset.pinned !== "0";
    box.innerHTML = data.logs.map(function (l) {
      var t = clockTime(l.timestamp);
      var stage = l.stage ? "[" + l.stage + "] " : "";
      return '<span class="lv-' + l.level + '">[' + t + "] " +
             escapeHtml(l.process) + " " + escapeHtml(stage + l.message) + "</span>";
    }).join("\n");
    if (pinned) box.scrollTop = box.scrollHeight;
  }

  function renderMatrix(data) {
    $("mm-dates").textContent  = data.rows.length;
    $("mm-green").textContent  = data.totals.green;
    $("mm-yellow").textContent = data.totals.yellow;
    $("mm-red").textContent    = data.totals.red;

    var head = "<tr><th>Date</th>" + data.processes.map(function (p) {
      return "<th>" + escapeHtml(p) + "</th>";
    }).join("") + "</tr>";
    var body = data.rows.map(function (row) {
      return '<tr><td class="datecell">' + row.date + "</td>" +
        data.processes.map(function (p) {
          var c = row.cells[p] || { state: "red", text: "" };
          return '<td class="' + c.state + '">' + escapeHtml(c.text) + "</td>";
        }).join("") + "</tr>";
    }).join("");
    $("matrix").innerHTML = head + body;

    var uHead = "<tr><th>Date</th><th>Processes Scraped</th><th>In Upload File</th>" +
                "<th>Upload Rows</th><th>HRMS</th></tr>";
    var uBody = data.rows.map(function (row) {
      var rows = row.upload_rows > 0 ? row.upload_rows : "-";
      return '<tr><td class="datecell">' + row.date + "</td>" +
             "<td>" + row.processes_scraped + "</td>" +
             "<td>" + row.processes_in_upload + "</td>" +
             '<td class="' + (row.upload_rows > 0 ? "" : "red") + '">' + rows + "</td>" +
             '<td class="' + (row.hrms ? "pushed" : "notpushed") + '">' +
             (row.hrms ? "Pushed" : "Not pushed") + "</td></tr>";
    }).join("");
    $("uploads").innerHTML = uHead + uBody;

    // HRMS per-process: union of everything that appears in any day's detail.
    var names = {};
    data.rows.forEach(function (r) {
      if (r.hrms_detail) Object.keys(r.hrms_detail).forEach(function (n) { names[n] = 1; });
    });
    var cols = Object.keys(names).sort();
    if (!cols.length) {
      $("hrms").innerHTML = '<tr><td class="muted" style="padding:10px">No HRMS upload detail for this range.</td></tr>';
      return;
    }
    var hHead = "<tr><th>Date</th>" + cols.map(function (c) {
      return "<th>" + escapeHtml(c) + "</th>"; }).join("") + "</tr>";
    var hBody = data.rows.map(function (row) {
      return '<tr><td class="datecell">' + row.date + "</td>" + cols.map(function (c) {
        var d = row.hrms_detail && row.hrms_detail[c];
        if (!d) return '<td class="red"></td>';
        var text = d.failed > 0 ? d.uploaded + " (" + d.failed + "F)" : String(d.uploaded);
        return '<td class="' + (d.failed > 0 ? "yellow" : "green") + '">' + text + "</td>";
      }).join("") + "</tr>";
    }).join("");
    $("hrms").innerHTML = hHead + hBody;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  /* ---------------- data fetching ---------------- */
  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (r.status === 401 || r.status === 403 || r.redirected) {
        location.href = "/accounts/login/?next=" + encodeURIComponent(location.pathname);
        throw new Error("auth");
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  // Which date the Scraper tab is showing. Defaults to yesterday (what the
  // scrapers report by default) but is changeable, so a run for any date can
  // be watched live.
  var currentDate = window.DASHBOARD.yesterday;
  //: "" means every process, which is what the panel showed before the filter.
  var currentProcess = "";
  var socket = null;   // assigned by connectSocket() below

  /* Requirement 8: after any reconnect, re-fetch full state so the screen is
     never stale. This is the single entry point both polling and the future
     WebSocket call. */
  function refreshAll() {
    return Promise.all([
      getJSON("/api/dashboard/state/?date=" + currentDate).then(renderState),
      getJSON("/api/dashboard/logs/?date=" + currentDate + "&limit=300"
              + "&process=" + encodeURIComponent(currentProcess)).then(renderLogs),
    ]).then(function () {
      if (socket && socket.readyState === WebSocket.OPEN) setConnection("live");
      else setConnection("polling");
    }).catch(function (e) { if (e.message !== "auth") setConnection("down"); });
  }

  function loadMatrix() {
    var url = "/api/dashboard/matrix/?start=" + $("mon_start").value +
              "&end=" + $("mon_end").value +
              "&process=" + encodeURIComponent(selectedMonProcesses().join(","));
    return getJSON(url).then(renderMatrix).catch(function (e) {
      if (e.message !== "auth") {
        $("matrix").innerHTML = '<tr><td style="padding:10px;color:#721c24">' +
                                escapeHtml(e.message) + "</td></tr>";
      }
    });
  }

  /* ---------------- live updates: WebSocket with auto-reconnect ----------
   * Requirement 8: show LIVE / DISCONNECTED, reconnect automatically, and
   * after reconnecting fetch full state so nothing on screen is stale.
   * A slow-poll safety net stays armed while the socket is down, so the
   * screen keeps updating even if the WebSocket can never be established
   * (an old proxy, for instance).
   */
  var retryDelay = 1000;          // grows to RETRY_MAX on repeated failures
  var RETRY_MAX = 15000;
  var fallbackTimer = null;
  var pingTimer = null;

  function startFallbackPolling() {
    if (fallbackTimer) return;
    fallbackTimer = setInterval(refreshAll, POLL_MS);
  }

  function stopFallbackPolling() {
    if (!fallbackTimer) return;
    clearInterval(fallbackTimer);
    fallbackTimer = null;
  }

  function connectSocket() {
    var scheme = location.protocol === "https:" ? "wss:" : "ws:";
    var url = scheme + "//" + location.host + "/ws/dashboard/";
    try {
      socket = new WebSocket(url);
    } catch (e) {
      scheduleReconnect();
      return;
    }

    socket.onopen = function () {
      setConnection("live");
      retryDelay = 1000;
      stopFallbackPolling();
      // Re-sync on every (re)connect - this is what prevents stale data.
      refreshAll();
      clearInterval(pingTimer);
      pingTimer = setInterval(function () {
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "ping" }));
        }
      }, 25000);
    };

    socket.onmessage = function (event) {
      var msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.type === "refresh") refreshAll();
    };

    socket.onclose = function (event) {
      clearInterval(pingTimer);
      socket = null;
      if (event.code === 4401) {       // server said: not authenticated
        location.href = "/accounts/login/?next=" +
                        encodeURIComponent(location.pathname);
        return;
      }
      setConnection("down");
      startFallbackPolling();          // keep the screen alive meanwhile
      scheduleReconnect();
    };

    socket.onerror = function () {
      if (socket && socket.readyState !== WebSocket.OPEN) setConnection("down");
    };
  }

  function scheduleReconnect() {
    setTimeout(connectSocket, retryDelay);
    retryDelay = Math.min(retryDelay * 2, RETRY_MAX);
  }

  function startLiveUpdates() {
    refreshAll();
    connectSocket();
  }

  /* ---------------- wiring ---------------- */
  document.querySelectorAll('input[name=date_mode]').forEach(function (r) {
    r.addEventListener("change", syncDateRows);
  });
  ["single_date", "start_date", "end_date"].forEach(function (id) {
    $(id).addEventListener("change", describeDates);
  });

  $("select-all").addEventListener("change", function () {
    var on = this.checked;
    document.querySelectorAll(".proc-check:not(:disabled)").forEach(function (c) {
      c.checked = on;
    });
  });

  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".tab").forEach(function (t) { t.classList.remove("active"); });
      tab.classList.add("active");
      var name = tab.dataset.tab;
      var pane = name;
      ["scraper", "monitor", "apr-clean", "disposition"].forEach(function (id) {
        $("tab-" + id).classList.toggle("hidden", id !== pane);
      });
      if (pane === "monitor") loadMatrix();
      if (pane === "apr-clean") aprCleanPanel.load();
      if (pane === "disposition") dispositionPanel.load();
    });
  });

  $("mon-refresh").addEventListener("click", loadMatrix);

  /* ---------------- Monitoring: process multi-select ---------------- */
  function monBoxes() {
    return Array.prototype.slice.call(document.querySelectorAll(".mon-proc"));
  }

  function selectedMonProcesses() {
    return monBoxes().filter(function (c) { return c.checked; })
                     .map(function (c) { return c.value; });
  }

  function paintMonLabel() {
    var picked = selectedMonProcesses();
    // Nothing ticked means no filter at all, which is every process - saying
    // "Select Process" for that would read as though nothing were shown.
    $("mon-process-label").textContent =
      picked.length === 0 ? "Select Process"
      : picked.length === 1 ? picked[0]
      : picked.length + " selected";
  }

  function openMonPanel(open) {
    $("mon-process-panel").classList.toggle("hidden", !open);
    $("mon-process-btn").setAttribute("aria-expanded", String(open));
  }

  $("mon-process-btn").addEventListener("click", function (e) {
    e.stopPropagation();
    openMonPanel($("mon-process-panel").classList.contains("hidden"));
  });

  // Clicking inside the panel must not close it; clicking anywhere else should.
  $("mon-process-panel").addEventListener("click", function (e) { e.stopPropagation(); });
  document.addEventListener("click", function () { openMonPanel(false); });

  monBoxes().forEach(function (box) {
    box.addEventListener("change", function () { paintMonLabel(); loadMatrix(); });
  });

  $("mon-process-all").addEventListener("click", function () {
    monBoxes().forEach(function (c) { c.checked = true; });
    paintMonLabel(); loadMatrix();
  });

  $("mon-process-none").addEventListener("click", function () {
    monBoxes().forEach(function (c) { c.checked = false; });
    paintMonLabel(); loadMatrix();
  });

  paintMonLabel();

  $("view_date").addEventListener("change", function () {
    currentDate = this.value || window.DASHBOARD.yesterday;
    refreshAll();
  });

  $("logs").addEventListener("scroll", function () {
    var atBottom = this.scrollHeight - this.scrollTop - this.clientHeight < 40;
    this.dataset.pinned = atBottom ? "1" : "0";
  });

  $("view_process").addEventListener("change", function () {
    currentProcess = this.value || "";
    refreshAll();
  });

  $("run-btn").addEventListener("click", function () {
    var names = checkedProcesses();
    var msg = $("run-msg");
    if (!names.length) { msg.className = "info err"; msg.textContent = "Select at least one process."; return; }

    var d = selectedDates();
    var body = new URLSearchParams();
    body.append("date_mode", d.mode);
    if (d.single) body.append("single_date", d.single);
    if (d.start) body.append("start_date", d.start);
    if (d.end) body.append("end_date", d.end);
    names.forEach(function (n) { body.append("processes", n); });

    $("run-btn").disabled = true;
    msg.className = "info"; msg.textContent = "Starting…";
    fetch("/api/dashboard/run/", {
      method: "POST", credentials: "same-origin",
      headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        $("run-btn").disabled = false;
        if (!res.ok) { msg.className = "info err"; msg.textContent = res.j.error || "Failed to start"; return; }
        var parts = ["Started " + res.j.started.length + " process(es) for " + res.j.dates.length + " date(s)"];
        if (res.j.busy.length) parts.push("already running: " + res.j.busy.join(", "));
        if (res.j.skipped.length) parts.push("unavailable: " + res.j.skipped.join(", "));
        msg.className = "info"; msg.textContent = parts.join(" · ");
        refreshAll();
      })
      .catch(function () {
        $("run-btn").disabled = false;
        msg.className = "info err"; msg.textContent = "Could not reach the server.";
      });
  });

  /* ---------------- theme ---------------- */
  /* The inline script in the page head has already applied the stored theme
     before paint; this only keeps the button in step and records a change.
     The choice is stored per browser, so the kiosk screen and someone's own
     laptop can sensibly differ. */
  function paintThemeButton(theme) {
    var dark = theme === "dark";
    $("theme-light").className = dark ? "" : "on";
    $("theme-dark").className  = dark ? "on" : "";
    $("theme-light").setAttribute("aria-pressed", String(!dark));
    $("theme-dark").setAttribute("aria-pressed", String(dark));
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark"
      ? "dark" : "light";
  }

  function applyTheme(next) {
    document.documentElement.setAttribute("data-theme", next);
    paintThemeButton(next);
    // Storage can throw in a private window or with site data blocked; the
    // theme still applies for this visit, it just is not remembered.
    try { localStorage.setItem("dashboard-theme", next); } catch (e) {}
  }

  paintThemeButton(currentTheme());
  $("theme-light").addEventListener("click", function () { applyTheme("light"); });
  $("theme-dark").addEventListener("click", function () { applyTheme("dark"); });

  // Follow the operating system while the viewer has not chosen for themselves.
  if (window.matchMedia) {
    var query = window.matchMedia("(prefers-color-scheme: dark)");
    var onSystemChange = function (e) {
      var chosen = null;
      try { chosen = localStorage.getItem("dashboard-theme"); } catch (err) {}
      if (chosen) return;
      var next = e.matches ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", next);
      paintThemeButton(next);
    };
    if (query.addEventListener) query.addEventListener("change", onSystemChange);
    else if (query.addListener) query.addListener(onSystemChange);
  }


  /* ---------------- dataset status panels (APR Clean, Disposition) ----------------
     Both panes have the same controls and the same four cards over a different
     folder, so one factory wires each of them rather than two near-copies. */
  function datasetPanel(prefix, kind, label) {
    function boxes() {
      return Array.prototype.slice.call(
        document.querySelectorAll("." + prefix + "-proc"));
    }
    function picked() {
      return boxes().filter(function (c) { return c.checked; })
                    .map(function (c) { return c.value; });
    }
    function paintLabel() {
      var sel = picked();
      $(prefix + "-process-label").textContent =
        sel.length === 0 ? "Select Process"
        : sel.length === 1 ? sel[0]
        : sel.length + " selected";
    }
    function openPanel(open) {
      $(prefix + "-process-panel").classList.toggle("hidden", !open);
      $(prefix + "-process-btn").setAttribute("aria-expanded", String(open));
    }

    function render(data) {
      $(prefix + "-dates").textContent     = data.rows.length;
      $(prefix + "-pushed").textContent    = data.totals.pushed;
      $(prefix + "-notpushed").textContent = data.totals.not_pushed;
      $(prefix + "-nodata").textContent    = data.totals.no_data;

      var table = $(prefix + "-table");
      var note = $(prefix + "-note");
      if (!data.processes.length) {
        table.innerHTML = '<tr><td class="muted" style="padding:10px">' +
          "No process selected, and none are available." + "</td></tr>";
        note.textContent = "";
        return;
      }
      if (!data.has_data) {
        // Real absence, not a loading state - say so plainly instead of
        // drawing a grid of empty cells that looks like a broken table.
        table.innerHTML = '<tr><td class="muted" style="padding:14px">' +
          "No " + label + " data found for " + ddmmyyyy(data.start) + " to " +
          ddmmyyyy(data.end) + " in Media/&lt;process&gt;/" + data.folder +
          "/." + "</td></tr>";
        note.textContent = "Nothing is inferred here - the cards count what is "
          + "on disk, so they stay at zero until this dataset is produced.";
        return;
      }
      var head = "<tr><th>Date</th>" + data.processes.map(function (p) {
        return "<th>" + escapeHtml(p) + "</th>";
      }).join("") + "</tr>";
      var body = data.rows.map(function (row) {
        return '<tr><td class="datecell">' + row.date + "</td>" +
          data.processes.map(function (p) {
            var c = row.cells[p] || { state: "red", text: "" };
            return '<td class="' + c.state + '">' + escapeHtml(c.text) + "</td>";
          }).join("") + "</tr>";
      }).join("");
      table.innerHTML = head + body;
      note.innerHTML = data.uploads_to_hrms
        ? "<code>N</code> = rows present and pushed to HRMS &nbsp;|&nbsp; " +
          "<code>N*</code> = rows present, not in HRMS &nbsp;|&nbsp; empty red = no data"
        : "<code>N*</code> = rows present &nbsp;|&nbsp; empty red = no data. " +
          "Disposition is a separate report and is never uploaded to HRMS, so " +
          "<b>Pushed to HRMS</b> stays at zero for this dataset.";
    }

    function load() {
      var url = "/api/dashboard/dataset/?kind=" + kind +
                "&start=" + $(prefix + "_start").value +
                "&end=" + $(prefix + "_end").value +
                "&process=" + encodeURIComponent(picked().join(","));
      return getJSON(url).then(render).catch(function (e) {
        $(prefix + "-table").innerHTML =
          '<tr><td style="padding:10px;color:#721c24">' + escapeHtml(String(e)) +
          "</td></tr>";
      });
    }

    $(prefix + "-refresh").addEventListener("click", load);
    $(prefix + "-process-btn").addEventListener("click", function (e) {
      e.stopPropagation();
      openPanel($(prefix + "-process-panel").classList.contains("hidden"));
    });
    $(prefix + "-process-panel").addEventListener("click", function (e) {
      e.stopPropagation();
    });
    document.addEventListener("click", function () { openPanel(false); });
    boxes().forEach(function (box) {
      box.addEventListener("change", function () { paintLabel(); load(); });
    });
    document.querySelector('[data-msel-all="' + prefix + '"]')
      .addEventListener("click", function () {
        boxes().forEach(function (c) { c.checked = true; });
        paintLabel(); load();
      });
    document.querySelector('[data-msel-none="' + prefix + '"]')
      .addEventListener("click", function () {
        boxes().forEach(function (c) { c.checked = false; });
        paintLabel(); load();
      });
    paintLabel();
    return { load: load };
  }

  var aprCleanPanel = datasetPanel("apr-clean", "apr_clean", "APR Clean");
  var dispositionPanel = datasetPanel("disposition", "disposition", "Disposition");

  syncDateRows();
  startLiveUpdates();
})();
