// REC Campus Live 3D: campus -> building (exploded floors) -> lab (desks, PCs, racks) -> device.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

const { buildings, devices, LICENCES, SOFTWARE, rnd, pick } = CAMPUS;
const byId = Object.fromEntries(devices.map((d) => [d.id, d]));
const bById = Object.fromEntries(buildings.map((b) => [b.id, b]));
const roomById = {};
buildings.forEach((b) => b.floors.forEach((f) => f.rooms.forEach((r) => (roomById[r.id] = r))));
const HEX = { online: 0x22c55e, warning: 0xf59e0b, faulty: 0xef4444, offline: 0x64748b };
const CSSC = { online: "#22c55e", warning: "#f59e0b", faulty: "#ef4444", offline: "#64748b" };
const LABEL = { online: "Online", warning: "Needs attention", faulty: "Faulty", offline: "Offline" };
const state = (d) => (d.faulty ? "faulty" : !d.online ? "offline" : Math.max(d.cpu, d.ram, d.disk) >= 90 ? "warning" : "online");
const tally = (ids) => ids.reduce((t, id) => (t[state(byId[id])]++, t), { online: 0, warning: 0, faulty: 0, offline: 0 });
const worst = (t) => ["faulty", "offline", "warning", "online"].find((k) => t[k]) || "online";
const idsOfBuilding = (b) => b.floors.flatMap((f) => f.rooms.flatMap((r) => r.devices));
const allIds = devices.map((d) => d.id);
const behind = (d) => d.software.filter((s) => s.version !== s.newest);
const floorNo = (f) => (f === "G" ? 0 : +f);
const floorName = (f) => (f === "G" ? "Ground floor" : `Floor ${f}`);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const bar = (t) => `<span class="bar">${Object.keys(HEX).map((k) => (t[k] ? `<i style="flex:${t[k]};background:${CSSC[k]}"></i>` : "")).join("")}</span>`;

// ---------- scene ----------
const stage = document.getElementById("stage");
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setClearColor(0x000000, 0); // the page's night-sky gradient shows through
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
stage.appendChild(renderer.domElement);
const labels = new CSS2DRenderer();
labels.domElement.style.cssText = "position:absolute;top:0;left:0;pointer-events:none;z-index:1"; // its own layer, under the HUD and tab pages
stage.appendChild(labels.domElement);
const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0xcfe3f2, 200, 560);
const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 1000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.47;
controls.minDistance = 5;
controls.maxDistance = 300;
const hemi = new THREE.HemisphereLight(0xe3ecf8, 0x9a8f78, 1.1);
scene.add(hemi);
const fill = new THREE.DirectionalLight(0xfff0dd, 0.6); // soft light from the other side so shaded walls keep their colour
fill.position.set(-80, 50, -60);
scene.add(fill);
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
sun.position.set(70, 110, 50);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.bias = -0.0004;
sun.shadow.normalBias = 0.05;
Object.assign(sun.shadow.camera, { left: -110, right: 110, top: 110, bottom: -110, far: 400 });
sun.target.position.set(-8, 0, -21);
scene.add(sun, sun.target);

const mats = {};
const mat = (color, o = {}) => mats[color + JSON.stringify(o)] ||= new THREE.MeshStandardMaterial({ color, roughness: 0.75, metalness: 0.05, ...o });
const box = (w, h, d, material, x = 0, y = 0, z = 0) => {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
  m.position.set(x, y, z);
  m.castShadow = m.receiveShadow = true;
  return m;
};
const edges = (mesh, color = 0xffffff, opacity = 0.25) => {
  const l = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry), new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
  l.position.copy(mesh.position);
  return l;
};
const label = (html, y = 0, cls = "label3d") => {
  const div = document.createElement("div");
  div.className = cls;
  div.innerHTML = html;
  const o = new CSS2DObject(div);
  o.position.y = y;
  return o;
};
const clear = (g) => {
  g.traverse((o) => { if (o.isCSS2DObject) o.element.remove(); });
  g.clear();
};

const campusG = new THREE.Group(), buildingG = new THREE.Group(), roomG = new THREE.Group();
scene.add(campusG, buildingG, roomG);
let pickables = [];
const live = {}; // device id -> materials that show its status (screens, LEDs)

// ---------- campus: real outlines (OpenStreetMap) + the college's own map, built with detail ----------
const FH = 0.95; // one floor = 3.8 m (1 unit = 4 m)
const CENTER = new THREE.Vector3(-8, 0, -21);
const roofs = {}, bLabels = {};
const STYLE = { // wall colour, trim, accent per kind of building
  academic: { wall: "#efe7d8", trim: "#d9cfbb", glass: [143, 183, 222] },
  hostel: { wall: "#ead2ad", trim: "#d1b88f", glass: [150, 190, 215] },
  workshop: { wall: "#b7c0cc", trim: "#97a1ae", glass: [120, 150, 180] },
  cafe: { wall: "#f0c697", trim: "#dca96f", glass: [160, 200, 220] },
  hall: { wall: "#e3def2", trim: "#c9c1e2", glass: [150, 170, 215] },
};
const centroidOf = (poly) => poly.reduce((a, [x, z]) => [a[0] + x / poly.length, a[1] + z / poly.length], [0, 0]);
const inside = (x, z, poly) => {
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, zi] = poly[i], [xj, zj] = poly[j];
    if (zi > z !== zj > z && x < ((xj - xi) * (z - zi)) / (zj - zi) + xi) c = !c;
  }
  return c;
};
function prism(poly, h, material, y0 = 0) {
  const s = new THREE.Shape();
  poly.forEach(([x, z], i) => (i ? s.lineTo(x, z) : s.moveTo(x, z)));
  const g = new THREE.ExtrudeGeometry(s, { depth: h, bevelEnabled: false });
  g.rotateX(Math.PI / 2); // the shape's y becomes the ground's z; extrusion points down, so lift it
  g.translate(0, y0 + h, 0);
  const m = new THREE.Mesh(g, material);
  m.castShadow = m.receiveShadow = true;
  return m;
}
function ribbon(points, width, material, y = 0.03, shadow = false) {
  const g = new THREE.Group();
  for (let i = 1; i < points.length; i++) {
    const [x1, z1] = points[i - 1], [x2, z2] = points[i], len = Math.hypot(x2 - x1, z2 - z1);
    const seg = box(len + width * 0.6, 0.05, width, material, (x1 + x2) / 2, y, (z1 + z2) / 2);
    seg.rotation.y = -Math.atan2(z2 - z1, x2 - x1);
    seg.castShadow = shadow;
    g.add(seg);
  }
  return g;
}
const tex = (canvas, repeat) => {
  const t = new THREE.CanvasTexture(canvas);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  if (repeat) { t.wrapS = t.wrapT = THREE.RepeatWrapping; t.repeat.set(...repeat); }
  return t;
};
const canvasOf = (w, h, draw) => { const c = document.createElement("canvas"); c.width = w; c.height = h; draw(c.getContext("2d"), w, h); return c; };

// one bay of one floor, drawn per kind of building; the emissive twin lights the windows at night
function drawFacade(ctx, w, h, kind, lit) {
  const S = STYLE[kind], [gr, gg, gb] = S.glass, glow = lit === 1 ? "#ffd98a" : "#d8ecff";
  const win = (x, y, ww, hh, cols = 1, rows = 2) => {
    if (lit) { ctx.fillStyle = glow; ctx.fillRect(x, y, ww, hh); const g = ctx.createLinearGradient(0, y, 0, y + hh); g.addColorStop(0, "rgba(255,255,255,.35)"); g.addColorStop(1, "rgba(0,0,0,.25)"); ctx.fillStyle = g; ctx.fillRect(x, y, ww, hh); return; }
    ctx.fillStyle = "#4b5563"; ctx.fillRect(x - 5, y - 5, ww + 10, hh + 10); // frame
    const g = ctx.createLinearGradient(x, y, x + ww, y + hh);
    g.addColorStop(0, `rgb(${gr + 40},${gg + 35},${gb + 25})`); g.addColorStop(0.45, `rgb(${gr},${gg},${gb})`); g.addColorStop(1, `rgb(${gr - 55},${gg - 55},${gb - 45})`);
    ctx.fillStyle = g; ctx.fillRect(x, y, ww, hh);
    ctx.fillStyle = "rgba(255,255,255,.22)"; ctx.beginPath(); ctx.moveTo(x, y + hh * 0.7); ctx.lineTo(x + ww * 0.55, y); ctx.lineTo(x + ww * 0.8, y); ctx.lineTo(x, y + hh); ctx.fill(); // reflection
    ctx.fillStyle = "#4b5563";
    for (let i = 1; i < cols; i++) ctx.fillRect(x + (ww * i) / cols - 2, y, 4, hh);
    for (let i = 1; i < rows; i++) ctx.fillRect(x, y + (hh * i) / rows - 2, ww, 4);
    ctx.fillStyle = "#f4f1ea"; ctx.fillRect(x - 9, y + hh + 5, ww + 18, 7); // sill
  };
  if (!lit) { // plaster with a little speckle and a darker floor band
    ctx.fillStyle = S.wall; ctx.fillRect(0, 0, w, h);
    for (let i = 0; i < 700; i++) { ctx.fillStyle = `rgba(${Math.random() < 0.5 ? "255,255,255" : "90,80,60"},${Math.random() * 0.06})`; ctx.fillRect(Math.random() * w, Math.random() * h, 2 + Math.random() * 3, 2 + Math.random() * 3); }
    ctx.fillStyle = S.trim; ctx.fillRect(0, h - 16, w, 16);
    ctx.fillStyle = "rgba(0,0,0,.08)"; ctx.fillRect(0, h - 16, w, 3);
    ctx.fillStyle = "rgba(255,255,255,.35)"; ctx.fillRect(0, 0, 14, h); ctx.fillRect(w - 14, 0, 14, h); // pilasters between bays
    ctx.fillStyle = "rgba(0,0,0,.07)"; ctx.fillRect(14, 0, 3, h); ctx.fillRect(w - 17, 0, 3, h);
  } else { ctx.fillStyle = "#000"; ctx.fillRect(0, 0, w, h); }
  if (kind === "academic") {
    win(w * 0.25, h * 0.22, w * 0.5, h * 0.44, 2, 2);
    if (!lit) { const g = ctx.createLinearGradient(0, h * 0.12, 0, h * 0.2); g.addColorStop(0, "rgba(0,0,0,0)"); g.addColorStop(1, "rgba(0,0,0,.35)"); ctx.fillStyle = g; ctx.fillRect(w * 0.14, h * 0.12, w * 0.72, h * 0.08); ctx.fillStyle = "#e9e3d4"; ctx.fillRect(w * 0.12, h * 0.1, w * 0.76, 7); } // sunshade
  } else if (kind === "hostel") {
    win(w * 0.18, h * 0.18, w * 0.28, h * 0.5, 1, 2);
    win(w * 0.56, h * 0.18, w * 0.26, h * 0.5, 1, 2);
    if (!lit) { ctx.fillStyle = "#f7f4ee"; ctx.fillRect(0, h * 0.74, w, 9); ctx.fillStyle = "rgba(60,60,60,.55)"; for (let x = 8; x < w; x += 14) ctx.fillRect(x, h * 0.74 + 9, 3, h * 0.12); ctx.fillRect(0, h * 0.74 + h * 0.12, w, 3); } // balcony rail
  } else if (kind === "workshop") {
    if (!lit) { ctx.fillStyle = "rgba(0,0,0,.14)"; for (let x = 0; x < w; x += 9) ctx.fillRect(x, 0, 3, h); }
    win(w * 0.1, h * 0.08, w * 0.8, h * 0.2, 4, 1);
    if (!lit) { ctx.fillStyle = "#6b7280"; ctx.fillRect(w * 0.18, h * 0.42, w * 0.64, h * 0.5); ctx.fillStyle = "rgba(0,0,0,.25)"; for (let y = h * 0.42; y < h * 0.92; y += 9) ctx.fillRect(w * 0.18, y, w * 0.64, 2); } // roller door
  } else if (kind === "cafe") {
    win(w * 0.07, h * 0.28, w * 0.86, h * 0.5, 3, 1);
    if (!lit) { ctx.fillStyle = "#b5532a"; ctx.fillRect(0, h * 0.14, w, h * 0.13); ctx.fillStyle = "#fff"; for (let x = 0; x < w; x += 28) ctx.fillRect(x, h * 0.14, 14, h * 0.13); }
  } else {
    win(w * 0.1, h * 0.12, w * 0.36, h * 0.72, 1, 3);
    win(w * 0.54, h * 0.12, w * 0.36, h * 0.72, 1, 3);
  }
}
const facade = {}, facadeLit = [];
for (const kind of Object.keys(STYLE)) for (const v of [0, 1, 2]) {
  const m = new THREE.MeshStandardMaterial({ map: tex(canvasOf(256, 224, (c, w, h) => drawFacade(c, w, h, kind, 0)), [1, 1]), roughness: 0.82, metalness: 0.02, side: THREE.DoubleSide });
  if (v) { m.emissive = new THREE.Color(0xffffff); m.emissiveMap = tex(canvasOf(256, 224, (c, w, h) => drawFacade(c, w, h, kind, v)), [1, 1]); m.emissiveIntensity = 0.1; facadeLit.push(m); }
  facade[`${kind}:${v}`] = m;
}
const grassTex = tex(canvasOf(512, 512, (c, w, h) => {
  c.fillStyle = "#4d8a45"; c.fillRect(0, 0, w, h);
  for (let i = 0; i < 9000; i++) { c.fillStyle = `hsla(${95 + Math.random() * 25},${40 + Math.random() * 25}%,${28 + Math.random() * 22}%,.35)`; c.fillRect(Math.random() * w, Math.random() * h, 2 + Math.random() * 6, 2); }
  for (let y = 0; y < h; y += 64) { c.fillStyle = "rgba(255,255,255,.035)"; c.fillRect(0, y, w, 32); } // mowing stripes
}), [70, 60]);
const asphaltTex = tex(canvasOf(256, 256, (c, w, h) => {
  c.fillStyle = "#3a3e49"; c.fillRect(0, 0, w, h);
  for (let i = 0; i < 4000; i++) { const v = 40 + Math.random() * 45; c.fillStyle = `rgba(${v},${v},${v + 6},.5)`; c.fillRect(Math.random() * w, Math.random() * h, 2, 2); }
}), [1, 1]);
const pavingTex = tex(canvasOf(256, 256, (c, w, h) => {
  c.fillStyle = "#d9d4c8"; c.fillRect(0, 0, w, h);
  c.strokeStyle = "rgba(90,80,60,.35)"; c.lineWidth = 2;
  for (let x = 0; x <= w; x += 32) { c.beginPath(); c.moveTo(x, 0); c.lineTo(x, h); c.stroke(); }
  for (let y = 0; y <= h; y += 32) { c.beginPath(); c.moveTo(0, y); c.lineTo(w, y); c.stroke(); }
}), [8, 8]);

