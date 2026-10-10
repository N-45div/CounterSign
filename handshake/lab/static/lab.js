// Doorstep Lab controller: plays a scenario in 3D, uploads what the doorbell records to the
// Ring emulator, then submits the courier's claim to the house and checks the signed receipt.
import { createWorld } from "./scene.js";

const $ = (id) => document.getElementById(id);
const world = createWorld($("world"), $("doorbell"));
let config = null;
let run = null;

const REASONS = {
  package_appeared: "A parcel is on the doorstep in the after-frame that was not there before the visit.",
  no_one_at_door: "The camera was recording the whole window and nobody came to the door.",
  visit_without_package: "Someone came to the door and left nothing behind.",
  instruction_text_in_frame: "Text in the frame was addressed to the AI. Handshake never confirms a delivery from a frame like that.",
  camera_not_recording: "The camera was offline for part of the window, so a no would not be fair. Route this to a person.",
  package_already_there: "A parcel was already on the doorstep before the visit, so a new one cannot be told apart.",
  unclassified_motion: "There was motion the camera could not classify. Route this to a person.",
  low_confidence: "The witness was not sure enough. Route this to a person.",
  frames_unavailable: "The frames around the visit were not available.",
  package_state_unknown: "The witness could not tell whether a parcel was there.",
};
const VERDICTS = { delivered: "Delivered", not_observed: "Not observed", inconclusive: "Inconclusive" };

const clock = (ms) => new Date(ms).toISOString().slice(11, 19) + " UTC";
const within = (t, s, e) => t >= s && t < (e ?? Infinity);

async function init() {
  config = await (await fetch("/lab/config")).json();
  $("witness").textContent = `witness: ${config.witness}`;
  const scenarios = await (await fetch("/lab/scenarios")).json();
  for (const s of scenarios) {
    const b = document.createElement("button");
    b.textContent = s.title; b.title = s.story; b.dataset.name = s.name;
    b.onclick = () => start(s.name);
    $("scenarios").append(b);
  }
  requestAnimationFrame(frame);
}

function lockButtons(name) {
  for (const b of $("scenarios").children) { b.disabled = !!name; b.classList.toggle("active", b.dataset.name === name); }
}

function drawTrack(tl, end) {
  const track = $("track");
  track.querySelectorAll(".seg, .claim").forEach((n) => n.remove());
  const pct = (x) => `${(100 * Math.min(x ?? end, end)) / end}%`;
  const seg = (cls, s, e) => {
    const d = document.createElement("div"); d.className = `seg ${cls}`;
    d.style.left = pct(s); d.style.width = `calc(${pct(e)} - ${pct(s)})`; track.append(d);
  };
  tl.spans.vehicles.forEach(([s, e]) => seg("vehicle", s, e));
  tl.spans.people.forEach(([s, e]) => seg("person", s, e));
  tl.spans.parcels.forEach(([s, e]) => seg("parcel", s, e));
  tl.spans.offline.forEach(([s, e]) => seg("off", s, e));
  const c = document.createElement("div"); c.className = "claim"; c.style.left = pct(tl.claim_at_ms / 1000 - tl.start_ms / 1000);
  track.append(c);
}

function resetConsole() {
  $("ring-log").innerHTML = "";
  $("claim").className = "muted"; $("claim").textContent = "Waiting for the tap.";
  $("verdict").className = "verdict muted"; $("verdict").textContent = "No claim yet.";
  $("evidence").hidden = true;
  $("receipt").className = "muted"; $("receipt").textContent = "Nothing signed yet.";
  $("payload-box").hidden = true;
}

function log(text) {
  const li = document.createElement("li"); li.textContent = text;
  $("ring-log").append(li); $("ring-log").scrollTop = 1e9;
}

async function start(name) {
  lockButtons(name); resetConsole();
  const tl = await (await fetch("/lab/start", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario: name }) })).json();
  const claimAt = (tl.claim_at_ms - tl.start_ms) / 1000;
  const ends = [claimAt, ...Object.values(tl.spans).flat().map((x) => x[1]).filter((x) => x !== null)];
  const end = Math.min(tl.duration_s, Math.max(...ends) + 15);
  world.load(tl);
  drawTrack(tl, end);
  $("story").textContent = tl.story;
  const events = [
    ...tl.spans.vehicles.map(([s]) => [s, "motion · vehicle"]),
    ...tl.spans.people.map(([s]) => [s, "motion · human"]),
    ...tl.spans.offline.flatMap(([s, e]) => [[s, "device_offline (webhook)"], ...(e !== null ? [[e, "device_online (webhook)"]] : [])]),
  ].sort((a, b) => a[0] - b[0]);
  run = { tl, end, claimAt, events, t0: performance.now(), ticks: tl.ticks.filter((t) => t <= end), next: 0,
          uploads: [], tapped: false, done: false };
  log(`playing "${tl.title}" · device Lab Doorbell`);
}

