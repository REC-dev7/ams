// Ticket form helpers: Block > Floor > Room pickers, "goes to team" hint, urgent-reason hint, quick replies.
(function () {
  function opt(value, text, selected) {
    var o = document.createElement("option");
    o.value = value;
    o.textContent = text;
    if (selected) o.selected = true;
    return o;
  }

  document.querySelectorAll(".loc-picker").forEach(function (box) {
    var tree = JSON.parse(box.dataset.tree || "{}");
    var block = box.querySelector("[data-role=block]");
    var floor = box.querySelector("[data-role=floor]");
    var room = box.querySelector("[data-role=room]");
    var other = box.querySelector("input[name=location]");
    var want = box.dataset.selected, startBlock = "", startFloor = "";
    Object.keys(tree).forEach(function (b) {
      Object.keys(tree[b]).forEach(function (f) {
        tree[b][f].forEach(function (r) { if (String(r[0]) === want) { startBlock = b; startFloor = f; } });
      });
    });
    function fill(sel, first, items, chosen) {
      sel.innerHTML = "";
      sel.appendChild(opt("", first));
      items.forEach(function (i) { sel.appendChild(opt(i[0], i[1], i[0] === chosen)); });
    }
    function onBlock(f) {
      fill(floor, "Floor", Object.keys(tree[block.value] || {}).map(function (x) { return [x, x]; }), f);
      onFloor();
    }
    function onFloor() {
      var rooms = ((tree[block.value] || {})[floor.value] || []).map(function (r) { return [String(r[0]), r[1]]; });
      fill(room, "Room", rooms, want);
    }
    fill(block, "Block", Object.keys(tree).map(function (x) { return [x, x]; }), startBlock);
    if (startBlock) { block.value = startBlock; onBlock(startFloor); floor.value = startFloor; onFloor(); room.value = want; }
    else onBlock("");
    block.addEventListener("change", function () { onBlock(""); });
    floor.addEventListener("change", onFloor);
    room.addEventListener("change", function () { if (room.value) other.value = ""; });
  });

  document.querySelectorAll("select[data-teams]").forEach(function (sel) {
    var hint = sel.parentNode.querySelector("[data-team-hint]");
    var map = JSON.parse(sel.dataset.teams);
    function show() { if (hint) hint.textContent = "Goes to: " + (map[sel.value] || "Other"); }
    sel.addEventListener("change", show);
    show();
  });

  document.querySelectorAll("select[data-reasons]").forEach(function (sel) {
    var hint = sel.parentNode.querySelector("[data-reason-hint]");
    var map = JSON.parse(sel.dataset.reasons);
    sel.addEventListener("change", function () {
      if (hint) hint.textContent = map[sel.value] ? "Priority will be at least " + map[sel.value] + "." : "An exam, event or class in progress moves it up the queue.";
    });
  });

  document.querySelectorAll("[data-canned]").forEach(function (b) {
    b.addEventListener("click", function () {
      var box = b.closest("form").querySelector("[name=body]");
      box.value = (box.value ? box.value + " " : "") + b.dataset.canned;
      box.focus();
    });
  });
})();