const flagParts = {}, fountainParts = [], movers = [], lamps = { mat: null, halos: [] };
const lampMat = new THREE.MeshStandardMaterial({ color: 0xfff1c9, emissive: 0xffe9a8, emissiveIntensity: 0.1 });
const haloTex = tex(canvasOf(64, 64, (c) => { const g = c.createRadialGradient(32, 32, 0, 32, 32, 32); g.addColorStop(0, "rgba(255,236,170,.95)"); g.addColorStop(1, "rgba(255,236,170,0)"); c.fillStyle = g; c.fillRect(0, 0, 64, 64); }));

// merged wall geometry: one quad per edge piece, grouped by material so each building costs a handful of draw calls
function wallBuilder() {
  const store = new Map();
  const add = (key, x1, z1, x2, z2, y0, y1, nx, nz, u1, v1) => {
    if (!store.has(key)) store.set(key, { p: [], n: [], u: [], i: [] });
    const s = store.get(key), o = s.p.length / 3;
    s.p.push(x1, y0, z1, x2, y0, z2, x2, y1, z2, x1, y1, z1);
    for (let k = 0; k < 4; k++) s.n.push(nx, 0, nz);
    s.u.push(0, 0, u1, 0, u1, v1, 0, v1);
    s.i.push(o, o + 1, o + 2, o, o + 2, o + 3);
  };
  const meshes = (materials, userData) => [...store].map(([key, s]) => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(s.p, 3));
    g.setAttribute("normal", new THREE.Float32BufferAttribute(s.n, 3));
    g.setAttribute("uv", new THREE.Float32BufferAttribute(s.u, 2));
    g.setIndex(s.i);
    const m = new THREE.Mesh(g, materials[key]);
    m.castShadow = m.receiveShadow = true;
    m.userData = userData;
    return m;
  });
  return { add, meshes };
}
const outward = (poly, k, cx, cz) => {
  const [x1, z1] = poly[k], [x2, z2] = poly[(k + 1) % poly.length], len = Math.hypot(x2 - x1, z2 - z1) || 1;
  let nx = (z2 - z1) / len, nz = -(x2 - x1) / len;
  const mx = (x1 + x2) / 2, mz = (z1 + z2) / 2;
  if ((mx - cx) * nx + (mz - cz) * nz < 0) { nx = -nx; nz = -nz; }
  return { x1, z1, x2, z2, len, nx, nz, mx, mz };
};

const roofBits = { tanks: [], units: [], stairs: [], panels: [] };
const awningMat = mat(0xb5532a), purple = mat(0x6a1b9a), white = mat(0xf8fafc), steel = mat(0x8b94a3, { metalness: 0.6, roughness: 0.35 });
function buildBuildingMesh(b) {
  const [cx, cz] = centroidOf(b.poly), levels = b.floors.length, kind = STYLE[b.kind] ? b.kind : "academic", H = levels * FH;
  const g = new THREE.Group(), wb = wallBuilder(), mats3 = { ...Object.fromEntries(Object.entries(facade)), plinth: mat(0x8a8f99), cornice: purple, parapet: mat(0xe9e4d8), strip: null };
  const stripMat = new THREE.MeshStandardMaterial({ color: 0x22c55e, emissive: 0x22c55e, emissiveIntensity: 0.9, roughness: 0.4 });
  mats3.strip = stripMat;
  const litProb = kind === "hostel" ? 0.6 : kind === "workshop" ? 0.25 : 0.5;
  for (let k = 0; k < b.poly.length; k++) {
    const e = outward(b.poly, k, cx, cz);
    if (e.len < 0.5) continue;
    const bays = Math.max(1, Math.round(e.len / 1.15));
    for (let f = 0; f < levels; f++) {
      for (let s = 0; s < bays; s += 3) { // groups of up to three bays share a lit/dark state
        const n = Math.min(3, bays - s), t0 = s / bays, t1 = (s + n) / bays;
        const v = rnd() < litProb ? (rnd() < 0.6 ? 1 : 2) : 0;
        wb.add(`${kind}:${v}`, e.x1 + (e.x2 - e.x1) * t0, e.z1 + (e.z2 - e.z1) * t0, e.x1 + (e.x2 - e.x1) * t1, e.z1 + (e.z2 - e.z1) * t1, f * FH, (f + 1) * FH, e.nx, e.nz, n, 1);
      }
    }
    wb.add("plinth", e.x1, e.z1, e.x2, e.z2, 0, 0.1, e.nx, e.nz, 1, 1);
    wb.add("cornice", e.x1, e.z1, e.x2, e.z2, H - 0.09, H + 0.05, e.nx, e.nz, 1, 1);
    // parapet and the glowing status strip on top of it
    const px = e.nx * -0.04, pz = e.nz * -0.04;
    wb.add("parapet", e.x1 + px, e.z1 + pz, e.x2 + px, e.z2 + pz, H + 0.05, H + 0.3, e.nx, e.nz, 1, 1);
    wb.add("strip", e.x1 + px * 0.2, e.z1 + pz * 0.2, e.x2 + px * 0.2, e.z2 + pz * 0.2, H + 0.3, H + 0.37, e.nx, e.nz, 1, 1);
  }
  const body = wb.meshes(mats3, { type: "building", id: b.id });
  body.forEach((m) => { g.add(m); pickables.push(m); });
  const roof = prism(b.poly, 0.06, mat(0x9aa0aa, { roughness: 0.95 }), H + 0.02);
  roof.userData = { type: "building", id: b.id };
  g.add(roof);
  pickables.push(roof);
  roofs[b.id] = { material: stripMat };

  // roof furniture: water tanks, air-conditioning units, stair head-room, a few solar arrays
  const wantTanks = kind === "hostel" ? 3 : kind === "academic" ? 2 : 1, spot = () => {
    for (let t = 0; t < 40; t++) {
      const x = cx + (rnd() - 0.5) * (b.size[0] || 6), z = cz + (rnd() - 0.5) * (b.size[1] || 6);
      if (inside(x, z, b.poly) && b.poly.every(([px, pz], i) => { const e = outward(b.poly, i, cx, cz); return Math.abs((x - e.mx) * e.nx + (z - e.mz) * e.nz) > 0.9 || Math.hypot(x - e.mx, z - e.mz) > e.len; })) return [x, z];
    }
    return null;
  };
  for (let i = 0; i < wantTanks; i++) { const p = spot(); if (p) roofBits.tanks.push([p[0], H + 0.3, p[1]]); }
  for (let i = 0; i < 3 + levels; i++) { const p = spot(); if (p) roofBits.units.push([p[0], H + 0.2, p[1], rnd() * 3]); }
  { const p = spot(); if (p && kind !== "cafe") roofBits.stairs.push([p[0], H + 0.35, p[1]]); }
  if ((kind === "academic" || kind === "hall") && rnd() < 0.7) for (let i = 0; i < 6; i++) { const p = spot(); if (p) roofBits.panels.push([p[0], H + 0.2, p[1]]); }

  // main entrance on the longest wall: steps, two columns and a purple canopy
  if (kind !== "workshop" || b.name.includes("Workshop Block")) {
    let best = 0, e0 = null;
    for (let k = 0; k < b.poly.length; k++) { const e = outward(b.poly, k, cx, cz); if (e.len > best) { best = e.len; e0 = e; } }
    if (e0 && best > 3) {
      const ang = -Math.atan2(e0.z2 - e0.z1, e0.x2 - e0.x1), ox = e0.nx, oz = e0.nz, w = Math.min(2.6, best * 0.4);
      const ent = new THREE.Group();
      ent.position.set(e0.mx + ox * 0.5, 0, e0.mz + oz * 0.5);
      ent.rotation.y = ang;
      ent.add(box(w, 0.08, 1.2, mat(0xcfc8b8), 0, 0.04, 0), box(w, 0.07, 1.0, purple, 0, 0.62, 0.0), box(w - 0.1, 0.35, 0.05, mat(0x3b82f6, { emissive: 0x1d4ed8, emissiveIntensity: 0.2 }), 0, 0.3, -0.5));
      for (const s of [-1, 1]) ent.add(box(0.1, 0.58, 0.1, white, s * (w / 2 - 0.1), 0.33, 0.42));
      g.add(ent);
    }
  }
  if (kind === "hostel") { // balcony ledges along the long walls
    for (let k = 0; k < b.poly.length; k++) {
      const e = outward(b.poly, k, cx, cz);
      if (e.len < 5) continue;
      for (let f = 1; f < levels; f++) {
        const l = box(e.len - 0.4, 0.04, 0.3, white, e.mx + e.nx * 0.15, f * FH + 0.02, e.mz + e.nz * 0.15);
        l.rotation.y = -Math.atan2(e.z2 - e.z1, e.x2 - e.x1);
        l.castShadow = true;
        g.add(l);
      }
    }
  }
  if (kind === "workshop") { // ribbed roof lights across the shed
    const xs = b.poly.map((p) => p[0]), zs = b.poly.map((p) => p[1]);
    for (let x = Math.min(...xs) + 1.2; x < Math.max(...xs) - 0.8; x += 1.6) for (let z = Math.min(...zs) + 1; z < Math.max(...zs) - 0.5; z += 3.2) {
      if (inside(x, z, b.poly) && inside(x + 0.7, z + 1.4, b.poly)) g.add(box(1.0, 0.14, 2.6, steel, x, H + 0.13, z + 0.9));
    }
  }
  const lab = label("", 0, "label3d pill");
  lab.position.set(cx, H + 2.4, cz);
  lab.element.dataset.p = String(Math.round(levels * (b.size[0] * b.size[1])));
  bLabels[b.id] = lab;
  g.add(lab);
  // soft contact shadow so the block sits on the ground
  const cs = prism(b.poly.map(([x, z]) => [cx + (x - cx) * 1.07, cz + (z - cz) * 1.07]), 0.01, new THREE.MeshBasicMaterial({ color: 0x1b3a1f, transparent: true, opacity: 0.32 }), 0.025);
  cs.castShadow = cs.receiveShadow = false;
  campusG.add(cs);
  return g;
}
function instanced(geo, material, list, place, colors) {
  if (!list.length) return null;
  const im = new THREE.InstancedMesh(geo, material, list.length), o = new THREE.Object3D();
  list.forEach((item, i) => { place(o, item); o.updateMatrix(); im.setMatrixAt(i, o.matrix); if (colors) im.setColorAt(i, new THREE.Color(colors(item, i))); });
  im.castShadow = true;
  im.receiveShadow = true;
  campusG.add(im);
  return im;
}

// the campus is hundreds of small boxes and strips; merge the ones that never move, per material, so it costs a few dozen draw calls
function bake() {
  campusG.updateMatrixWorld(true);
  const lists = new Map(), gone = [];
  campusG.traverse((o) => {
    if (!o.isMesh || o.isInstancedMesh || o.userData.type || o.userData.keep) return;
    for (let p = o.parent; p && p !== campusG; p = p.parent) if (p.userData.keep) return;
    let g = o.geometry.clone();
    if (g.index) g = g.toNonIndexed();
    g.applyMatrix4(o.matrixWorld);
    for (const name of Object.keys(g.attributes)) if (!["position", "normal", "uv"].includes(name)) g.deleteAttribute(name);
    if (!g.attributes.uv) g.setAttribute("uv", new THREE.Float32BufferAttribute(new Float32Array((g.attributes.position.count) * 2), 2));
    const key = `${o.material.uuid}|${o.castShadow ? "s" : ""}${o.receiveShadow ? "r" : ""}`;
    if (!lists.has(key)) lists.set(key, { material: o.material, cast: o.castShadow, receive: o.receiveShadow, geos: [] });
    lists.get(key).geos.push(g);
    gone.push(o);
  });
  gone.forEach((o) => o.parent.remove(o));
  for (const l of lists.values()) {
    const m = new THREE.Mesh(mergeGeometries(l.geos, false), l.material);
    m.castShadow = l.cast;
    m.receiveShadow = l.receive;
    campusG.add(m);
  }
}