function frame(now) {
  requestAnimationFrame(frame);
  if (!run || run.done) { world.render(); return; }
  const speed = Number($("speed").value);
  const t = Math.min(run.end, ((now - run.t0) / 1000) * speed);
  // Capture every recording moment the playhead has passed, exactly at its own time.
  while (run.next < run.ticks.length && run.ticks[run.next] <= t) {
    const tick = run.ticks[run.next++];
    const atMs = run.tl.start_ms + Math.round(tick * 1000);
    world.setTime(tick);
    const blob = world.capture(atMs);
    run.uploads.push(blob.then((b) => fetch(`/lab/frames/${atMs}`, { method: "POST", body: b })));
  }
  world.setTime(t);
  world.render();
  $("clock").textContent = `t = ${t.toFixed(0)} s · ${clock(run.tl.start_ms + t * 1000)}`;
  $("cursor").style.left = `${(100 * t) / run.end}%`;
  const offline = run.tl.spans.offline.some(([s, e]) => within(t, s, e));
  $("offline").hidden = !offline;
  $("rec").classList.toggle("on", !offline && [...run.tl.spans.vehicles, ...run.tl.spans.people].some(([s, e]) => within(t, s, e)));
  while (run.events.length && run.events[0][0] <= t) {
    const [s, text] = run.events.shift();
    log(`${clock(run.tl.start_ms + s * 1000)}  ${text}`);
  }
  if (!run.tapped && t >= run.claimAt) {
    run.tapped = true;
    $("claim").className = "";
    $("claim").innerHTML = `<strong>Marked delivered</strong> at ${clock(run.tl.claim_at_ms)} by Lab Courier.<br>
      <span class="muted">The house answers once the claim window has closed (compressed here).</span>`;
  }
  if (t >= run.end) { run.done = true; finish(run); }
}

async function finish(r) {
  const results = await Promise.all(r.uploads);
  const failed = results.filter((x) => !x.ok).length;
  log(`${results.length} frames uploaded to the Ring emulator${failed ? `, ${failed} refused` : ""}`);
  $("verdict").textContent = "Checking Ring history, frames and the witness…";
  const order = `LAB-${r.tl.scenario}-${Date.now().toString(36)}`;
  let res = await fetch("/house/v1/claims", {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${config.merchant_key}` },
    body: JSON.stringify({ order_id: order, claimed_at: r.tl.claim_at_ms, carrier: "Lab Courier" }) });
  let body = await res.json();
  while (res.status === 202) {
    await new Promise((ok) => setTimeout(ok, 1000 * Math.max(1, body.retry_after_s)));
    res = await fetch(`/house/v1/claims/${body.claim_id}`, { headers: { Authorization: `Bearer ${config.merchant_key}` } });
    body = await res.json();
  }
  showVerdict(body, r.tl);
  if (body.receipt) await showReceipt(body.receipt);
  lockButtons(null);
}

function showVerdict(body, tl) {
  const v = $("verdict");
  if (!body.verdict) { v.className = "verdict"; v.textContent = `Claim ${body.status}: ${body.last_error || body.detail || ""}`; return; }
  const match = body.verdict === tl.expected.verdict && body.reason === tl.expected.reason;
  v.className = `verdict ${body.verdict}`;
  v.innerHTML = `<span class="big">${VERDICTS[body.verdict]}</span>${REASONS[body.reason] || body.reason}
    <div class="muted" style="margin-top:6px">reason: <code>${body.reason}</code> · ${match ? "<span class='check'>matches the scenario</span>" : "<span class='fail'>differs from the scenario's expected verdict</span>"}</div>`;
  if (body.receipt_id) {
    $("ev-before").src = `/lab/evidence/${body.receipt_id}/before`;
    $("ev-after").src = `/lab/evidence/${body.receipt_id}/after`;
    $("ev-before").onerror = $("ev-after").onerror = () => { $("evidence").hidden = true; };
    $("evidence").hidden = false;
  }
}

const b64url = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0));

async function showReceipt(token) {
  const [h, p, sig] = token.split(".");
  const header = JSON.parse(new TextDecoder().decode(b64url(h)));
  const payload = JSON.parse(new TextDecoder().decode(b64url(p)));
  const jwks = await (await fetch("/house/.well-known/jwks.json")).json();
  const jwk = jwks.keys.find((k) => k.kid === header.kid);
  let verdict;
  try {
    const key = await crypto.subtle.importKey("jwk", { kty: "OKP", crv: "Ed25519", x: jwk.x }, { name: "Ed25519" }, false, ["verify"]);
    const ok = await crypto.subtle.verify({ name: "Ed25519" }, key, b64url(sig), new TextEncoder().encode(`${h}.${p}`));
    verdict = ok ? "<span class='check'>Signature verified in this browser</span>" : "<span class='fail'>Signature does not verify</span>";
  } catch (e) {
    verdict = `<span class='muted'>This browser cannot check Ed25519 signatures (${e.name}).</span>`;
  }
  $("receipt").className = "";
  $("receipt").innerHTML = `${verdict} · Ed25519 · key <code>${header.kid.slice(0, 12)}…</code><br>
    <code>${token.slice(0, 96)}…</code>
    <div class="muted" style="margin-top:6px">${payload.evidence.length} evidence hashes, no frames, device pseudonym <code>${payload.device}</code></div>`;
  $("payload").textContent = JSON.stringify(payload, null, 2);
  $("payload-box").hidden = false;
}

init();
