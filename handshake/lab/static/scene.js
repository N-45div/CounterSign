// The 3D doorstep, animated from the emulator's timeline. Nothing here invents events:
// every vehicle, visit, parcel and note comes from the scenario the Ring emulator is playing.
import * as THREE from "three";

const WALK_S = 8;          // seconds to walk between the street and the door
const DRIVE_S = 5;         // seconds to drive in or out
const STAY_GAP_S = 60;     // vehicle spans closer than this are one parked stay
const DOOR_Z = 1.35;       // where a visitor stands, just past the mat
const STREET_Z = 11.6;
const MAT = new THREE.Vector3(-0.1, 0, 1.45);

function textTexture(lines, { w = 512, h = 256, bg = "#fff", fg = "#111", size = 44, pad = 24, bold = false } = {}) {
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const g = c.getContext("2d");
  g.fillStyle = bg; g.fillRect(0, 0, w, h);
  g.fillStyle = fg; g.textBaseline = "top";
  g.font = `${bold ? "700 " : ""}${size}px Inter, Arial, sans-serif`;
  lines.forEach((line, i) => g.fillText(line, pad, pad + i * size * 1.2));
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}

function box(w, h, d, color, opts = {}) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), new THREE.MeshStandardMaterial({ color, roughness: 0.85, ...opts }));
  m.castShadow = true; m.receiveShadow = true;
  return m;
}

function buildStatic(scene) {
  scene.background = new THREE.Color("#cfdbe6");
  scene.add(new THREE.HemisphereLight("#ffffff", "#8a7f70", 0.9));
  const sun = new THREE.DirectionalLight("#fff4e0", 1.6);
  sun.position.set(-6, 10, 8); sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  Object.assign(sun.shadow.camera, { left: -12, right: 12, top: 12, bottom: -12, near: 1, far: 40 });
  scene.add(sun);

  const ground = (w, d, x, z, color, y = 0) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, d), new THREE.MeshStandardMaterial({ color, roughness: 1 }));
    m.rotation.x = -Math.PI / 2; m.position.set(x, y, z); m.receiveShadow = true; scene.add(m);
  };
  ground(60, 40, 0, 10, "#6f8f58");            // lawn
  ground(60, 6, 0, 13.4, "#3d4046", 0.005);    // street
  ground(60, 1.4, 0, 9.8, "#b9b4aa", 0.006);   // pavement
  ground(1.5, 8.6, 0, 5.5, "#c8c2b6", 0.007);  // garden path
  ground(3.4, 1.9, 0, 1.0, "#d4cec2", 0.008);  // porch

  const wall = box(9, 3.4, 0.3, "#c9a98a"); wall.position.set(0, 1.7, -0.15); scene.add(wall);
  const frame = box(1.3, 2.35, 0.08, "#3b2a1f"); frame.position.set(0, 1.17, 0.02); scene.add(frame);
  const door = box(1.05, 2.15, 0.06, "#6d4426"); door.position.set(0, 1.08, 0.05); scene.add(door);
  const knob = new THREE.Mesh(new THREE.SphereGeometry(0.04), new THREE.MeshStandardMaterial({ color: "#c9a53a", metalness: 0.6 }));
  knob.position.set(0.38, 1.05, 0.1); scene.add(knob);
  const plate = new THREE.Mesh(new THREE.PlaneGeometry(0.42, 0.26), new THREE.MeshStandardMaterial({ map: textTexture(["402"], { w: 256, h: 160, size: 96, bg: "#20242c", fg: "#f0e6d0", pad: 34, bold: true }) }));
  plate.position.set(-1.0, 1.85, 0.01); scene.add(plate);
  const mat = box(1.0, 0.03, 0.6, "#4a3b30"); mat.position.set(0, 0.015, 1.05); scene.add(mat);
  const pot = box(0.36, 0.42, 0.36, "#a5552f"); pot.position.set(-1.45, 0.21, 0.45);  // left of the door, out of the doorbell view scene.add(pot);
  const bush = new THREE.Mesh(new THREE.SphereGeometry(0.42, 16, 12), new THREE.MeshStandardMaterial({ color: "#3e6b34", roughness: 1 }));
  bush.position.set(-1.45, 0.72, 0.45); bush.castShadow = true; scene.add(bush);
  // Neighbouring houses across the street, so the doorbell view has depth.
  for (const x of [-9, 0, 9]) {
    const h = box(7, 4, 4, ["#b7c1c9", "#d8c7a6", "#a9b79a"][(x / 9 + 1)]);
    h.position.set(x, 2, 20.5); scene.add(h);
  }
}