function buildCampus() {
  const G = CAMPUS_GEO, L = G.landmarks;
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(520, 440), new THREE.MeshStandardMaterial({ map: grassTex, roughness: 1 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.set(CENTER.x, -0.02, CENTER.z);
  ground.receiveShadow = true;
  campusG.add(ground);
  const xs = G.buildings.flatMap((b) => b.poly.map((p) => p[0])), zs = G.buildings.flatMap((b) => b.poly.map((p) => p[1]));
  const lot = [[Math.min(...xs) - 14, Math.min(...zs) - 14], [Math.max(...xs) + 10, Math.min(...zs) - 14], [Math.max(...xs) + 10, Math.max(...zs) + 10], [Math.min(...xs) - 14, Math.max(...zs) + 10]];
  const lawn = prism(lot, 0.02, new THREE.MeshStandardMaterial({ color: 0x6aa85a, roughness: 1 }));
  lawn.castShadow = false;
  campusG.add(lawn);
  // surroundings: darker fields and a few distant tree rows so the campus does not float on a blank plane
  const rim = new THREE.Mesh(new THREE.RingGeometry(150, 260, 48), new THREE.MeshBasicMaterial({ color: 0x24472b, transparent: true, opacity: 0.55 }));
  rim.rotation.x = -Math.PI / 2;
  rim.position.set(CENTER.x, 0.0, CENTER.z);
  campusG.add(rim);

  // roads with kerbs, centre lines and lamp posts
  asphaltTex.repeat.set(1, 1);
  const asphalt = new THREE.MeshStandardMaterial({ map: asphaltTex, roughness: 0.95 }), kerb = mat(0xcfd2d8), dash = mat(0xf1f1d9);
  const allRoads = [...G.roads, [L.main_gate, L.oval], [L.temple_gate, L.oval]];
  for (const r of allRoads) {
    campusG.add(ribbon(r, 2.0, kerb, 0.035), ribbon(r, 1.7, asphalt, 0.05));
    for (let i = 1; i < r.length; i++) { // dashed centre line
      const [x1, z1] = r[i - 1], [x2, z2] = r[i], len = Math.hypot(x2 - x1, z2 - z1), a = -Math.atan2(z2 - z1, x2 - x1);
      for (let d = 0.8; d < len - 0.8; d += 1.6) { const m = box(0.7, 0.01, 0.06, dash, x1 + ((x2 - x1) * d) / len, 0.08, z1 + ((z2 - z1) * d) / len); m.rotation.y = a; m.castShadow = false; campusG.add(m); }
    }
  }
  const lampPts = [];
  for (const r of allRoads) for (let i = 1; i < r.length; i++) {
    const [x1, z1] = r[i - 1], [x2, z2] = r[i], len = Math.hypot(x2 - x1, z2 - z1) || 1, nx = -(z2 - z1) / len, nz = (x2 - x1) / len;
    for (let d = 3; d < len - 2; d += 9) lampPts.push([x1 + ((x2 - x1) * d) / len + nx * 1.25, z1 + ((z2 - z1) * d) / len + nz * 1.25]);
  }
  instanced(new THREE.CylinderGeometry(0.04, 0.05, 1.5, 6), mat(0x4b5563), lampPts, (o, p) => { o.position.set(p[0], 0.75, p[1]); o.scale.set(1, 1, 1); o.rotation.set(0, 0, 0); });
  instanced(new THREE.SphereGeometry(0.1, 8, 6), lampMat, lampPts, (o, p) => { o.position.set(p[0], 1.55, p[1]); o.scale.set(1, 1, 1); o.rotation.set(0, 0, 0); });
  lampPts.forEach((p) => { const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: haloTex, color: 0xffe9a8, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending })); s.scale.set(2.6, 2.6, 1); s.position.set(p[0], 1.55, p[1]); campusG.add(s); lamps.halos.push(s); });

  // paths from every block to the nearest road, a stone plaza round the fountain and the flag
  const roadPts = allRoads.flat(), paving = new THREE.MeshStandardMaterial({ map: pavingTex, roughness: 0.9 });
  for (const b of buildings) {
    const [bx, bz] = centroidOf(b.poly), tgt = roadPts.reduce((a, p) => (Math.hypot(p[0] - bx, p[1] - bz) < Math.hypot(a[0] - bx, a[1] - bz) ? p : a)), from = b.poly.reduce((a, p) => (Math.hypot(p[0] - tgt[0], p[1] - tgt[1]) < Math.hypot(a[0] - tgt[0], a[1] - tgt[1]) ? p : a));
    if (Math.hypot(from[0] - tgt[0], from[1] - tgt[1]) < 14) campusG.add(ribbon([from, tgt], 0.7, paving, 0.04));
  }
  for (const [p, r] of [[L.flag, 3.2], [L.oval, 0]]) if (r) { const m = new THREE.Mesh(new THREE.CircleGeometry(r, 40), paving); m.rotation.x = -Math.PI / 2; m.position.set(p[0], 0.045, p[1]); m.receiveShadow = true; campusG.add(m); }
  // low compound wall with brand-coloured pillars, open at the two gates
  const wallM = mat(0xefe9dc), pillar = purple;
  for (let i = 0; i < 4; i++) {
    const a = lot[i], c = lot[(i + 1) % 4], len = Math.hypot(c[0] - a[0], c[1] - a[1]);
    for (let d = 0; d < len; d += 3) {
      const x = a[0] + ((c[0] - a[0]) * d) / len, z = a[1] + ((c[1] - a[1]) * d) / len;
      if (Math.hypot(x - L.main_gate[0], z - L.main_gate[1]) < 7 || Math.hypot(x - L.temple_gate[0], z - L.temple_gate[1]) < 7) continue;
      const seg = box(3.05, 0.34, 0.12, wallM, x + ((c[0] - a[0]) / len) * 1.5, 0.17, z + ((c[1] - a[1]) / len) * 1.5);
      seg.rotation.y = -Math.atan2(c[1] - a[1], c[0] - a[0]);
      campusG.add(seg, box(0.2, 0.46, 0.2, pillar, x, 0.23, z));
    }
  }

  // playground: running track, pitches, pool, gym
  const pg = L.playground;
  campusG.add(prism(pg, 0.05, mat(0xb4553e)));
  const pc = centroidOf(pg), shrink = pg.map(([x, z]) => [pc[0] + (x - pc[0]) * 0.86, pc[1] + (z - pc[1]) * 0.8]);
  campusG.add(prism(shrink, 0.07, new THREE.MeshStandardMaterial({ color: 0x5fa052, roughness: 1 })));
  for (const p of G.pitches) { const m = prism(p, 0.08, mat(0x4f9a5a)); campusG.add(m, edges(m, 0xffffff, 0.8)); }
  const poolMat = new THREE.MeshPhysicalMaterial({ color: 0x1fa5e8, roughness: 0.05, metalness: 0.1, clearcoat: 1, emissive: 0x0ea5e9, emissiveIntensity: 0.25 });
  campusG.add(box(7, 0.1, 4.2, mat(0xe8edf2), L.pool[0], 0.05, L.pool[1]), box(6, 0.12, 3.2, poolMat, L.pool[0], 0.11, L.pool[1]));
  for (let i = -2; i <= 2; i++) campusG.add(box(5.8, 0.01, 0.04, white, L.pool[0], 0.18, L.pool[1] + i * 0.55));
  const gym = box(4.5, 2.0, 3.4, mat(0x475569), L.gym[0], 1.0, L.gym[1]);
  const gymTag = label("Gym", 0, "label3d pill");
  gymTag.position.set(L.gym[0], 3.2, L.gym[1]);
  campusG.add(gym, box(4.7, 0.18, 3.6, mat(0x1f2937), L.gym[0], 2.1, L.gym[1]), box(3.2, 0.9, 0.05, mat(0x93c5fd, { emissive: 0x3b82f6, emissiveIntensity: 0.3 }), L.gym[0], 1.0, L.gym[1] + 1.72), gymTag);

  // buildings
  for (const b of buildings) { const g = buildBuildingMesh(b); campusG.add(g); }
  // the rooftop kit, drawn once for the whole campus
  instanced(new THREE.CylinderGeometry(0.22, 0.22, 0.5, 12), mat(0x2563eb, { roughness: 0.5 }), roofBits.tanks, (o, p) => { o.position.set(p[0], p[1] + 0.2, p[2]); o.scale.set(1, 1, 1); o.rotation.set(0, 0, 0); });
  instanced(new THREE.BoxGeometry(0.5, 0.34, 0.4), steel, roofBits.units, (o, p) => { o.position.set(p[0], p[1] + 0.1, p[2]); o.rotation.set(0, p[3], 0); o.scale.set(1, 1, 1); });
  instanced(new THREE.BoxGeometry(0.9, 0.5, 0.9), mat(0xe4e0d4), roofBits.stairs, (o, p) => { o.position.set(p[0], p[1] + 0.2, p[2]); o.rotation.set(0, 0, 0); o.scale.set(1, 1, 1); });
  instanced(new THREE.BoxGeometry(0.9, 0.04, 0.55), mat(0x1e3a8a, { metalness: 0.5, roughness: 0.25 }), roofBits.panels, (o, p) => { o.position.set(p[0], p[1] + 0.12, p[2]); o.rotation.set(-0.35, 0, 0); o.scale.set(1, 1, 1); });

  // landmarks
  const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.08, 0.1, 9, 10), mat(0xd1d5db, { metalness: 0.8, roughness: 0.3 }));
  pole.position.set(L.flag[0], 4.5, L.flag[1]);
  pole.castShadow = true;
  const fc = canvasOf(90, 60, (c) => {
    [["#ff9933", 0], ["#ffffff", 20], ["#138808", 40]].forEach(([col, y]) => { c.fillStyle = col; c.fillRect(0, y, 90, 20); });
    c.strokeStyle = "#000080"; c.lineWidth = 1.5; c.beginPath(); c.arc(45, 30, 8, 0, Math.PI * 2); c.stroke();
    for (let i = 0; i < 24; i++) { c.beginPath(); c.moveTo(45, 30); c.lineTo(45 + 8 * Math.cos((i * Math.PI) / 12), 30 + 8 * Math.sin((i * Math.PI) / 12)); c.stroke(); }
  });
  const flagGeo = new THREE.PlaneGeometry(3, 2, 18, 6);
  const flag = new THREE.Mesh(flagGeo, new THREE.MeshStandardMaterial({ map: tex(fc), side: THREE.DoubleSide }));
  flag.userData.keep = true; // animated: not baked
  flag.position.set(L.flag[0] + 1.55, 7.9, L.flag[1]);
  Object.assign(flagParts, { geo: flagGeo, base: flagGeo.attributes.position.array.slice() });
  campusG.add(pole, flag, box(1.6, 0.4, 1.6, mat(0xb8b2a6), L.flag[0], 0.2, L.flag[1]));
  const gate = (p, name, temple, ang) => {
    const gg = new THREE.Group();
    gg.position.set(p[0], 0, p[1]);
    gg.rotation.y = ang;
    gg.add(box(0.9, 3.6, 0.9, mat(0xf5e6c8), -3.2, 1.8, 0), box(0.9, 3.6, 0.9, mat(0xf5e6c8), 3.2, 1.8, 0), box(8.2, 0.9, 1.1, purple, 0, 3.9, 0), box(8.4, 0.12, 1.3, white, 0, 4.4, 0));
    for (const s of [-1, 1]) gg.add(box(0.5, 0.5, 0.5, mat(0xe9c46a), s * 3.2, 3.9 + 0.7, 0));
    if (temple) for (let i = 0; i < 6; i++) gg.add(box(3.8 - i * 0.55, 0.85, 2.5 - i * 0.32, mat([0xd9534f, 0xf0ad4e, 0xe8c07d][i % 3]), 0, 4.9 + i * 0.85, 0), box(0.12, 0.45, 0.12, mat(0xe9c46a), 0, 4.9 + 6 * 0.85 + 0.3, 0));
    const t = label(name, 0, "label3d pill");
    t.position.set(0, temple ? 11.4 : 5.7, 0);
    gg.add(t);
    campusG.add(gg);
  };
  gate(L.main_gate, "Main Gate", false, 0.2);
  gate(L.temple_gate, "Temple Gate", true, 0.9);
  const oval = new THREE.Mesh(new THREE.CircleGeometry(1, 40), new THREE.MeshStandardMaterial({ color: 0x5fae5a, roughness: 1 }));
  oval.rotation.x = -Math.PI / 2;
  oval.scale.set(3.2, 5.5, 1);
  oval.position.set(L.oval[0], 0.07, L.oval[1]);
  campusG.add(oval);
  const plane = new THREE.Group(), blue = mat(0x1e3a8a);
  const body = new THREE.Mesh(new THREE.CylinderGeometry(0.45, 0.35, 7, 20), white);
  body.rotation.z = Math.PI / 2;
  const nose = new THREE.Mesh(new THREE.ConeGeometry(0.45, 1.2, 20), white);
  nose.rotation.z = -Math.PI / 2;
  nose.position.x = 4.1;
  plane.add(body, nose, box(1.8, 0.1, 8, white, 0.3, 0, 0), box(0.9, 0.08, 3, white, -3.2, 0.3, 0), box(0.9, 1.4, 0.08, blue, -3.3, 0.9, 0), box(6, 0.12, 0.92, blue, 0, 0.1, 0), box(1.0, 0.25, 0.7, mat(0x93c5fd), 3.0, 0.38, 0));
  plane.position.set(L.aircraft[0], 1.4, L.aircraft[1]);
  plane.rotation.y = 0.6;
  plane.traverse((o) => (o.castShadow = true));
  const planeTag = label("Air craft area", 0, "label3d pill");
  planeTag.position.set(L.aircraft[0], 4.4, L.aircraft[1]);
  campusG.add(new THREE.Mesh(new THREE.BoxGeometry(11, 0.05, 11), mat(0xa8adb8)), plane, planeTag);
  campusG.children[campusG.children.length - 3].position.set(L.aircraft[0], 0.05, L.aircraft[1]);

  // college buses: all parked in the yard beside the Transport Workshop, none inside the campus
  const busGeo = new THREE.BoxGeometry(2.8, 0.8, 0.68), glassGeo = new THREE.BoxGeometry(2.5, 0.28, 0.7), roofGeo = new THREE.BoxGeometry(2.8, 0.06, 0.68);
  const parked = [];
  for (const z of [-83, -79.4, -75.8]) for (let i = 0; i < 7; i++) parked.push([24.5 + i * 3.5, z, 0]);
  const yard = new THREE.Mesh(new THREE.PlaneGeometry(27, 12), mat(0x3a3f4b));
  yard.rotation.x = -Math.PI / 2; yard.position.set(33.5, 0.04, -79.4); yard.receiveShadow = true;
  campusG.add(yard);
  for (const [geo, m, dy] of [[busGeo, mat(0xf2b705, { roughness: 0.5 }), 0], [glassGeo, mat(0x1f2937, { roughness: 0.15, metalness: 0.5 }), 0.18], [roofGeo, mat(0xfafafa), 0.43]]) {
    instanced(geo, m, parked, (o, p) => { o.position.set(p[0], 0.5 + dy, p[1]); o.rotation.set(0, p[2], 0); o.scale.set(1, 1, 1); });
  }
  const busTag = label(`Transport yard · ${parked.length} buses`, 0, "label3d pill");
  busTag.position.set(33.5, 3, -79.4);
  campusG.add(busTag);
  // cars along the roads
  const CARS = [0xe11d48, 0x2563eb, 0xf8fafc, 0x111827, 0x94a3b8, 0x16a34a, 0xf59e0b];
  const cars = [];
  for (let i = 0; i < 26; i++) {
    const r = pick(allRoads.filter((r) => r.length > 1)), s = Math.floor(rnd() * (r.length - 1)), t = rnd(), [x1, z1] = r[s], [x2, z2] = r[s + 1], len = Math.hypot(x2 - x1, z2 - z1) || 1;
    if (len < 6) continue;
    const side = rnd() < 0.5 ? 1 : -1;
    cars.push([x1 + (x2 - x1) * t - ((z2 - z1) / len) * 1.15 * side, z1 + (z2 - z1) * t + ((x2 - x1) / len) * 1.15 * side, -Math.atan2(z2 - z1, x2 - x1), pick(CARS)]);
  }
  instanced(new THREE.BoxGeometry(0.95, 0.28, 0.45), mat(0xffffff, { roughness: 0.35, metalness: 0.4 }), cars, (o, p) => { o.position.set(p[0], 0.2, p[1]); o.rotation.set(0, p[2], 0); o.scale.set(1, 1, 1); }, (p) => p[3]);
  instanced(new THREE.BoxGeometry(0.5, 0.2, 0.4), mat(0x1f2937, { roughness: 0.1, metalness: 0.5 }), cars, (o, p) => { o.position.set(p[0] - Math.cos(p[2]) * 0.0, 0.43, p[1]); o.rotation.set(0, p[2], 0); o.scale.set(1, 1, 1); });
  // people
  const centres = buildings.map((b) => centroidOf(b.poly)), SHIRT = [0x1e3a8a, 0xbe185d, 0x047857, 0x7c3aed, 0xb45309, 0x0e7490, 0x4b5563];
  const people = new THREE.InstancedMesh(new THREE.CapsuleGeometry(0.1, 0.34, 3, 6), mat(0xffffff), 80), heads = new THREE.InstancedMesh(new THREE.SphereGeometry(0.095, 8, 6), mat(0xffffff), 80);
  for (let i = 0; i < 80; i++) {
    people.setColorAt(i, new THREE.Color(pick(SHIRT)));
    heads.setColorAt(i, new THREE.Color(pick([0xd9a66b, 0xc68642, 0x8d5524, 0xe0ac69, 0x6f4a2f])));
    movers.push({ person: i, path: [pick(centres), pick(centres)], t: rnd(), speed: 0.006 + rnd() * 0.006, y: 0.32, dir: 1 });
  }
  people.castShadow = heads.castShadow = true;
  campusG.add(people, heads);
  movers.people = people;
  movers.heads = heads;

  // vegetation: three tree shapes with colour variety, hedges along walls and lamps' neighbours
  const spots = [];
  const nearRoad = (x, z) => allRoads.some((r) => r.some(([rx, rz]) => Math.hypot(rx - x, rz - z) < 3.4));
  for (let i = 0; i < 2600 && spots.length < 420; i++) {
    const far = i > 1500, x = CENTER.x - (far ? 170 : 95) + rnd() * (far ? 340 : 190), z = CENTER.z - (far ? 130 : 75) + rnd() * (far ? 260 : 150);
    if (buildings.some((b) => inside(x, z, b.poly) || b.poly.some(([px, pz]) => Math.hypot(px - x, pz - z) < 2.6)) || inside(x, z, pg) || nearRoad(x, z)) continue;
    if (Math.hypot(x - L.aircraft[0], z - L.aircraft[1]) < 8 || Math.hypot(x - L.flag[0], z - L.flag[1]) < 4) continue;
    if (far && x > Math.min(...xs) - 16 && x < Math.max(...xs) + 12 && z > Math.min(...zs) - 16 && z < Math.max(...zs) + 12) continue;
    spots.push([x, z, 0.8 + rnd() * 0.9, Math.floor(rnd() * 10)]);
  }
  const round = spots.filter((s) => s[3] < 6), cone = spots.filter((s) => s[3] >= 6 && s[3] < 8), slim = spots.filter((s) => s[3] >= 8);
  const GREENS = [0x2f7d4a, 0x3d8f4f, 0x2a6b3f, 0x4a9a55, 0x5fa052, 0x22633a], FLOWER = [0xf472b6, 0xfb923c, 0xfacc15];
  const trunkM = mat(0x6b4f2a), place = (h, sy) => (o, p) => { o.position.set(p[0], h * p[2], p[1]); o.scale.set(p[2], p[2] * (sy || 1), p[2]); o.rotation.set(0, p[3], 0); };
  instanced(new THREE.CylinderGeometry(0.13, 0.19, 1.2, 6), trunkM, spots, (o, p) => { o.position.set(p[0], 0.6 * p[2], p[1]); o.scale.set(p[2], p[2], p[2]); o.rotation.set(0, 0, 0); });
  instanced(new THREE.IcosahedronGeometry(1.15, 1), mat(0xffffff, { flatShading: true, roughness: 1 }), round, place(2.1), (p, i) => (i % 11 === 0 ? pick(FLOWER) : pick(GREENS)));
  instanced(new THREE.ConeGeometry(0.95, 2.8, 8), mat(0xffffff, { flatShading: true, roughness: 1 }), cone, place(2.5), () => pick([0x1f5a35, 0x2a6b3f]));
  instanced(new THREE.IcosahedronGeometry(0.75, 1), mat(0xffffff, { flatShading: true, roughness: 1 }), slim, place(2.6, 2.1), () => pick(GREENS));
  const hedges = [];
  for (const b of buildings) for (let k = 0; k < b.poly.length; k++) {
    const [cx, cz] = centroidOf(b.poly), e = outward(b.poly, k, cx, cz);
    if (e.len < 4 || b.kind === "workshop") continue;
    for (let d = 1; d < e.len - 1; d += 1.0) { if (Math.abs(d - e.len / 2) < 1.8) continue; hedges.push([e.x1 + ((e.x2 - e.x1) * d) / e.len + e.nx * 0.5, e.z1 + ((e.z2 - e.z1) * d) / e.len + e.nz * 0.5, 0.75 + rnd() * 0.5]); }
  }
  instanced(new THREE.IcosahedronGeometry(0.38, 0), mat(0xffffff, { flatShading: true, roughness: 1 }), hedges, (o, p) => { o.position.set(p[0], 0.28, p[1]); o.scale.set(p[2], p[2] * 0.8, p[2]); o.rotation.set(0, 0, 0); }, () => pick([0x2f7d4a, 0x3d8f4f, 0x4a9a55]));

  bake();
  const north = label("N ↑", 0, "label3d pill");
  north.position.set(CENTER.x, 1, CENTER.z - 72);
  campusG.add(north);
  const credit = document.createElement("div");
  credit.className = "credit";
  credit.textContent = "Building outlines © OpenStreetMap contributors · other blocks from the college's campus map";
  stage.append(credit);
  refreshCampus();
}

