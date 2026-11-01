// Snappier page changes: warm the next page on hover/touch, and show instant feedback on click.
(function () {
  var warmed = {};
  function target(e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.origin !== location.origin || a.target === "_blank" || a.hasAttribute("download")) return null;
    if (a.pathname === location.pathname && a.search === location.search) return null;
    if (/^\/(logout|photos)/.test(a.pathname) || a.search.indexOf("export=") > -1) return null;
    return a;
  }
  function warm(e) {
    var a = target(e);
    if (!a || warmed[a.href]) return;
    warmed[a.href] = 1;
    var l = document.createElement("link");
    l.rel = "prefetch";
    l.href = a.href;
    document.head.appendChild(l);
  }
  document.addEventListener("mouseover", warm, { passive: true });
  document.addEventListener("touchstart", warm, { passive: true });
  document.addEventListener("click", function (e) {
    if (e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && target(e)) document.documentElement.classList.add("loading");
  });
  addEventListener("pageshow", function () { document.documentElement.classList.remove("loading"); });
})();
