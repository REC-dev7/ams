// Made-up devices placed in the real campus layout (campus_geo.js). Same seed every time, so the demo looks the same.
const CAMPUS = (() => {
  let seed = 2026;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  const pick = (a) => a[Math.floor(rnd() * a.length)];

  const SOFTWARE = [
    { name: "Windows 11 Education", publisher: "Microsoft", versions: ["23H2", "22H2"] },
    { name: "Microsoft Office 2021", publisher: "Microsoft", versions: ["16.0.17928", "16.0.17328", "16.0.16130"] },
    { name: "Google Chrome", publisher: "Google", versions: ["131.0.6778", "129.0.6668", "126.0.6478"] },
    { name: "Python", publisher: "Python Software Foundation", versions: ["3.12.7", "3.12.4", "3.10.11"] },
    { name: "Visual Studio Code", publisher: "Microsoft", versions: ["1.95.3", "1.92.2"] },
    { name: "Eclipse IDE", publisher: "Eclipse Foundation", versions: ["4.33", "4.30"], labs: ["CSE", "IT"] },
    { name: "MATLAB", publisher: "MathWorks", versions: ["R2024b", "R2023a"], labs: ["ECE", "EEE", "MECH", "AERO"] },
    { name: "AutoCAD", publisher: "Autodesk", versions: ["2025.1", "2025.0", "2024.1"], labs: ["MECH", "CIVIL", "AUTO"] },
    { name: "SolidWorks", publisher: "Dassault Systèmes", versions: ["2024 SP5", "2024 SP2"], labs: ["MECH", "AUTO", "AERO"] },
    { name: "ANSYS Student", publisher: "Ansys", versions: ["2024 R2", "2023 R2"], labs: ["AERO", "MECH"] },
    { name: "Oracle Database XE", publisher: "Oracle", versions: ["21.3", "18.4"], labs: ["CSE", "IT"] },
    { name: "Adobe Acrobat Reader", publisher: "Adobe", versions: ["24.004", "23.008"] },
  ];
  const LICENCES = [
    { product: "Microsoft Office 2021", seats: 700, renewal: "2027-03-31", type: "Campus agreement" },
    { product: "MATLAB", seats: 200, renewal: "2026-12-31", type: "Concurrent" },
    { product: "AutoCAD", seats: 12, renewal: "2026-12-15", type: "Education network" },
    { product: "SolidWorks", seats: 90, renewal: "2027-06-30", type: "Network" },
    { product: "ANSYS Student", seats: null, renewal: null, type: "Free" },
    { product: "Oracle Database XE", seats: null, renewal: null, type: "Free" },
  ];
  const LAB_NAMES = {
    CSE: ["Programming Lab", "AI & ML Lab", "Data Science Lab", "Project Lab"], IT: ["IT Lab", "Network Lab", "Cloud Lab", "Web Lab"],
    ECE: ["Electronics Lab", "DSP Lab", "VLSI Lab", "Embedded Lab"], EEE: ["Machines Lab", "Power Electronics Lab", "Control Lab"],
    MECH: ["CAD Lab", "CAM Lab", "Metrology Lab"], AERO: ["Aero CAD Lab", "Wind Tunnel Lab", "Avionics Lab"], AUTO: ["Engine Lab", "Auto CAD Lab"],
    CIVIL: ["Survey Lab", "Civil CAD Lab"], LIB: ["Digital Library", "Reading Hall"], ADMIN: ["Office", "Accounts", "Exam Cell", "Principal Office"],
  };
  const floors = (n) => ["G", ...Array.from({ length: n - 1 }, (_, i) => String(i + 1))];
  const devices = [], buildings = [];
  let n = 0;
  const used = {};
  for (const g of CAMPUS_GEO.buildings) {
    const xs = g.poly.map((p) => p[0]), zs = g.poly.map((p) => p[1]);
    const b = { ...g, size: [Math.max(...xs) - Math.min(...xs), Math.max(...zs) - Math.min(...zs)], floors: [] };
    for (const f of floors(g.levels)) {
      const rooms = [];
      if (g.kind === "academic") {
        const names = LAB_NAMES[g.dept] || LAB_NAMES.ADMIN;
        const k = 2 + Math.floor(rnd() * 2);
        for (let i = 0; i < k; i++) {
          if (g.dept === "ADMIN" || (f === "G" && i === 0)) rooms.push([g.dept === "ADMIN" ? `${pick(names)} ${f}${i + 1}` : "Department Office", "Office", 4 + Math.floor(rnd() * 5)]);
          else if (rnd() < 0.3) rooms.push([`${g.dept}-${f === "G" ? 0 : f}0${i + 1}`, "Classroom", 1]);
          else {
            used[g.dept] = (used[g.dept] || 0) + 1;
            rooms.push([`${names[(used[g.dept] - 1) % names.length]} ${Math.ceil(used[g.dept] / names.length)}`, "Lab", 18 + Math.floor(rnd() * 13)]);
          }
        }
      } else if (g.kind === "workshop") rooms.push(["Workshop floor", "Workshop", 3 + Math.floor(rnd() * 4)]);
      else if (g.kind === "hostel") rooms.push([f === "G" ? "Warden office" : `Floor ${f} corridor`, f === "G" ? "Office" : "Hostel", f === "G" ? 2 : 0]);
      else if (g.kind === "cafe") rooms.push(["Billing counter", "Cafe", 2]);
      else rooms.push(["Main hall", "Hall", 2]);
      b.floors.push({ floor: f, rooms: rooms.map(([name, type, count]) => ({ id: `${b.id}-${f}-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`, name, type, count, building: b.id, floor: f, devices: [] })) });
    }
    buildings.push(b);
  }
  const MODEL = { PC: ["Dell OptiPlex 7090", "HP ProDesk 400 G9", "Lenovo ThinkCentre M70q"], Server: ["Dell PowerEdge R650"], Switch: ["Cisco Catalyst 9200"],
    AP: ["Aruba AP-515"], Projector: ["Epson EB-X51"], Printer: ["HP LaserJet M408"] };
  for (const b of buildings) for (const fl of b.floors) for (const room of fl.rooms) {
    const short = room.name.split(/\s+/).filter((w) => /^[A-Za-z0-9]/.test(w)).map((w) => w[0]).join("").toUpperCase().slice(0, 3) || "RM";
    const add = (kind, label) => {
      n += 1;
      const d = { id: `AMS-${String(n).padStart(5, "0")}`, name: label, kind, room: room.id, building: b.id, dept: b.dept, model: pick(MODEL[kind]),
        serial: Math.floor(rnd() * 1e8).toString(36).toUpperCase(), ip: `10.${10 + buildings.indexOf(b)}.${b.floors.indexOf(fl) + 1}.${room.devices.length + 11}`,
        warranty: `202${6 + Math.floor(rnd() * 3)}-${String(1 + Math.floor(rnd() * 12)).padStart(2, "0")}-28`, faulty: false, issue: null,
        cpu: 10 + rnd() * 30, ram: 30 + rnd() * 30, disk: 30 + rnd() * 45, online: true, software: [] };
      if (kind === "PC" || kind === "Server") for (const s of SOFTWARE) {
        if (s.labs && !s.labs.includes(b.dept)) continue;
        if (!s.labs && rnd() < 0.08) continue;
        d.software.push({ name: s.name, version: rnd() < 0.68 ? s.versions[0] : pick(s.versions.slice(1)), newest: s.versions[0], publisher: s.publisher });
      }
      room.devices.push(d.id);
      devices.push(d);
    };
    const pcKind = room.type === "Classroom" || room.type === "Hall" ? "Projector" : "PC";
    const tag = `${b.dept.slice(0, 2)}${short}`;
    for (let i = 1; i <= room.count; i++) add(pcKind, `${tag}-${pcKind === "PC" ? "PC" : "PJ"}-${String(i).padStart(2, "0")}`);
    if (room.type === "Lab") add("Switch", `${tag}-SW-1`), add("Projector", `${tag}-PJ-L`), add("Printer", `${tag}-PR-1`);
    add("AP", `${tag}-AP-1`);
    if (b.name.startsWith("Main Block") && fl.floor === "G" && fl.rooms.indexOf(room) === 0) for (let i = 1; i <= 6; i++) add("Server", `DC-SERVER-${i}`);
  }
  for (const d of devices) {
    const r = rnd();
    if (r < 0.022) {
      d.faulty = true;
      d.issue = pick({ PC: ["Does not power on", "Monitor flickers", "Keyboard not working", "Blue screen on start", "No network"],
        Server: ["Disk failure warning", "Power supply fault"], Switch: ["Port 12 down", "Fan failure"], AP: ["No Wi-Fi signal"],
        Projector: ["Lamp not working", "No display"], Printer: ["Paper jam", "Toner empty"] }[d.kind]);
    } else if (r < 0.05) d.online = false;
    else if (r < 0.065) d.disk = 92 + rnd() * 7;
  }
  return { buildings, devices, SOFTWARE, LICENCES, rnd, pick };
})();