// moving buses and people; called every frame with the seconds since the last one
const tmp = new THREE.Object3D();
function moveThings(dt) {
  for (const m of movers) {
    const p = m.path, segs = p.length - 1;
    m.t += m.speed * dt * m.dir * (m.person !== undefined ? 1 : 2 / segs);
    if (m.t > 1 || m.t < 0) { m.dir *= -1; m.t = Math.min(1, Math.max(0, m.t)); }
    const f = m.t * segs, i = Math.min(Math.floor(f), segs - 1), k = f - i;
    const [x1, z1] = p[i], [x2, z2] = p[i + 1];
    const x = x1 + (x2 - x1) * k, z = z1 + (z2 - z1) * k, ang = -Math.atan2((z2 - z1) * m.dir, (x2 - x1) * m.dir);
    if (m.person !== undefined) {
      const bob = Math.abs(Math.sin(performance.now() / 160 + m.person)) * 0.03, px = x + Math.sin(m.person) * 0.6, pz = z + Math.cos(m.person) * 0.6;
      tmp.position.set(px, m.y + bob, pz); tmp.rotation.set(0, ang, 0); tmp.updateMatrix(); movers.people.setMatrixAt(m.person, tmp.matrix);
      tmp.position.set(px, m.y + 0.42 + bob, pz); tmp.updateMatrix(); movers.heads.setMatrixAt(m.person, tmp.matrix);
    } else {
      m.obj.position.set(x, m.y, z);
      m.obj.rotation.y = ang;
    }
  }
  if (movers.people) { movers.people.instanceMatrix.needsUpdate = true; movers.heads.instanceMatrix.needsUpdate = true; }
}

// day and night follow the real clock (or a preview chosen with the sun button)
let timeMode = "live";
const _moon = new THREE.Color(0x8fa8ff);
function daylight() {
  const now = new Date(), h = timeMode === "day" ? 11 : timeMode === "night" ? 21 : now.getHours() + now.getMinutes() / 60;
  const sunUp = Math.max(0, Math.sin(((h - 6) / 12) * Math.PI)); // 0 at night, 1 at noon
  const warm = 1 - Math.min(1, Math.abs(sunUp - 0.18) / 0.3); // 1 around sunrise and sunset
  sun.intensity = 0.15 + 2.0 * sunUp;
  sun.color.setHSL(0.1 - 0.03 * warm, 0.35 + 0.4 * warm, 0.8 + 0.1 * (1 - warm));
  const night = 1 - Math.min(1, sunUp * 3);
  hemi.intensity = 0.62 + 0.55 * sunUp; // moonlight keeps the campus readable at night
  hemi.color.set(0xe3ecf8).lerp(_moon, night * 0.8);
  fill.intensity = 0.3 + 0.5 * sunUp;
  fill.color.set(0xfff0dd).lerp(_moon, night);
  const a = ((h - 6) / 12) * Math.PI;
  sun.position.set(CENTER.x + Math.cos(a) * 130, 22 + 120 * sunUp, CENTER.z + 70);
  facadeLit.forEach((m) => (m.emissiveIntensity = 0.08 + 1.5 * night));
  lampMat.emissiveIntensity = 0.1 + 2.2 * night;
  lamps.halos.forEach((s) => (s.material.opacity = 0.75 * night));
  renderer.toneMappingExposure = 1.15 - 0.2 * sunUp;
  const sky = sunUp > 0.35 ? 0xcfe3f2 : sunUp > 0.05 ? 0x9b6d78 : 0x0d1224;
  scene.fog.color.setHex(sky);
  updateSky(sunUp, night);
  const pill = document.getElementById("sky");
  if (pill) pill.textContent = `${timeMode === "live" ? "Live time" : timeMode === "day" ? "Day preview" : "Night preview"} · ${sunUp > 0.05 ? "☀" : "☾"}`;
}

// a building is judged by the share of its devices that are down: one broken PC shouldn't paint the whole block red
function bState(b) {
  const ids = idsOfBuilding(b), t = tally(ids), down = (t.faulty + t.offline) / (ids.length || 1);
  return down >= 0.1 ? "faulty" : down >= 0.03 || t.warning / (ids.length || 1) >= 0.05 ? "warning" : "online";
}
function refreshCampus() {
  for (const b of buildings) {
    const ids = idsOfBuilding(b), t = tally(ids), s = bState(b);
    roofs[b.id].material.color.setHex(HEX[s]);
    roofs[b.id].material.emissive.setHex(HEX[s]);
    bLabels[b.id].element.innerHTML = `<i class="dot ${s}"></i>${esc(b.name)}`; // counts are in the tooltip and the side panel
  }
}

// ---------- building: floors pulled apart ----------
let roomBoxes = {}, dotMesh = null, dotIds = [];
// a room is judged by the share of its devices that are down: one broken PC in a lab of thirty is amber, not red
const roomState = (ids) => { const t = tally(ids), n = ids.length || 1, down = (t.faulty + t.offline) / n; return down >= 0.1 ? "faulty" : down >= 0.03 || t.warning / n >= 0.08 ? "warning" : "online"; };
const SLAB = new THREE.MeshStandardMaterial({ color: 0xe9edf5, roughness: 0.6 }), SHELL = new THREE.MeshStandardMaterial({ color: 0xbfd4ee, transparent: true, opacity: 0.16, roughness: 0.1, metalness: 0.3, side: THREE.DoubleSide, depthWrite: false });
function buildBuilding(b) {
  clear(buildingG);
  pickables = [];
  roomBoxes = {};
  dotIds = [];
  const w = Math.min(Math.max(Math.max(...b.size) * 0.8, 20), 36), d = Math.min(Math.max(Math.min(...b.size), 12), 18), GAP = 7.2;
  const floors = [...b.floors].sort((x, y) => floorNo(x.floor) - floorNo(y.floor));
  const dots = []; // one small cube per device, drawn on its room's floor
  floors.forEach((f, i) => {
    const y = i * GAP;
    const slab = box(w + 0.6, 0.4, d + 0.6, SLAB, 0, y, 0);
    buildingG.add(slab, edges(slab, 0x7d89a8, 0.55), box(w + 0.6, 0.1, 0.12, purple, 0, y + 0.25, d / 2 + 0.3));
    const tag = label(`<i class="dot ${roomState(f.rooms.flatMap((r) => r.devices))}"></i>${floorName(f.floor)}`, 0, "label3d pill");
    tag.position.set(-w / 2 - 4.2, y + 0.6, 0);
    buildingG.add(tag);
    const weights = f.rooms.map((r) => Math.sqrt(r.devices.length + 3));
    const total = weights.reduce((a, c) => a + c, 0);
    let x = -w / 2 + 0.5;
    f.rooms.forEach((r, k) => {
      const rw = (w - 1) * (weights[k] / total) - 0.5, s = roomState(r.devices), rd = d - 1.8;
      const m = box(rw, 2.6, rd, new THREE.MeshStandardMaterial({ color: HEX[s], transparent: true, opacity: 0.26, roughness: 0.15, metalness: 0.2, depthWrite: false }), x + rw / 2, y + 1.5, 0);
      m.userData = { type: "room", id: r.id };
      pickables.push(m);
      roomBoxes[r.id] = m;
      const tile = box(rw, 0.06, rd, new THREE.MeshStandardMaterial({ color: HEX[s], emissive: HEX[s], emissiveIntensity: 0.22, roughness: 0.6 }), x + rw / 2, y + 0.23, 0);
      tile.userData = { room: r.id };
      roomBoxes[`tile-${r.id}`] = tile;
      const lb = label(`<i class="dot ${s}"></i>${esc(r.name)}`, 0, "label3d pill");
      lb.position.set(x + rw / 2, y + 3.4, 0);
      buildingG.add(m, edges(m, 0xffffff, 0.7), tile, lb);
      // the devices as small cubes in a grid on the floor
      const n = r.devices.length, cols = Math.max(1, Math.min(n, Math.floor((rw - 0.8) / 0.55)));
      r.devices.forEach((id, j) => dots.push({ id, x: x + 0.5 + (j % cols) * ((rw - 1) / Math.max(cols, 1)) + 0.15, y: y + 0.45, z: -rd / 2 + 0.8 + Math.floor(j / cols) * 0.55 }));
      x += rw + 0.5;
    });
    if (i) for (const [cx, cz] of [[-w / 2, -d / 2], [w / 2, -d / 2], [-w / 2, d / 2], [w / 2, d / 2]]) buildingG.add(box(0.14, GAP - 0.4, 0.14, mat(0x6b7693), cx, y - GAP / 2, cz));
  });
  const top = floors.length * GAP - GAP + 3.4;
  const ghost = box(w + 0.6, top + 0.4, d + 0.6, SHELL, 0, (top + 0.4) / 2, 0);
  ghost.castShadow = false;
  buildingG.add(ghost, edges(ghost, 0x9fb6d8, 0.25));
  dotMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(0.3, 0.22, 0.3), new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xffffff, emissiveIntensity: 0.5, roughness: 0.4 }), dots.length);
  const o = new THREE.Object3D();
  dots.forEach((dt, i) => { o.position.set(dt.x, dt.y, dt.z); o.updateMatrix(); dotMesh.setMatrixAt(i, o.matrix); dotIds.push(dt.id); });
  dotMesh.castShadow = true;
  buildingG.add(dotMesh);
  refreshBuilding();
  return { h: floors.length * GAP, w };
}
const _c = new THREE.Color();
function refreshBuilding() {
  for (const [rid, m] of Object.entries(roomBoxes)) {
    if (!roomById[m.userData.room || rid]) continue;
    const r = roomById[m.userData.room || rid], s = roomState(r.devices);
    m.material.color.setHex(HEX[s]);
    if (m.material.emissive && m.userData.room) m.material.emissive.setHex(HEX[s]);
  }
  if (!dotMesh) return;
  dotIds.forEach((id, i) => { const st = state(byId[id]); dotMesh.setColorAt(i, _c.setHex(HEX[st])); });
  dotMesh.instanceColor.needsUpdate = true;
}

