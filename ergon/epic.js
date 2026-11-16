// Epic timeline: hover tips and the replay player. Server values arrive as attributes and are
// shown through textContent only. Replay is a pure function of the time T (seconds from the
// epic's start): reveal() gives each phase's visible width, so scrubbing and playing agree.
(function () {
  "use strict";

  // Width of a phase at time T: null before it starts, its full width once it has ended.
  function reveal(s, e, w, T) {
    if (T < s) return null;
    if (e <= s || T >= e) return w;
    return Math.max(1, w * (T - s) / (e - s));
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { reveal: reveal };
  if (typeof document === "undefined") return;

  var svg = document.getElementById("tl");
  if (!svg) return;
  var span = parseFloat(svg.getAttribute("data-span")) || 1;
  var segs = [].slice.call(svg.querySelectorAll("rect.seg")).map(function (r) {
    return { el: r, s: +r.getAttribute("data-s"), e: +r.getAttribute("data-e"), w: +r.getAttribute("data-w") };
  });
  var steps = [].slice.call(svg.querySelectorAll("path.cost")).map(function (p) {
    return { el: p, s: +p.getAttribute("data-s"), c: +p.getAttribute("data-c") };
  });
  var head = document.getElementById("head");
  var costNow = document.getElementById("cost-now");
  var clock = document.getElementById("clock");
  var play = document.getElementById("play");
  var speed = document.getElementById("speed");
  var scrub = document.getElementById("scrub");
  var tip = document.getElementById("tip");
  var x0 = parseFloat(head.getAttribute("x1")), plot;
  plot = svg.viewBox.baseVal.width - x0 - 14;

  function dur(s) {
    s = Math.floor(s);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return h + "h " + (m < 10 ? "0" : "") + m + "m";
    if (m) return m + "m " + (sec < 10 ? "0" : "") + sec + "s";
    return sec + "s";
  }

  var T = span, timer = null, prev = 0;

  function frame(t, replaying) {
    T = Math.max(0, Math.min(span, t));
    segs.forEach(function (g) {
      var w = replaying ? reveal(g.s, g.e, g.w, T) : g.w;
      if (w === null) { g.el.setAttribute("visibility", "hidden"); return; }
      g.el.setAttribute("visibility", "visible");
      g.el.setAttribute("width", String(w));
    });
    var cost = 0;
    steps.forEach(function (p) {
      var on = !replaying || T >= p.s;
      p.el.setAttribute("visibility", on ? "visible" : "hidden");
      if (on && p.c > cost) cost = p.c;
    });
    if (costNow) costNow.textContent = "$" + cost.toFixed(2);
    head.setAttribute("visibility", replaying ? "visible" : "hidden");
    var hx = x0 + (T / span) * plot;
    head.setAttribute("x1", String(hx));
    head.setAttribute("x2", String(hx));
    if (clock) clock.textContent = replaying ? "+" + dur(T) + " of " + dur(span) : "full epic · " + dur(span);
    if (scrub) scrub.value = String(Math.round(T / span * 1000));
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    if (play) play.textContent = "▶ replay";
  }

  function tick() {
    var now = performance.now();
    var rate = span / 60 * Number(speed.value);  // x1: the whole epic in a minute
    var t = T + (now - prev) / 1000 * rate;
    prev = now;
    if (t >= span) { frame(span, true); stop(); return; }
    frame(t, true);
  }

  play.addEventListener("click", function () {
    if (timer) { stop(); return; }
    if (T >= span) T = 0;
    prev = performance.now();
    play.textContent = "⏸ pause";
    frame(T, true);
    timer = setInterval(tick, 40);
  });
  scrub.addEventListener("input", function () {
    stop();
    frame(span * Number(scrub.value) / 1000, true);
  });

  svg.addEventListener("mousemove", function (ev) {
    var t = ev.target;
    var text = t && t.getAttribute ? t.getAttribute("data-tip") : null;
    if (!text) { tip.hidden = true; return; }
    tip.textContent = text;
    tip.hidden = false;
    tip.style.left = Math.min(ev.clientX + 12, window.innerWidth - 260) + "px";
    tip.style.top = (ev.clientY + 14) + "px";
  });
  svg.addEventListener("mouseleave", function () { tip.hidden = true; });
  frame(span, false);
})();