function buildVan() {
  const van = new THREE.Group();
  const body = box(4.6, 2.1, 1.9, "#eef0f2"); body.position.y = 1.35; van.add(body);
  const cab = box(1.2, 1.5, 1.85, "#e2e5e8"); cab.position.set(2.85, 1.05, 0); van.add(cab);
  const glass = box(0.05, 0.6, 1.6, "#8fb0cc", { metalness: 0.3, roughness: 0.2 }); glass.position.set(3.46, 1.45, 0); van.add(glass);
  const side = new THREE.Mesh(new THREE.PlaneGeometry(3.6, 0.9), new THREE.MeshStandardMaterial({
    map: textTexture(["PARCELS"], { w: 512, h: 128, size: 84, bg: "#eef0f2", fg: "#2b4f8f", pad: 18, bold: true }) }));
  side.position.set(-0.1, 1.45, -0.96); side.rotation.y = Math.PI; van.add(side);
  for (const [x, z] of [[-1.5, 0.95], [1.9, 0.95], [-1.5, -0.95], [1.9, -0.95]]) {
    const wheel = new THREE.Mesh(new THREE.CylinderGeometry(0.38, 0.38, 0.28, 20), new THREE.MeshStandardMaterial({ color: "#1f1f22" }));
    wheel.rotation.x = Math.PI / 2; wheel.position.set(x, 0.38, z); wheel.castShadow = true; van.add(wheel);
  }
  van.visible = false;
  return van;
}

function buildPerson(jacket) {
  const p = new THREE.Group();
  const skin = "#9c6b4e";
  const torso = box(0.46, 0.62, 0.26, jacket); torso.position.y = 1.2; p.add(torso);
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.13, 16, 12), new THREE.MeshStandardMaterial({ color: skin }));
  head.position.y = 1.65; head.castShadow = true; p.add(head);
  const cap = box(0.27, 0.07, 0.29, "#2a3a5c"); cap.position.set(0, 1.76, 0.02); p.add(cap);
  const limb = (w, h, color, x, y) => {
    const pivot = new THREE.Group(); pivot.position.set(x, y, 0);
    const m = box(w, h, w, color); m.position.y = -h / 2; pivot.add(m); p.add(pivot); return pivot;
  };
  p.userData = {
    legL: limb(0.17, 0.88, "#2c2f3a", -0.12, 0.9), legR: limb(0.17, 0.88, "#2c2f3a", 0.12, 0.9),
    armL: limb(0.12, 0.62, jacket, -0.31, 1.48), armR: limb(0.12, 0.62, jacket, 0.31, 1.48),
  };
  p.visible = false;
  return p;
}

function buildParcel() {
  const label = textTexture(["Deliver to: A. Sharma", "Flat 402", "FRAGILE"], { size: 40 });
  const cardboard = new THREE.MeshStandardMaterial({ color: "#c4985f", roughness: 0.9 });
  const top = new THREE.MeshStandardMaterial({ color: "#c4985f", map: label });
  const m = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.32, 0.38), [cardboard, cardboard, top, cardboard, cardboard, cardboard]);
  m.castShadow = true;
  m.visible = false;
  return m;
}

function buildNote(lines) {
  // Unlit, so the printed text stays crisp in the doorbell frame whatever the sun does.
  const m = new THREE.Mesh(new THREE.PlaneGeometry(0.84, 0.6), new THREE.MeshBasicMaterial({
    map: textTexture(lines, { w: 620, h: 440, size: 54, bg: "#ffe97a", fg: "#111", pad: 26, bold: true }), side: THREE.DoubleSide }));
  m.castShadow = true;
  m.visible = false;
  return m;
}

const lerp = (a, b, k) => a + (b - a) * Math.min(1, Math.max(0, k));
const within = (t, s, e) => t >= s && t < (e ?? Infinity);

function stays(spans) {
  const sorted = [...spans].sort((a, b) => a[0] - b[0]);
  const out = [];
  for (const [s, e] of sorted) {
    const last = out[out.length - 1];
    if (last && s - last[1] < STAY_GAP_S) last[1] = Math.max(last[1], e ?? Infinity);
    else out.push([s, e ?? Infinity]);
  }
  return out;
}