// ---------- room: desks, PCs, racks ----------
let picked = null, ring = null;
function statusMat(d) {
  const s = state(d);
  return new THREE.MeshStandardMaterial({ color: HEX[s], emissive: HEX[s], emissiveIntensity: s === "offline" ? 0.05 : 0.9 });
}
// the walls, windows, door and the small things that make a room look used
const floorTex = tex(canvasOf(128, 128, (c, w, h) => {
  c.fillStyle = "#d2d5dc"; c.fillRect(0, 0, w, h);
  for (let i = 0; i < 500; i++) { c.fillStyle = `rgba(${Math.random() < 0.5 ? "255,255,255" : "80,80,90"},.05)`; c.fillRect(Math.random() * w, Math.random() * h, 3, 3); }
  c.strokeStyle = "rgba(60,60,75,.45)"; c.lineWidth = 3; c.strokeRect(0, 0, w, h);
}), [1, 1]);
function roomDetails(RW, RD, r) {
  const wood = mat(0x8b5a2b), dark = mat(0x374151), glass = new THREE.MeshStandardMaterial({ color: 0x9ec8ee, emissive: 0x6aa8e0, emissiveIntensity: 0.4, roughness: 0.1, metalness: 0.3 });
  const zb = -RD / 2 + 0.12, xl = -RW / 2 + 0.12;
  roomG.add(box(RW, 0.2, 0.08, dark, 0, 0.1, zb), box(0.08, 0.2, RD, dark, xl, 0.1, 0));              // skirting
  roomG.add(box(RW, 0.22, 0.08, purple, 0, 3.55, zb), box(0.08, 0.22, RD, purple, xl, 3.55, 0));     // brand stripe
  const wins = Math.max(2, Math.floor(RD / 4.2));
  for (let i = 0; i < wins; i++) {                                                                      // windows on the left wall
    const z = -RD / 2 + (RD * (i + 0.5)) / wins;
    roomG.add(box(0.07, 1.9, 2.3, dark, xl + 0.01, 2.2, z), box(0.08, 1.7, 2.1, glass, xl + 0.03, 2.2, z), box(0.09, 1.7, 0.06, dark, xl + 0.04, 2.2, z), box(0.12, 0.09, 2.4, white, xl + 0.06, 1.2, z));
  }
  const dx = RW / 2 - 2.4;                                                                              // door on the back wall
  roomG.add(box(1.7, 3.1, 0.07, dark, dx, 1.55, zb + 0.01), box(1.5, 2.95, 0.09, wood, dx, 1.5, zb + 0.03), box(0.08, 0.3, 0.14, steel, dx + 0.55, 1.5, zb + 0.1), box(0.4, 0.5, 0.1, glass, dx, 2.4, zb + 0.09));
  roomG.add(box(2.1, 0.5, 0.36, white, -RW / 2 + 3.2, 3.2, zb + 0.16), box(0.12, 0.05, 0.02, mat(0x22c55e, { emissive: 0x22c55e, emissiveIntensity: 1 }), -RW / 2 + 2.5, 3.1, zb + 0.35)); // air conditioner
  const clock = new THREE.Mesh(new THREE.CylinderGeometry(0.34, 0.34, 0.06, 28), white);
  clock.rotation.x = Math.PI / 2;
  clock.position.set(RW / 2 - 5.2, 3.05, zb + 0.08);
  roomG.add(clock, box(0.03, 0.22, 0.02, dark, RW / 2 - 5.2, 3.14, zb + 0.13), box(0.16, 0.03, 0.02, dark, RW / 2 - 5.12, 3.05, zb + 0.13));
  roomG.add(box(1.9, 1.1, 0.05, mat(0xb08968), -RW / 2 + 1.6 + 5, 2.1, zb + 0.08));                  // notice board with pinned papers
  for (let i = 0; i < 5; i++) roomG.add(box(0.3, 0.4, 0.02, mat([0xffffff, 0xfde68a, 0xbfdbfe, 0xfbcfe8][i % 4]), -RW / 2 + 1.0 + 5 + i * 0.35, 2.1 + (i % 2) * 0.15, zb + 0.12));
  roomG.add(box(0.22, 0.7, 0.22, mat(0xdc2626), RW / 2 - 0.8, 0.5, zb + 0.18));                       // fire extinguisher
  roomG.add(box(1.8, 0.08, 0.9, wood, 3, 1.0, -RD / 2 + 2.2), box(0.08, 1.0, 0.08, dark, 2.2, 0.5, -RD / 2 + 2.2), box(0.08, 1.0, 0.08, dark, 3.8, 0.5, -RD / 2 + 2.2)); // teacher's desk
  const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.16, 0.4, 10), mat(0xb45309)), leaf = new THREE.Mesh(new THREE.IcosahedronGeometry(0.4, 1), mat(0x2f9e52, { flatShading: true }));
  pot.position.set(RW / 2 - 1, 0.2, -RD / 2 + 1.4);
  leaf.position.set(RW / 2 - 1, 0.75, -RD / 2 + 1.4);
  pot.castShadow = leaf.castShadow = true;
  roomG.add(pot, leaf);
}
function buildRoom(r) {
  clear(roomG);
  pickables = [];
  Object.keys(live).forEach((k) => delete live[k]);
  const ds = r.devices.map((id) => byId[id]);
  const pcs = ds.filter((d) => d.kind === "PC"), racks = ds.filter((d) => ["Server", "Switch"].includes(d.kind));
  const cols = Math.min(6, Math.max(3, Math.ceil(Math.sqrt(pcs.length * 1.6))));
  const rows = Math.max(1, Math.ceil(pcs.length / cols));
  const RW = Math.max(16, cols * 3.4 + 6), RD = Math.max(12, rows * 2.9 + 8);
  const ft = floorTex.clone();
  ft.needsUpdate = true;
  ft.repeat.set(RW / 2.4, RD / 2.4);
  const floor = box(RW, 0.2, RD, new THREE.MeshStandardMaterial({ map: ft, roughness: 0.35, metalness: 0.05 }), 0, -0.1, 0);
  roomG.add(floor, box(RW, 4.2, 0.2, mat(0xf1ece0), 0, 2, -RD / 2), box(0.2, 4.2, RD, mat(0xece6d8), -RW / 2, 2, 0));
  roomG.add(box(6, 2.6, 0.08, mat(0xf8fafc, { emissive: 0xdbeafe, emissiveIntensity: 0.35 }), 0, 2.4, -RD / 2 + 0.15));
  roomDetails(RW, RD, r);
  const tag = (m, d) => { m.userData = { type: "device", id: d.id }; pickables.push(m); return m; };
  pcs.forEach((d, i) => {
    const c = i % cols, rr = Math.floor(i / cols);
    const x = -((cols - 1) * 3.4) / 2 + c * 3.4, z = -RD / 2 + 4.5 + rr * 2.9;
    const g = new THREE.Group();
    g.position.set(x, 0, z);
    g.add(box(2.6, 0.08, 1.1, mat(0x8b6a4e), 0, 1.5, 0));
    for (const [lx, lz] of [[-1.2, -0.45], [1.2, -0.45], [-1.2, 0.45], [1.2, 0.45]]) g.add(box(0.07, 1.5, 0.07, mat(0x3f3f46), lx, 0.75, lz));
    const sm = statusMat(d);
    live[d.id] = [sm];
    g.add(tag(box(1.15, 0.72, 0.07, mat(0x111827), 0, 2.08, -0.3), d), tag(box(1.05, 0.62, 0.02, sm, 0, 2.08, -0.255), d));
    g.add(box(0.12, 0.25, 0.12, mat(0x111827), 0, 1.66, -0.33), box(0.6, 0.03, 0.22, mat(0x1f2937), 0, 1.56, 0.1));
    const tower = tag(box(0.32, 0.8, 0.75, mat(0x1f2937), 1.0, 0.4, 0), d);
    const led = box(0.02, 0.05, 0.05, sm, 0.84, 0.7, 0.3);
    g.add(tower, tag(led, d));
    g.add(box(0.9, 0.08, 0.9, mat(0x334155), 0, 0.95, 0.95), box(0.9, 0.9, 0.08, mat(0x334155), 0, 1.4, 1.38), box(0.08, 0.95, 0.08, mat(0x52525b), 0, 0.47, 0.95));
    roomG.add(g);
  });
  racks.forEach((d, i) => {
    const sm = statusMat(d);
    live[d.id] = [sm];
    const x = -RW / 2 + 1.2 + (i % 6) * 1.3, z = RD / 2 - 1.6 - Math.floor(i / 6) * 1.6;
    roomG.add(tag(box(1.0, d.kind === "Server" ? 2.6 : 2.0, 1.0, mat(0x111827), x, d.kind === "Server" ? 1.3 : 1.0, z), d));
    for (let k = 0; k < (d.kind === "Server" ? 6 : 3); k++) roomG.add(tag(box(0.7, 0.05, 0.02, sm, x, 0.5 + k * 0.35, z - 0.51), d));
  });
  ds.filter((d) => !["PC", "Server", "Switch"].includes(d.kind)).forEach((d, i) => {
    const sm = statusMat(d);
    live[d.id] = [sm];
    if (d.kind === "AP") {
      const ap = new THREE.Mesh(new THREE.CylinderGeometry(0.45, 0.45, 0.12, 24), mat(0xf1f5f9));
      ap.position.set(RW / 4, 4.1, 0);
      roomG.add(tag(ap, d), tag(box(0.12, 0.04, 0.12, sm, RW / 4, 4.02, 0), d));
    } else if (d.kind === "Projector") {
      roomG.add(tag(box(0.8, 0.3, 0.6, mat(0xe5e7eb), (i - 0.5) * 2, 3.7, -RD / 2 + 4), d), tag(box(0.15, 0.15, 0.03, sm, (i - 0.5) * 2, 3.7, -RD / 2 + 3.69), d));
    } else {
      roomG.add(box(1.4, 1.0, 0.8, mat(0x8b6a4e), RW / 2 - 1.2, 0.5, -RD / 2 + 1.5 + i * 1.6));
      roomG.add(tag(box(0.9, 0.5, 0.7, mat(0xe5e7eb), RW / 2 - 1.2, 1.25, -RD / 2 + 1.5 + i * 1.6), d), tag(box(0.15, 0.05, 0.05, sm, RW / 2 - 1.2, 1.3, -RD / 2 + 1.13 + i * 1.6), d));
    }
  });
  if (!ds.length) roomG.add(label("<b>No devices in this room</b>", 2));
  const real = ds.find((d) => d.real);
  if (real && real.reporting) {
    const m = pickables.find((p) => p.userData.id === real.id);
    if (m) {
      const p = new THREE.Vector3();
      m.getWorldPosition(p);
      const tag = label(`<b class="livetag">● LIVE</b>${esc(real.name)}`, 0);
      tag.position.set(p.x, 4.6, p.z);
      roomG.add(tag);
    }
  }
  ring = new THREE.Mesh(new THREE.RingGeometry(1.5, 1.75, 40), new THREE.MeshBasicMaterial({ color: 0xf5b800, side: THREE.DoubleSide }));
  ring.rotation.x = -Math.PI / 2;
  ring.visible = false;
  roomG.add(ring);
  return { RW, RD };
}
function refreshRoom() {
  for (const [id, ms] of Object.entries(live)) {
    const s = state(byId[id]);
    ms.forEach((m) => { m.color.setHex(HEX[s]); m.emissive.setHex(HEX[s]); m.emissiveIntensity = s === "offline" ? 0.05 : 0.9; });
  }
}
function markDevice(id) {
  picked = id;
  ring.visible = false;
  const m = pickables.find((p) => p.userData.id === id);
  if (!m) return;
  const p = new THREE.Vector3();
  m.getWorldPosition(p);
  ring.position.set(p.x, 0.05, p.z);
  ring.visible = true;
}

// ---------- camera, navigation ----------
let goal = null;
const fly = (pos, target) => (goal = { pos: new THREE.Vector3(...pos), target: new THREE.Vector3(...target) });
const nav = { view: "campus", b: null, r: null };
// a hidden group does not hide its floating labels, so switch those too
const setVisible = (g, v) => { g.visible = v; g.traverse((o) => { if (o.isCSS2DObject) o.visible = v; }); };
function show(view) {
  setVisible(campusG, view === "campus");
  setVisible(buildingG, view === "building");
  setVisible(roomG, view === "room");
  document.getElementById("hint").textContent = { campus: "Drag to rotate · scroll to zoom · click a building", building: "Click a room · floors are pulled apart so you can see inside", room: "Click a PC, rack or projector" }[view];
}
function goCampus() {
  Object.assign(nav, { view: "campus", b: null, r: null });
  pickables = [];
  campusG.traverse((o) => o.userData.type === "building" && pickables.push(o));
  show("campus");
  fly([CENTER.x - 100, 72, CENTER.z + 12], [CENTER.x + 4, 0, CENTER.z]); // same way round as the college's map: east at the top
  crumbs();
  panelCampus();
}
function goBuilding(bid) {
  const b = bById[bid];
  Object.assign(nav, { view: "building", b: bid, r: null });
  const { h, w } = buildBuilding(b);
  show("building");
  fly([w * 0.8, h * 0.5 + 7, w * 1.85], [w * 0.06, h / 2 - 1.5, 0]);
  crumbs();
  panelBuilding(b);
}
function goRoom(rid, deviceId) {
  const r = roomById[rid];
  picked = null;
  Object.assign(nav, { view: "room", b: r.building, r: rid });
  const { RW, RD } = buildRoom(r);
  show("room");
  fly([RW * 0.32, Math.max(RW, RD) * 0.6, RD * 0.82], [0, 1, 0]);
  crumbs();
  if (deviceId) selectDevice(deviceId); else panelRoom(r);
}
function selectDevice(id) {
  const d = byId[id];
  if (nav.r !== d.room) return goRoom(d.room, id);
  markDevice(id);
  panelDevice(d);
}
function crumbs() {
  const el = document.getElementById("crumbs");
  const parts = [["Campus", goCampus]];
  if (nav.b) parts.push([bById[nav.b].name, () => goBuilding(nav.b)]);
  if (nav.r) parts.push([roomById[nav.r].name, () => goRoom(nav.r)]);
  el.replaceChildren(...parts.map(([t, f]) => { const b = document.createElement("button"); b.textContent = t; b.onclick = () => { closeOverlay(); f(); }; return b; }));
}

