// Shows the extra fields for the chosen kind of asset (CPU and RAM for a computer, ports for a switch).
(function () {
  var box = document.getElementById("specs");
  if (!box) return;
  var defs = JSON.parse(box.dataset.defs || "{}"), saved = JSON.parse(box.dataset.values || "{}"), posted = JSON.parse(box.dataset.posted || "{}");
  var holder = document.getElementById("specs-fields"), cat = document.querySelector("select[name=category_id]");
  function draw() {
    holder.innerHTML = "";
    var list = defs[cat.value] || [];
    box.style.display = list.length ? "" : "none";
    list.forEach(function (d) {
      var label = document.createElement("label");
      label.textContent = d.name + (d.unit ? " (" + d.unit + ")" : "") + (d.required ? " *" : "") + " ";
      var el;
      if (d.type === "choice") {
        el = document.createElement("select");
        var none = document.createElement("option"); none.value = ""; none.textContent = "—"; el.appendChild(none);
        d.choices.forEach(function (c) { var o = document.createElement("option"); o.textContent = c; el.appendChild(o); });
      } else {
        el = document.createElement("input");
        el.type = d.type === "number" ? "number" : d.type === "date" ? "date" : "text";
        if (d.type === "number") el.step = "any";
      }
      el.name = "attr_" + d.id;
      var v = posted["attr_" + d.id];
      if (v === undefined) v = saved[d.id];
      if (v !== undefined && v !== null) el.value = v;
      if (d.required) el.required = true;
      label.appendChild(el);
      holder.appendChild(label);
    });
  }
  cat.addEventListener("change", draw);
  draw();
})();