export function createWorld(worldCanvas, doorbellCanvas) {
  const scene = new THREE.Scene();
  buildStatic(scene);
  const van = buildVan(); scene.add(van);
  const courier = buildPerson("#2f5bd3"); scene.add(courier);
  const resident = buildPerson("#7a3f8f"); scene.add(resident);
  const parcel = buildParcel(); scene.add(parcel);
  let note = null;

  const director = new THREE.PerspectiveCamera(42, 16 / 9, 0.1, 100);
  director.position.set(-8.5, 6.2, 16.5); director.lookAt(0.6, 0.4, 3.6);  // from across the street, van in front
  const doorbell = new THREE.PerspectiveCamera(70, 16 / 9, 0.05, 80);
  doorbell.position.set(0.72, 1.32, 0.16); doorbell.lookAt(0.0, -0.35, 5.5);  // tilted down: porch and mat in view, street at the top

  const worldR = new THREE.WebGLRenderer({ canvas: worldCanvas, antialias: true });
  worldR.shadowMap.enabled = true;
  const bellR = new THREE.WebGLRenderer({ canvas: doorbellCanvas, antialias: true });
  bellR.shadowMap.enabled = true;
  const capCanvas = document.createElement("canvas");
  const capR = new THREE.WebGLRenderer({ canvas: capCanvas, antialias: true, preserveDrawingBuffer: true });
  capR.shadowMap.enabled = true; capR.setSize(1280, 720, false);

  function resize() {
    const r = worldCanvas.getBoundingClientRect();
    worldR.setSize(r.width, r.height, false);
    director.aspect = r.width / r.height; director.updateProjectionMatrix();
  }
  window.addEventListener("resize", resize); resize();

  let tl = null;
  function load(timeline) {
    tl = timeline;
    if (note) scene.remove(note);
    note = null;
    if (tl.spans.notes.length) { note = buildNote(tl.spans.notes[0][2]); scene.add(note); }
  }

  function walk(person, t, from, to, phase) {
    person.position.set(lerp(from.x, to.x, phase), 0, lerp(from.z, to.z, phase));
    const swing = Math.sin(t * 9) * 0.5 * (phase > 0 && phase < 1 ? 1 : 0);
    const u = person.userData;
    u.legL.rotation.x = swing; u.legR.rotation.x = -swing; u.armL.rotation.x = -swing * 0.7; u.armR.rotation.x = swing * 0.7;
  }

  function setTime(t) {
    if (!tl) return;
    const sp = tl.spans;
    // Van: arrives over DRIVE_S, parks, leaves over DRIVE_S.
    van.visible = false;
    for (const [s, e] of stays(sp.vehicles)) {
      if (!within(t, s, e)) continue;
      van.visible = true;
      const x = t < s + DRIVE_S ? lerp(-26, -1.5, (t - s) / DRIVE_S) : t > e - DRIVE_S ? lerp(-1.5, 26, (t - (e - DRIVE_S)) / DRIVE_S) : -1.5;
      van.position.set(x, 0, 13.1);
    }
    // Parcel on the mat while any parcel span is open (unless someone is carrying it).
    const onMat = sp.parcels.some(([s, e]) => within(t, s, e));
    parcel.visible = onMat; parcel.position.set(MAT.x, 0.16, MAT.z); parcel.rotation.set(0, 0.25, 0);
    courier.visible = false; resident.visible = false;
    if (note) {
      const [s, e] = sp.notes[0];
      note.visible = within(t, s, e);
      // Face the doorbell (planes face +z; the camera looks out from the door) and lean back a little.
      note.rotation.order = "YXZ"; note.position.set(0.35, 0.32, 1.3); note.rotation.set(0.55, Math.PI - 0.15, 0);
    }
    sp.people.forEach(([s, e], i) => {
      if (!within(t, s, e)) return;
      const picksUp = sp.parcels.find(([ps, pe]) => pe !== null && pe >= s && pe <= e);
      const delivers = sp.parcels.find(([ps]) => ps >= s && ps <= e);
      const person = picksUp ? resident : courier;
      person.visible = true;
      const span = Math.max(1, e - s), w = Math.min(picksUp ? 4 : WALK_S, span / 2);
      const home = picksUp ? new THREE.Vector3(0.55, 0, 0.45) : new THREE.Vector3(0.35, 0, STREET_Z);
      const door = new THREE.Vector3(0.3, 0, DOOR_Z + 0.25);
      if (t < s + w) { walk(person, t, home, door, (t - s) / w); person.rotation.y = Math.PI; }
      else if (t > e - w) { walk(person, t, door, home, (t - (e - w)) / w); person.rotation.y = 0; }
      else { walk(person, t, door, door, 1); person.rotation.y = Math.PI; }
      if (picksUp && t >= picksUp[1]) carry(person);
      else if (delivers && t < delivers[0]) carry(person);
      else if (!delivers && !picksUp && !sp.parcels.length && !sp.notes.length) carry(person);  // came, took it back
    });
  }

  function carry(person) {
    parcel.visible = true;
    parcel.position.set(person.position.x, 1.0, person.position.z + (person.rotation.y ? -0.32 : 0.32));
    parcel.rotation.set(0, 0, 0);
  }

  function render() {
    worldR.render(scene, director);
    bellR.render(scene, doorbell);
  }

  // Draws synchronously into its own canvas, so several captures can encode at once.
  function capture(atMs, label = "Lab Doorbell") {
    capR.render(scene, doorbell);
    const compose = Object.assign(document.createElement("canvas"), { width: 1280, height: 720 });
    const g = compose.getContext("2d");
    g.drawImage(capCanvas, 0, 0, 1280, 720);
    g.fillStyle = "rgba(10,10,14,.78)"; g.fillRect(0, 0, 1280, 36);
    g.fillStyle = "#eee"; g.font = "20px Inter, Arial, sans-serif"; g.textBaseline = "middle";
    g.fillText(`Handshake Lab  |  ${label}`, 14, 18);
    g.fillText(new Date(atMs).toISOString().replace("T", " ").slice(0, 19) + " UTC", 1010, 18);
    return new Promise((ok) => compose.toBlob(ok, "image/jpeg", 0.88));
  }

  return { load, setTime, render, capture };
}