// ---------- pointer ----------
const ray = new THREE.Raycaster(), mouse = new THREE.Vector2(), tip = document.getElementById("tip");
let hovered = null;
function hit(e) {
  const r = renderer.domElement.getBoundingClientRect();
  mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(mouse, camera);
  return ray.intersectObjects(pickables, false)[0]?.object;
}
renderer.domElement.addEventListener("pointermove", (e) => {
  const o = hit(e);
  hovered = o?.userData || null;
  setOutline(hovered?.type === "building" && nav.view === "campus" ? hovered.id : null);
  renderer.domElement.style.cursor = hovered ? "pointer" : "grab";
  if (!hovered) return (tip.hidden = true);
  const r = renderer.domElement.getBoundingClientRect();
  tip.style.left = `${e.clientX - r.left}px`;
  tip.style.top = `${e.clientY - r.top}px`;
  tip.hidden = false;
  if (hovered.type === "building") {
    const b = bById[hovered.id], t = tally(idsOfBuilding(b));
    tip.textContent = `${b.name}: ${idsOfBuilding(b).length} devices, ${t.offline + t.faulty} down · click to open`;
  }
  else if (hovered.type === "room") tip.textContent = `${roomById[hovered.id].name}: ${roomById[hovered.id].devices.length} devices`;
  else { const d = byId[hovered.id]; tip.textContent = `${d.name} · ${LABEL[state(d)]}`; }
});
let down = null;
renderer.domElement.addEventListener("pointerdown", (e) => (down = [e.clientX, e.clientY]));
renderer.domElement.addEventListener("pointerup", (e) => {
  if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return; // that was a drag, not a click
  const o = hit(e);
  if (!o) return;
  const u = o.userData;
  if (u.type === "building") goBuilding(u.id);
  else if (u.type === "room") goRoom(u.id);
  else if (u.type === "device") selectDevice(u.id);
});

// ---------- side panel ----------
const panel = document.getElementById("panel");
const kpis = (t, n) => `<div class="kpis">${Object.keys(HEX).map((k) => `<div class="kpi"><b style="color:${CSSC[k]}">${t[k]}</b><span>${LABEL[k]}</span></div>`).join("")}</div>
  <p class="muted" style="margin-top:8px">${n} devices</p>`;
function list(items) {
  const ul = document.createElement("ul");
  ul.className = "list";
  for (const [html, fn] of items) {
    const li = document.createElement("li");
    li.innerHTML = html;
    li.tabIndex = 0;
    li.onclick = fn;
    li.onkeydown = (e) => e.key === "Enter" && fn();
    ul.append(li);
  }
  return ul;
}
function panelCampus() {
  panel.innerHTML = `<h2>Rajalakshmi Engineering College</h2><p class="muted">Whole campus, live</p>${kpis(tally(allIds), allIds.length)}<h3>Buildings</h3>`;
  panel.append(list(buildings.map((b) => {
    const t = tally(idsOfBuilding(b));
    return [`<span><i class="dot ${bState(b)}"></i> ${esc(b.name)}</span><span class="muted">${idsOfBuilding(b).length} · ${t.offline + t.faulty} down</span>`, () => goBuilding(b.id)];
  })));
}
function panelBuilding(b) {
  const ids = idsOfBuilding(b);
  panel.innerHTML = `<h2>${esc(b.name)}</h2><p class="muted">${b.floors.length} floor(s) · ${b.dept}</p>${kpis(tally(ids), ids.length)}`;
  for (const f of [...b.floors].sort((x, y) => floorNo(y.floor) - floorNo(x.floor))) {
    panel.insertAdjacentHTML("beforeend", `<h3>${floorName(f.floor)}</h3>`);
    panel.append(list(f.rooms.map((r) => {
      const t = tally(r.devices);
      return [`<span><i class="dot ${worst(t)}"></i> ${esc(r.name)}</span><span class="muted">${esc(r.type)} · ${r.devices.length}</span>`, () => goRoom(r.id)];
    })));
  }
}
function panelRoom(r) {
  panel.innerHTML = `<h2>${esc(r.name)}</h2><p class="muted">${esc(bById[r.building].name)} · ${floorName(r.floor)} · ${esc(r.type)}</p>${kpis(tally(r.devices), r.devices.length)}<h3>Devices</h3>`;
  panel.append(list(r.devices.map((id) => {
    const d = byId[id];
    return [`<span><i class="dot ${state(d)}"></i> ${esc(d.name)}</span><span class="muted">${esc(d.kind)}${behind(d).length ? ` · ${behind(d).length} update(s)` : ""}</span>`, () => selectDevice(id)];
  })));
}
const meter = (n, v) => `<div class="meter"><span>${n}</span><span class="track"><i class="${v >= 90 ? "hot" : v >= 75 ? "mid" : ""}" style="width:${Math.min(v, 100)}%"></i></span><b>${Math.round(v)}%</b></div>`;
function panelDevice(d) {
  const s = state(d), room = roomById[d.room], up = behind(d);
  const lic = (name) => LICENCES.find((l) => name.startsWith(l.product));
  panel.innerHTML = `<div class="head"><i class="dot ${s}"></i><div><h2>${esc(d.name)}</h2><span class="muted">${d.id} · ${esc(d.kind)} · ${esc(room.name)}, ${esc(bById[d.building].name)}</span></div></div>
    <p><span class="chip ${s === "online" ? "ok" : s === "warning" ? "warn" : s === "faulty" ? "bad" : ""}">${LABEL[s]}</span>${d.issue ? ` <span class="chip bad">${esc(d.issue)}</span>` : ""}</p>
    ${s === "offline" ? `<p class="muted">No report from this device. Last values shown.</p>` : ""}
    <h3>Live ${d.real ? (d.reporting ? `<span class="chip ok">real · from the agent</span>` : `<span class="chip">agent not reporting</span>`) : `<span class="chip">simulated</span>`}</h3>${meter("CPU", d.cpu)}${meter("Memory", d.ram)}${meter("Disk", d.disk)}${d.real && d.user ? `<p class="muted">Signed in: ${esc(d.user)} · up ${d.uptime_h} h · reported ${esc(d.seen)}</p>` : ""}
    <h3>Record</h3><table class="kv"><tbody>
      <tr><th>Model</th><td>${esc(d.model)}</td></tr><tr><th>Serial</th><td>${esc(d.serial)}</td></tr><tr><th>IP</th><td>${esc(d.ip)}</td></tr>
      <tr><th>Department</th><td>${esc(d.dept)}</td></tr><tr><th>Warranty until</th><td>${esc(d.warranty)}</td></tr></tbody></table>
    ${d.software.length ? `<h3>Software (${d.software.length})${up.length ? ` <span class="chip warn">${up.length} behind</span>` : ""}</h3>
      <ul class="soft">${d.software.map((x) => `<li><span>${esc(x.name)}${lic(x.name) ? ` <span class="chip">licensed</span>` : ""}</span><span>${esc(x.version)}${x.version !== x.newest ? ` <span class="chip warn">→ ${esc(x.newest)}</span>` : ""}</span></li>`).join("")}</ul>` : ""}
    <p class="muted" style="margin-top:14px">${d.real ? "This is the PC you are using: its numbers are read by the agent, not made up." : "Simulated. In the real system these values come from a small agent on each PC and from the AMS record."}</p>`;
}
function refreshPanel() {
  // only the open device keeps updating; lists stay put so they don't jump while being read
  if (overlayTab) return fillPage(); // an open page keeps its numbers live
  if (nav.view === "room" && picked) panelDevice(byId[picked]);
}

// ---------- tabs: software updates, licences, alerts: each is a page of its own (the 3D view is hidden) ----------
const overlay = document.getElementById("overlay"), layout = document.querySelector(".layout");
let overlayTab = null, pageState = { q: "", f: "all", sel: null };
function closeOverlay() {
  overlayTab = null;
  overlay.hidden = true;
  layout.classList.remove("paged");
  document.body.classList.remove("on-page");
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.tab === "map"));
  window.dispatchEvent(new Event("resize")); // the 3D view was hidden: let it measure itself again
}
const licenceUse = (l) => devices.filter((d) => d.software.some((s) => s.name.startsWith(l.product))).length;
const kpi = (n, label, c) => `<div class="kpi" style="--c:${c || "#64748b"}"><b>${n}</b><span>${label}</span></div>`;
function whyAlert(d) {
  const m = Math.max(d.cpu, d.ram, d.disk);
  return d.issue || (state(d) === "offline" ? "No report from the device" : `${["CPU", "Memory", "Disk"][[d.cpu, d.ram, d.disk].indexOf(m)]} at ${Math.round(m)}%`);
}
const PAGES = {
  updates: {
    title: "Software updates",
    sub: "Each program against the newest version found on campus. Pick a row to see the PCs that are behind. Track only: IT updates PCs with its usual tools.",
    data() {
      const rows = {}, known = new Set(SOFTWARE.map((x) => x.name)); // only programs the college tracks a newest version for; the real PC has 100+ others
      devices.forEach((d) => d.software.forEach((x) => {
        if (!known.has(x.name)) return;
        const r = (rows[x.name] ||= { name: x.name, publisher: x.publisher, newest: x.newest, total: 0, behind: [] });
        r.total++;
        if (x.version !== x.newest) r.behind.push(d.id);
      }));
      return Object.values(rows).sort((a, b) => b.behind.length - a.behind.length);
    },
    kpis(rows) {
      const pcs = new Set(rows.flatMap((r) => r.behind)), n = rows.reduce((a, r) => a + r.behind.length, 0);
      return kpi(rows.length, "programs tracked") + kpi(pcs.size, "PCs behind on something", "#f59e0b") + kpi(n, "outdated installs", "#ef4444") + kpi(rows.filter((r) => !r.behind.length).length, "fully up to date", "#22c55e");
    },
    chips: [["all", "All"], ["behind", "Behind only"]],
    filter: (r, f) => f === "all" || r.behind.length > 0,
    text: (r) => r.name + " " + (r.publisher || ""),
    head: "<th>Software</th><th>Newest</th><th class='num'>Installed</th><th>Up to date</th><th class='num'>Behind</th><th>Where</th>",
    row: (r) => {
      const cur = Math.round((100 * (r.total - r.behind.length)) / r.total), rooms = [...new Set(r.behind.map((id) => roomById[byId[id].room].name))];
      return `<tr class="go ${pageState.sel === r.name ? "sel" : ""}" data-key="${esc(r.name)}"><td><b>${esc(r.name)}</b><div class="muted">${esc(r.publisher || "")}</div></td><td class="mono">${esc(r.newest)}</td><td class="num">${r.total}</td>
        <td><div class="seats"><i style="width:${cur}%"></i></div><span class="muted">${cur}%</span></td><td class="num">${r.behind.length ? `<span class="chip warn">${r.behind.length}</span>` : `<span class="chip ok">0</span>`}</td>
        <td class="muted">${rooms.slice(0, 3).map(esc).join(", ")}${rooms.length > 3 ? ` +${rooms.length - 3} more` : ""}</td></tr>`;
    },
    detail(rows) {
      const r = rows.find((x) => x.name === pageState.sel);
      return r ? `<div class="card detail"><h3>${esc(r.name)}: ${r.behind.length} PC(s) below ${esc(r.newest)}</h3><div class="pcs">${r.behind.map((id) => `<button type="button" class="pcbtn" data-go="${id}"><i class="dot ${state(byId[id])}"></i>${esc(byId[id].name)}<small>${esc(byId[id].software.find((x) => x.name === r.name).version)} · ${esc(roomById[byId[id].room].name)}</small></button>`).join("") || "<p class='muted'>All current.</p>"}</div></div>` : "";
    },
  },
  licences: {
    title: "Licences",
    sub: "Installs found on PCs against seats bought, and renewal dates.",
    data: () => LICENCES.map((l) => ({ ...l, used: licenceUse(l), days: l.renewal ? Math.round((new Date(l.renewal) - new Date()) / 864e5) : null })),
    kpis(rows) {
      return kpi(rows.filter((r) => r.seats && r.used > r.seats).length, "over their seats", "#ef4444") + kpi(rows.filter((r) => r.days !== null && r.days < 60).length, "renewing within 60 days", "#f59e0b")
        + kpi(rows.filter((r) => r.seats).reduce((a, r) => a + r.used, 0).toLocaleString("en-IN"), "paid seats in use") + kpi(rows.filter((r) => !r.seats).length, "free products", "#22c55e");
    },
    chips: [["all", "All"], ["over", "Over seats"], ["renew", "Renewing soon"]],
    filter: (r, f) => f === "all" || (f === "over" ? r.seats && r.used > r.seats : r.days !== null && r.days < 60),
    text: (r) => r.product + " " + r.type,
    head: "<th>Product</th><th>Type</th><th>Seats used</th><th>Renewal</th>",
    row: (r) => {
      const over = r.seats && r.used > r.seats;
      return `<tr><td><b>${esc(r.product)}</b></td><td>${esc(r.type)}</td><td>${r.seats ? `${r.used} of ${r.seats}${over ? ` <span class="chip bad">over by ${r.used - r.seats}</span>` : ""}<div class="seats"><i class="${over ? "over" : ""}" style="width:${Math.min(100, (r.used / r.seats) * 100)}%"></i></div>` : `${r.used} installs <span class="chip ok">free</span>`}</td>
        <td>${r.renewal ? `${esc(r.renewal)} ${r.days < 0 ? `<span class="chip bad">expired</span>` : r.days < 60 ? `<span class="chip warn">${r.days} days</span>` : ""}` : "—"}</td></tr>`;
    },
  },
  alerts: {
    title: "Alerts",
    sub: "Every device that is faulty, offline or running short of CPU, memory or disk. Pick one to open it on the map.",
    data: () => devices.filter((d) => state(d) !== "online").sort((a, b) => ["faulty", "offline", "warning"].indexOf(state(a)) - ["faulty", "offline", "warning"].indexOf(state(b))),
    kpis(rows) {
      const c = (s) => rows.filter((d) => state(d) === s).length;
      return kpi(rows.length, "open alerts") + kpi(c("faulty"), "faulty", "#ef4444") + kpi(c("offline"), "offline", "#64748b") + kpi(c("warning"), "need attention", "#f59e0b");
    },
    chips: [["all", "All"], ["faulty", "Faulty"], ["offline", "Offline"], ["warning", "Needs attention"]],
    filter: (d, f) => f === "all" || state(d) === f,
    text: (d) => `${d.name} ${roomById[d.room].name} ${bById[d.building].name} ${whyAlert(d)}`,
    head: "<th>Device</th><th>State</th><th>Where</th><th>Why</th><th></th>",
    row: (d) => `<tr class="go" data-go="${d.id}"><td><i class="dot ${state(d)}"></i> <b>${esc(d.name)}</b><div class="muted">${esc(d.kind)} · ${d.id}</div></td><td>${LABEL[state(d)]}</td>
      <td>${esc(roomById[d.room].name)}<div class="muted">${esc(bById[d.building].name)}</div></td><td class="muted">${esc(whyAlert(d))}</td><td><button type="button" class="pcbtn" data-go="${d.id}">Open on map →</button></td></tr>`,
  },
};
function renderOverlay(tab) {
  if (overlayTab !== tab) pageState = { q: "", f: "all", sel: null };
  overlayTab = tab;
  overlay.hidden = false;
  layout.classList.add("paged");
  document.body.classList.add("on-page");
  const P = PAGES[tab];
  overlay.innerHTML = `<div class="page-inner">
    <div class="page-head"><div><h1>${P.title}</h1><p class="sub">${P.sub}</p></div><button type="button" class="pcbtn back" id="pageback">← Back to the 3D campus</button></div>
    <div class="kpis" id="pagekpis"></div>
    <div class="filters"><input type="search" id="pageq" placeholder="Search" aria-label="Search this page" value="${esc(pageState.q)}">
      ${P.chips ? P.chips.map(([k, l]) => `<button type="button" class="pcbtn chipf ${pageState.f === k ? "on" : ""}" data-f="${k}">${l}</button>`).join("") : ""}</div>
    <div class="card"><table><thead><tr>${P.head}</tr></thead><tbody id="pagebody"></tbody></table></div><div id="pagedetail"></div></div>`;
  document.getElementById("pageback").onclick = () => closeOverlay();
  document.getElementById("pageq").oninput = (e) => { pageState.q = e.target.value; fillPage(); };
  overlay.querySelectorAll(".chipf").forEach((b) => (b.onclick = () => { pageState.f = b.dataset.f; overlay.querySelectorAll(".chipf").forEach((x) => x.classList.toggle("on", x === b)); fillPage(); }));
  fillPage();
  overlay.scrollTop = 0;
}
// only the numbers and rows are redrawn on each tick, so typing in the search box is never interrupted
function fillPage() {
  const P = PAGES[overlayTab], rows = P.data(), term = pageState.q.trim().toLowerCase();
  const shown = rows.filter((r) => P.filter(r, pageState.f) && (!term || P.text(r).toLowerCase().includes(term)));
  document.getElementById("pagekpis").innerHTML = P.kpis(rows);
  document.getElementById("pagebody").innerHTML = shown.map(P.row).join("") || `<tr><td colspan="6" class="empty">Nothing matches.</td></tr>`;
  document.getElementById("pagedetail").innerHTML = P.detail ? P.detail(rows) : "";
  overlay.querySelectorAll("[data-go]").forEach((el) => (el.onclick = (e) => { e.stopPropagation(); const id = el.dataset.go; closeOverlay(); selectDevice(id); }));
  overlay.querySelectorAll("tr.go[data-key]").forEach((tr) => (tr.onclick = () => { pageState.sel = pageState.sel === tr.dataset.key ? null : tr.dataset.key; fillPage(); if (pageState.sel) document.getElementById("pagedetail").scrollIntoView({ behavior: "smooth", block: "nearest" }); }));
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
  if (t.dataset.tab === "map") { closeOverlay(); ({ campus: panelCampus, building: () => panelBuilding(bById[nav.b]), room: () => (picked ? panelDevice(byId[picked]) : panelRoom(roomById[nav.r])) })[nav.view](); }
  else renderOverlay(t.dataset.tab);
}));
function counts() {
  const up = devices.filter((d) => behind(d).length).length;
  const over = LICENCES.filter((l) => l.seats && devices.filter((d) => d.software.some((s) => s.name.startsWith(l.product))).length > l.seats).length;
  const alerts = devices.filter((d) => state(d) !== "online").length;
  document.getElementById("c-updates").textContent = up;
  document.getElementById("c-licences").textContent = over || "";
  document.getElementById("c-alerts").textContent = alerts;
  const t = tally(nav.r ? roomById[nav.r].devices : nav.b ? idsOfBuilding(bById[nav.b]) : allIds);
  document.getElementById("legend").innerHTML = Object.keys(HEX).map((k) => `<span><i class="dot ${k}"></i>${LABEL[k]} <b>${t[k]}</b></span>`).join("");
}

