/* NOW brand primitives and finite motion. No timers on streaming tokens. */
(function (global) {
  "use strict";
  var RT = global.RT = global.RT || {};
  var reduced = matchMedia("(prefers-reduced-motion: reduce)");
  var uid = 0;
  function duration(token) {
    return reduced.matches ? 0 : parseFloat(getComputedStyle(document.documentElement).getPropertyValue(token)) || 0;
  }
  function enter(node, kind) {
    if (node && !reduced.matches) {
      var name = "motion-" + kind;
      node.classList.add(name);
      node.addEventListener("animationend", function cleanup(e) {
        if (e.target !== node) return;
        node.classList.remove(name); node.removeEventListener("animationend", cleanup);
      });
    }
    return node;
  }
  function fold(root, body, open) {
    var inner = document.createElement("div"); inner.className = "rp-inner";
    var content = document.createElement("div"); content.className = "rp-content";
    while (body.firstChild) content.appendChild(body.firstChild);
    inner.appendChild(content); body.appendChild(inner);
    var button = root.querySelector(".rp-head");
    body.id = "execution-fold-" + (++uid);
    button.setAttribute("aria-controls", body.id);
    function set(next) {
      root.classList.toggle("open", next);
      button.setAttribute("aria-expanded", String(next));
      body.setAttribute("aria-hidden", String(!next)); body.inert = !next;
      var hint = button.querySelector(".rp-toggle-label");
      if (hint) hint.textContent = next ? "收起执行过程" : "查看执行过程";
    }
    set(open);
    button.addEventListener("click", function () { set(!root.classList.contains("open")); });
  }
  function collapse(node) {
    if (!node || !node.isConnected || reduced.matches) return Promise.resolve();
    // One layout read, then CSS transitions; no per-frame height measurements.
    var height = node.getBoundingClientRect().height;
    node.style.height = height + "px"; node.classList.add("motion-collapse");
    return new Promise(function (resolve) {
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { node.style.height = "0px"; node.style.opacity = "0"; });
      });
      setTimeout(resolve, duration("--motion-slow") + 40);
    });
  }
  function preserveReading(stream, render) {
    var scroll = stream.scrollTop;
    var nearBottom = stream.scrollHeight - scroll - stream.clientHeight < 50;
    var bounds = stream.getBoundingClientRect();
    var anchor = Array.from(stream.querySelectorAll(".msg-row")).find(function (n) { return n.getBoundingClientRect().bottom > bounds.top; });
    var text = anchor && anchor.textContent, top = anchor && anchor.getBoundingClientRect().top;
    render();
    if (nearBottom) { stream.scrollTop = stream.scrollHeight; return; }
    var replacement = text && Array.from(stream.querySelectorAll(".msg-row")).find(function (n) { return n.textContent === text; });
    stream.scrollTop = scroll;
    if (replacement) stream.scrollTop += replacement.getBoundingClientRect().top - top;
  }
  function wordmark() {
    var node = document.createElement("span"); node.className = "brand-wordmark";
    node.appendChild(document.createTextNode("此刻"));
    var english = document.createElement("small"); english.textContent = "NOW";
    node.appendChild(english); return node;
  }
  function lockup() {
    var node = document.createElement("span"); node.className = "brand-lockup";
    var mark = document.createElement("span"); mark.className = "logo"; mark.setAttribute("aria-hidden", "true");
    node.appendChild(mark); node.appendChild(wordmark()); return node;
  }
  RT.brand = { wordmark: wordmark, lockup: lockup };
  RT.motion = { enter: enter, fold: fold, collapse: collapse, preserveReading: preserveReading, duration: duration };

  // The shell follows the visual viewport when a mobile software keyboard opens.
  var frame;
  function viewport() {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(function () {
      var vv = global.visualViewport;
      var mobile = matchMedia("(max-width: 760px)").matches;
      if (vv && mobile && Math.abs(vv.scale - 1) < .01) {
        document.documentElement.style.setProperty("--app-height", vv.height + "px");
        document.documentElement.classList.toggle("keyboard-open", innerHeight - vv.height > 100);
        if (document.activeElement && document.activeElement.id === "composerInput") {
          requestAnimationFrame(function () {
            var composer = document.querySelector(".composer-wrap"), stream = document.getElementById("stream");
            if (!composer || !stream || !stream.contains(composer)) return;
            var bottom = composer.getBoundingClientRect().bottom;
            var limit = Math.min(vv.height, stream.getBoundingClientRect().bottom) - 12;
            if (bottom > limit) stream.scrollTop += bottom - limit;
          });
        }
      } else {
        document.documentElement.style.removeProperty("--app-height");
        document.documentElement.classList.remove("keyboard-open");
      }
    });
  }
  global.addEventListener("resize", viewport);
  if (global.visualViewport) global.visualViewport.addEventListener("resize", viewport);
  viewport();
  var app = document.querySelector(".app"), drawer = document.getElementById("detailDrawer");
  if (app && drawer) {
    var sync = function () { var open = app.classList.contains("drawer-open"); drawer.inert = !open; drawer.setAttribute("aria-hidden", String(!open)); };
    new MutationObserver(sync).observe(app, { attributes: true, attributeFilter: ["class"] }); sync();
  }
})(window);
