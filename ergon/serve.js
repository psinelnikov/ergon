// Live dispatch state for the board: polls /live.json and redraws the LIVE table and the counts.
// Every value from the server goes in through textContent, never as HTML.
(function () {
  "use strict";
  var POLL_MS = 4000;
  var skew = 0;  // server clock minus ours, seconds
  var rows = [];

  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  }

  function dur(s) {
    if (s === null || s === undefined || s < 0) return "—";
    s = Math.floor(s);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return h + "h " + (m < 10 ? "0" : "") + m + "m";
    if (m) return m + "m " + (sec < 10 ? "0" : "") + sec + "s";
    return sec + "s";
  }

  function money(x) { return "$" + (Number(x) || 0).toFixed(2); }

  function draw() {
    var body = document.getElementById("live-body");
    var empty = document.getElementById("live-empty");
    var table = document.getElementById("live-table");
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);
    var now = Date.now() / 1000 + skew;
    rows.forEach(function (r) {
      var tr = document.createElement("tr");
      var id = el("td", null, "id");
      var a = el("a", r.id);
      if (/^T-\d+$/.test(r.id)) a.setAttribute("href", "/t/" + r.id);
      id.appendChild(a);
      tr.appendChild(id);
      tr.appendChild(el("td", r.tier));
      tr.appendChild(el("td", r.since_ts ? dur(now - r.since_ts) : "—", "num"));
      tr.appendChild(el("td", r.round > 1 ? r.round : "1", "num"));
      tr.appendChild(el("td", money(r.spent), "num"));
      tr.appendChild(el("td", r.tokens === null || r.tokens === undefined ? "—" : Number(r.tokens).toLocaleString(), "num hide-narrow"));
      tr.appendChild(el("td", r.last || "—", "hide-narrow"));
      tr.appendChild(el("td", r.title, "why"));
      body.appendChild(tr);
    });
    if (table) table.hidden = rows.length === 0;
    if (empty) empty.hidden = rows.length !== 0;
  }

  function poll() {
    var x = new XMLHttpRequest();
    x.open("GET", "/live.json");
    x.timeout = 10000;
    x.onload = function () {
      if (x.status !== 200) return;
      var d;
      try { d = JSON.parse(x.responseText); } catch (e) { return; }
      skew = (d.now_ts || Date.now() / 1000) - Date.now() / 1000;
      rows = Array.isArray(d.doing) ? d.doing : [];
      Object.keys(d.counts || {}).forEach(function (k) {
        var c = document.getElementById("count-" + k);
        if (c) c.textContent = String(d.counts[k]);
      });
      var st = document.getElementById("live-at");
      if (st) st.textContent = "updated " + new Date().toLocaleTimeString();
      draw();
    };
    x.send();
  }

  poll();
  setInterval(poll, POLL_MS);
  setInterval(draw, 1000);
})();