// ---------- search ----------
const search = document.getElementById("search"), results = document.getElementById("results");
search.addEventListener("input", () => {
  const term = search.value.trim().toLowerCase();
  results.replaceChildren();
  if (term.length < 2) return;
  const hits = [...Object.values(roomById).filter((r) => r.name.toLowerCase().includes(term)).map((r) => ["room", r]),
    ...devices.filter((d) => d.name.toLowerCase().includes(term) || d.id.toLowerCase().includes(term)).map((d) => ["device", d])].slice(0, 10);
  for (const [kind, x] of hits) {
    const b = document.createElement("button");
    b.innerHTML = kind === "room" ? `<span>${esc(x.name)}</span><small>room · ${esc(bById[x.building].name)}</small>` : `<span><i class="dot ${state(x)}"></i> ${esc(x.name)}</span><small>${esc(roomById[x.room].name)}</small>`;
    b.onclick = () => { results.replaceChildren(); search.value = ""; closeOverlay(); kind === "room" ? goRoom(x.id) : selectDevice(x.id); };
    results.append(b);
  }
});

// ---------- live simulation ----------
const feed = document.getElementById("feed");
function toast(d, s) {
  const t = document.createElement("div");
  t.className = `toast ${s}`;
  t.textContent = `${d.name} (${roomById[d.room].name}): ${LABEL[s].toLowerCase()}`;
  feed.prepend(t);
  setTimeout(() => t.remove(), 6000);
  while (feed.children.length > 4) feed.lastChild.remove();
}
setInterval(() => {
  for (const d of devices) {
    if (d.real) continue; // the real PC is changed only by its agent
    const before = state(d);
    d.cpu = Math.min(99, Math.max(2, d.cpu + (Math.random() - 0.5) * 14));
    d.ram = Math.min(97, Math.max(15, d.ram + (Math.random() - 0.5) * 6));
    if (d.online && Math.random() < 0.0015) d.online = false;
    else if (!d.online && Math.random() < 0.03) d.online = true;
    if (!d.faulty && Math.random() < 0.0004) { d.faulty = true; d.issue = "Reported by staff: not working"; }
    const after = state(d);
    if (before !== after && (after !== "warning" || Math.random() < 0.25)) toast(d, after);
  }
  refreshCampus();
  if (nav.view === "building") refreshBuilding();
  if (nav.view === "room") refreshRoom();
  counts();
  refreshPanel();
}, 2500);
// ---------- real data: one device on the map is the PC the agent runs on ----------
const livePill = document.getElementById("livepill");
function pickRealSlot() {
  // the first PC in the first lab of the Academic Block: stands in for the real machine
  const b = bById["academic-block"] || buildings[0];
  for (const f of b.floors) for (const r of f.rooms) if (r.type === "Lab") { const d = r.devices.map((id) => byId[id]).find((x) => x.kind === "PC"); if (d) return d; }
  return devices.find((d) => d.kind === "PC");
}
const slot = pickRealSlot();
async function pollLive() {
  if (window.NO_LIVE) return; // inside the AMS there is no collector
  let data = null;
  try { const r = await fetch("/api/live", { cache: "no-store" }); if (r.ok) data = await r.json(); } catch (e) { /* static server, no collector */ }
  const rep = data?.devices?.[0];
  const fresh = rep && rep.age < 30;
  if (rep) {
    slot.real = true;
    slot.reporting = fresh;
    slot.name = `${rep.hostname} (this PC)`;
    Object.assign(slot, { cpu: rep.cpu ?? slot.cpu, ram: rep.ram ?? slot.ram, disk: rep.disk ?? slot.disk, online: fresh, faulty: false, issue: null,
      ip: rep.ip || slot.ip, model: rep.model || slot.model, serial: rep.serial || slot.serial, user: rep.user, uptime_h: rep.uptime_h, seen: `${Math.round(rep.age)} s ago` });
    if (rep.software?.length) slot.software = rep.software.map((s) => ({ name: s.name, version: s.version || "-", newest: s.version || "-", publisher: s.publisher }));
  }
  const tag = document.getElementById("demotag");
  tag.textContent = fresh ? "Demo · 1 real device live, rest simulated" : "Demo · simulated data";
  tag.classList.toggle("real", !!fresh);
  livePill.hidden = !data;
  livePill.className = `livepill ${fresh ? "on" : ""}`;
  livePill.textContent = !data ? "" : fresh ? `● LIVE: ${rep.hostname} · CPU ${Math.round(rep.cpu)}% · memory ${Math.round(rep.ram)}% · ${Math.round(rep.age)} s ago`
    : rep ? `○ ${rep.hostname}: no report for ${Math.round(rep.age)} s` : "○ Collector running: waiting for an agent";
  if (fresh) {
    refreshCampus();
    if (nav.view === "building") refreshBuilding();
    if (nav.view === "room") refreshRoom();
    counts();
    if (nav.view === "room" && picked === slot.id) panelDevice(slot);
  }
}
setInterval(pollLive, 3000);
pollLive();
setInterval(() => (document.getElementById("clock").textContent = new Date().toLocaleString("en-IN", { weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" })), 1000);

// ---------- atmosphere: sky dome, sun glow, stars, moon, clouds, birds ----------
camera.far = 3000;
camera.updateProjectionMatrix();
const skyU = { top: { value: new THREE.Color() }, horizon: { value: new THREE.Color() }, bottom: { value: new THREE.Color() }, sunDir: { value: new THREE.Vector3(0, 1, 0) }, sunCol: { value: new THREE.Color(1, 0.9, 0.7) }, glow: { value: 0.5 } };
const skyDome = new THREE.Mesh(new THREE.SphereGeometry(1400, 32, 16), new THREE.ShaderMaterial({
  side: THREE.BackSide, depthWrite: false, fog: false, uniforms: skyU,
  vertexShader: "varying vec3 vP; void main(){ vP = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
  fragmentShader: `uniform vec3 top; uniform vec3 horizon; uniform vec3 bottom; uniform vec3 sunDir; uniform vec3 sunCol; uniform float glow; varying vec3 vP;
    void main(){ float h = vP.y; vec3 c = h > 0.0 ? mix(horizon, top, pow(clamp(h, 0.0, 1.0), 0.5)) : mix(horizon, bottom, clamp(-h * 3.0, 0.0, 1.0));
      float s = max(dot(normalize(vP), sunDir), 0.0); c += sunCol * (pow(s, 700.0) * 2.5 + pow(s, 14.0) * 0.4 * glow); gl_FragColor = vec4(c, 1.0); }`,
}));
skyDome.renderOrder = -10;
skyDome.frustumCulled = false;
scene.add(skyDome);
const starPts = [];
for (let i = 0; i < 1100; i++) { const u = rnd() * 2 - 1, a = rnd() * Math.PI * 2, r = Math.sqrt(1 - u * u); if (u > 0.04) starPts.push(Math.cos(a) * r * 1300, u * 1300, Math.sin(a) * r * 1300); }
const starGeo = new THREE.BufferGeometry();
starGeo.setAttribute("position", new THREE.Float32BufferAttribute(starPts, 3));
const stars = new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0xffffff, size: 2.2, sizeAttenuation: false, transparent: true, opacity: 0, depthWrite: false, fog: false }));
stars.frustumCulled = false;
const moon = new THREE.Mesh(new THREE.SphereGeometry(34, 24, 16), new THREE.MeshBasicMaterial({ color: 0xf1f5ff, fog: false }));
moon.frustumCulled = false;
scene.add(stars, moon);
const cloudG = new THREE.Group(), cloudMat = new THREE.MeshLambertMaterial({ color: 0xffffff, emissive: 0x333333, fog: false, transparent: true, opacity: 0.93 });
for (let i = 0; i < 18; i++) {
  const c = new THREE.Group(), n = 4 + Math.floor(rnd() * 4);
  for (let k = 0; k < n; k++) {
    const m = new THREE.Mesh(new THREE.IcosahedronGeometry(1, 1), cloudMat), r = 5 + rnd() * 8;
    m.scale.set(r * 1.4, r * 0.55, r);
    m.position.set((k - n / 2) * 8 + rnd() * 4, rnd() * 3, (rnd() - 0.5) * 9);
    c.add(m);
  }
  c.position.set(-380 + rnd() * 760, 85 + rnd() * 45, -330 + rnd() * 660);
  c.userData.speed = 1.5 + rnd() * 2.5;
  cloudG.add(c);
}
scene.add(cloudG);
const PAL = { day: { top: 0x3f86d6, horizon: 0xcfe3f2, bottom: 0x7d9b78, cloud: 0xffffff }, dusk: { top: 0x2b2457, horizon: 0xf0a56f, bottom: 0x4a3a52, cloud: 0xffc9a3 }, night: { top: 0x03060f, horizon: 0x16214a, bottom: 0x0a1020, cloud: 0x2a3350 } };
const _a = new THREE.Color(), _b = new THREE.Color();
const blend = (key, w) => _a.setHex(0).add(_b.setHex(PAL.day[key]).multiplyScalar(w.day)).add(_b.setHex(PAL.dusk[key]).multiplyScalar(w.dusk)).add(_b.setHex(PAL.night[key]).multiplyScalar(w.night));
function updateSky(sunUp, night) {
  const day = Math.min(1, Math.max(0, (sunUp - 0.1) / 0.4)), nt = Math.min(1, Math.max(0, 1 - sunUp * 4)), dusk = Math.max(0, 1 - day - nt), sum = day + nt + dusk || 1;
  const w = { day: day / sum, dusk: dusk / sum, night: nt / sum };
  skyU.top.value.copy(blend("top", w));
  skyU.horizon.value.copy(blend("horizon", w));
  skyU.bottom.value.copy(blend("bottom", w));
  scene.fog.color.copy(skyU.horizon.value);
  cloudMat.color.copy(blend("cloud", w));
  cloudMat.emissive.copy(cloudMat.color).multiplyScalar(0.25);
  skyU.sunDir.value.copy(sun.position).sub(sun.target.position).normalize();
  skyU.glow.value = 0.35 + 0.65 * w.dusk;
  skyU.sunCol.value.set(1, 0.92 - 0.25 * w.dusk, 0.75 - 0.3 * w.dusk).multiplyScalar(sunUp > 0.02 ? 1 : 0);
  stars.material.opacity = night * 0.95;
  moon.visible = night > 0.25;
  moon.position.copy(skyU.sunDir.value).multiplyScalar(-1150).setY(Math.max(250, -skyU.sunDir.value.y * 1150));
  birds.visible = sunUp > 0.12;
  stage.dataset.sky = night > 0.6 ? "night" : sunUp > 0.35 ? "day" : "dusk";
}
// birds: V-shaped silhouettes circling over the campus
const birds = new THREE.InstancedMesh((() => { const g = new THREE.BufferGeometry(); g.setAttribute("position", new THREE.Float32BufferAttribute([0, 0, 0, -0.5, 0.05, -0.28, -0.12, 0, 0.06, 0, 0, 0, 0.5, 0.05, -0.28, 0.12, 0, 0.06], 3)); g.computeVertexNormals(); return g; })(), new THREE.MeshBasicMaterial({ color: 0x1f2937, side: THREE.DoubleSide, fog: false }), 14);
const birdSpec = Array.from({ length: 14 }, () => ({ r: 40 + rnd() * 70, h: 22 + rnd() * 20, a: rnd() * 6.28, w: (0.12 + rnd() * 0.12) * (rnd() < 0.5 ? 1 : -1), f: 6 + rnd() * 4, cx: CENTER.x + (rnd() - 0.5) * 40, cz: CENTER.z + (rnd() - 0.5) * 40 }));
birds.frustumCulled = false;
scene.add(birds);
const _o = new THREE.Object3D();
function updateAtmosphere(dt, t) {
  skyDome.position.copy(camera.position);
  stars.position.copy(camera.position);
  cloudG.children.forEach((c) => { c.position.x += c.userData.speed * dt; if (c.position.x > 400) c.position.x = -400; });
  if (birds.visible) birdSpec.forEach((b, i) => {
    b.a += b.w * dt;
    _o.position.set(b.cx + Math.cos(b.a) * b.r, b.h + Math.sin(t * 0.7 + i) * 1.2, b.cz + Math.sin(b.a) * b.r);
    _o.rotation.set(0, -b.a + (b.w > 0 ? Math.PI : 0) + Math.PI / 2 * (b.w > 0 ? -1 : 1) * 0, 0);
    _o.rotation.y = -(b.a + (b.w > 0 ? Math.PI / 2 : -Math.PI / 2));
    _o.scale.set(1.4, 0.4 + Math.abs(Math.sin(t * b.f + i)) * 0.9, 1.4); // wings flap by squashing the V
    _o.updateMatrix();
    birds.setMatrixAt(i, _o.matrix);
  });
  birds.instanceMatrix.needsUpdate = true;
}

// ---------- hover outline on a building, and a mini-map ----------
const outlines = {};
for (const b of buildings) {
  const H = b.floors.length * FH + 0.5, pts = b.poly.map(([x, z]) => new THREE.Vector3(x, H, z));
  const line = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.95, depthTest: false }));
  line.renderOrder = 6;
  line.visible = false;
  campusG.add(line);
  outlines[b.id] = line;
}
let outlined = null;
function setOutline(id) {
  if (outlined === id) return;
  if (outlined && outlines[outlined]) outlines[outlined].visible = false;
  outlined = id;
  if (id && outlines[id]) outlines[id].visible = true;
  drawMini();
}
const mini = document.createElement("canvas");
mini.className = "minimap";
mini.width = 440;
mini.height = 320;
mini.title = "Click a building to open it";
stage.append(mini);
const mx = mini.getContext("2d"), allPts = buildings.flatMap((b) => b.poly), PAD = 10;
const MB = { x0: Math.min(...allPts.map((p) => p[0])) - 12, x1: Math.max(...allPts.map((p) => p[0])) + 12, z0: Math.min(...allPts.map((p) => p[1])) - 12, z1: Math.max(...allPts.map((p) => p[1])) + 12 };
const MS = Math.min((mini.width - 2 * PAD) / (MB.z1 - MB.z0), (mini.height - 2 * PAD) / (MB.x1 - MB.x0)); // east at the top, like the college's own map
const toMini = (x, z) => [PAD + (z - MB.z0) * MS, mini.height - PAD - (x - MB.x0) * MS];
const fromMini = (px, py) => [MB.x0 + (mini.height - PAD - py) / MS, MB.z0 + (px - PAD) / MS];
function drawMini() {
  if (overlayTab) return;
  mx.clearRect(0, 0, mini.width, mini.height);
  mx.fillStyle = "rgba(10,14,28,.78)";
  mx.beginPath(); mx.roundRect(0, 0, mini.width, mini.height, 22); mx.fill();
  mx.strokeStyle = "#2f3a5e"; mx.lineWidth = 3;
  for (const r of CAMPUS_GEO.roads) { mx.beginPath(); r.forEach(([x, z], i) => { const [a, b] = toMini(x, z); i ? mx.lineTo(a, b) : mx.moveTo(a, b); }); mx.stroke(); }
  const pg = CAMPUS_GEO.landmarks.playground;
  mx.fillStyle = "rgba(180,85,62,.55)"; mx.beginPath(); pg.forEach(([x, z], i) => { const [a, b] = toMini(x, z); i ? mx.lineTo(a, b) : mx.moveTo(a, b); }); mx.fill();
  for (const b of buildings) {
    mx.beginPath();
    b.poly.forEach(([x, z], i) => { const [a, c] = toMini(x, z); i ? mx.lineTo(a, c) : mx.moveTo(a, c); });
    mx.closePath();
    mx.fillStyle = CSSC[bState(b)];
    mx.globalAlpha = 0.9; mx.fill(); mx.globalAlpha = 1;
    mx.strokeStyle = b.id === outlined || b.id === nav.b ? "#f5b800" : "rgba(255,255,255,.55)";
    mx.lineWidth = b.id === outlined || b.id === nav.b ? 4 : 1.5;
    mx.stroke();
  }
  const [cx, cy] = toMini(camera.position.x, camera.position.z), ang = Math.atan2(-(controls.target.x - camera.position.x), controls.target.z - camera.position.z); // up on the map is east
  mx.save(); mx.translate(cx, cy); mx.rotate(ang + Math.PI);
  mx.fillStyle = "rgba(255,255,255,.2)"; mx.beginPath(); mx.moveTo(0, 0); mx.lineTo(-26, 52); mx.lineTo(26, 52); mx.closePath(); mx.fill();
  mx.fillStyle = "#fff"; mx.beginPath(); mx.arc(0, 0, 6, 0, 7); mx.fill();
  mx.restore();
  mx.fillStyle = "#cbd5e1"; mx.font = "600 18px Montserrat, sans-serif"; mx.fillText("E ↑", mini.width - 46, 26);
}
mini.addEventListener("click", (e) => {
  const r = mini.getBoundingClientRect(), [x, z] = fromMini(((e.clientX - r.left) / r.width) * mini.width, ((e.clientY - r.top) / r.height) * mini.height);
  const hit = buildings.find((b) => inside(x, z, b.poly));
  if (hit) { closeOverlay(); goBuilding(hit.id); }
});
mini.addEventListener("pointermove", (e) => {
  const r = mini.getBoundingClientRect(), [x, z] = fromMini(((e.clientX - r.left) / r.width) * mini.width, ((e.clientY - r.top) / r.height) * mini.height);
  const hit = buildings.find((b) => inside(x, z, b.poly));
  mini.style.cursor = hit ? "pointer" : "default";
  setOutline(hit ? hit.id : null);
});
mini.addEventListener("pointerleave", () => setOutline(null));
setInterval(drawMini, 450);

// ---------- guided tour and idle camera ----------
const caption = document.createElement("div");
caption.className = "caption";
caption.hidden = true;
stage.append(caption);
const tourBtn = document.getElementById("tour");
let tourOn = false, tourTimer = null, tourStep = 0, lastInput = performance.now();
const tourList = [...buildings].sort((a, b) => b.floors.length * b.size[0] * b.size[1] - a.floors.length * a.size[0] * a.size[1]).slice(0, 8);
function stopTour(back) {
  clearTimeout(tourTimer);
  if (!tourOn) return;
  tourOn = false;
  caption.hidden = true;
  tourBtn.textContent = "▶ Tour";
  tourBtn.classList.remove("on");
  if (back) goCampus();
}
function tourNext() {
  if (!tourOn) return;
  if (tourStep >= tourList.length) return stopTour(true);
  const b = tourList[tourStep++], [cx, cz] = centroidOf(b.poly), H = b.floors.length * FH, t = tally(idsOfBuilding(b)), s = Math.max(...b.size);
  fly([cx - s * 0.9 - 8, H + 9, cz + s * 0.35 + 10], [cx, H * 0.45, cz]);
  setOutline(b.id);
  caption.innerHTML = `<b>${esc(b.name)}</b><span>${b.floors.length} floor(s) · ${idsOfBuilding(b).length} devices · ${t.faulty + t.offline} down · ${tourStep} of ${tourList.length}</span>`;
  caption.hidden = false;
  tourTimer = setTimeout(tourNext, 4800);
}
tourBtn.onclick = () => {
  if (tourOn) return stopTour(true);
  closeOverlay();
  if (nav.view !== "campus") goCampus();
  tourOn = true;
  tourStep = 0;
  tourBtn.textContent = "■ Stop tour";
  tourBtn.classList.add("on");
  tourNext();
};
["pointerdown", "wheel", "keydown", "touchstart"].forEach((ev) => window.addEventListener(ev, () => (lastInput = performance.now()), { passive: true }));
controls.addEventListener("start", () => { if (tourOn) { clearTimeout(tourTimer); stopTour(false); setOutline(null); } });
controls.autoRotateSpeed = 0.45;
function idleCamera() {
  controls.autoRotate = nav.view === "campus" && !tourOn && !goal && !overlayTab && performance.now() - lastInput > 40000; // a slow orbit when nobody is touching it
}

// ---------- glow at night (bloom), switched off by itself on a slow PC ----------
const composer = new EffectComposer(renderer), bloom = new UnrealBloomPass(new THREE.Vector2(512, 512), 0.7, 0.55, 0.88);
composer.addPass(new RenderPass(scene, camera));
composer.addPass(bloom);
composer.addPass(new OutputPass());
let quality = "high", slowFrames = 0;
const qBtn = document.getElementById("quality");
function setQuality(q) { quality = q; qBtn.textContent = q === "high" ? "Glow: on" : "Glow: off"; }
qBtn.onclick = () => { slowFrames = 0; setQuality(quality === "high" ? "standard" : "high"); };
setQuality("high");
let nightNow = 0;
function drawFrame(dt) {
  nightNow += (( stage.dataset.sky === "night" ? 1 : stage.dataset.sky === "dusk" ? 0.5 : 0) - nightNow) * Math.min(1, dt * 2);
  if (quality === "high" && nightNow > 0.2) {
    bloom.strength = 0.25 + 0.7 * nightNow;
    composer.render();
    slowFrames = dt > 0.045 ? slowFrames + 1 : Math.max(0, slowFrames - 1);
    if (slowFrames > 60) { setQuality("standard"); qBtn.title = "Switched off automatically: this computer is slow with it on"; } // about 3 seconds of slow frames in a row
  } else renderer.render(scene, camera);
}

// ---------- start ----------
// on the campus view, keep the biggest labels and hide the ones that would sit on top of each other
function declutter() {
  const els = [...stage.querySelectorAll(".label3d.pill")];
  if (nav.view !== "campus") return els.forEach((e) => (e.style.opacity = ""));
  const items = els.filter((e) => e.style.display !== "none").map((e) => ({ e, r: e.getBoundingClientRect(), p: +e.dataset.p || 900 })).sort((a, b) => b.p - a.p), kept = [];
  for (const it of items) {
    const r = it.r, hit = kept.some((k) => !(r.right < k.left - 4 || r.left > k.right + 4 || r.bottom < k.top - 2 || r.top > k.bottom + 2));
    it.e.style.opacity = hit ? "0" : "";
    if (!hit) kept.push(r);
  }
}
setInterval(declutter, 350);
const clock = new THREE.Clock();
let sinceSky = 99;
function loop() {
  const dt = Math.min(clock.getDelta(), 0.5);
  if (overlayTab) return requestAnimationFrame(loop); // a page is open: the 3D view is hidden, so don't draw it
  const k = 1 - Math.pow(0.02, dt); // same speed on a fast or a slow PC: about a second to arrive
  if (campusG.visible) {
    moveThings(dt);
    const t = performance.now() / 1000;
    if (flagParts.geo) {
      const pos = flagParts.geo.attributes.position;
      for (let i = 0; i < pos.count; i++) pos.array[i * 3 + 2] = Math.sin(flagParts.base[i * 3] * 2 + t * 4) * 0.12 * (flagParts.base[i * 3] + 1.5);
      pos.needsUpdate = true;
    }
  }
  if ((sinceSky += dt) > 5) { sinceSky = 0; daylight(); }
  updateAtmosphere(dt, performance.now() / 1000);
  idleCamera();
  if (goal) {
    camera.position.lerp(goal.pos, k);
    controls.target.lerp(goal.target, k);
    if (camera.position.distanceTo(goal.pos) < 0.08) goal = null;
  }
  if (ring?.visible) ring.rotation.z += 0.01;
  controls.update();
  drawFrame(dt);
  labels.render(scene, camera);
  requestAnimationFrame(loop);
}
controls.addEventListener("start", () => (goal = null)); // the user takes over the camera
new ResizeObserver(() => {
  const w = stage.clientWidth, h = stage.clientHeight;
  if (!w || !h) return; // hidden behind a page
  renderer.setSize(w, h);
  composer.setSize(w, h);
  labels.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}).observe(stage);
buildCampus();
camera.position.set(CENTER.x - 210, 160, CENTER.z + 30);
goCampus();
counts();
document.getElementById("loading").remove();
document.getElementById("sky").onclick = () => { timeMode = { live: "day", day: "night", night: "live" }[timeMode]; daylight(); };
daylight();
window.__demo = { camera, controls, goal: () => goal, devices, slot, renderer, scene }; // handy when checking the camera from the browser console
loop();
