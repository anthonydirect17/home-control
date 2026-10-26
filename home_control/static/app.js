// Home Control page. Talks to the local API in server.py.
"use strict";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const SWATCHES = ["#FFB46B", "#FFFFFF", "#FF0000", "#00C000", "#0040FF", "#8B00FF", "#FF7F00"];

const S = { devices: [], groups: {}, presets: {}, patterns: [], schedules: [], settings: {}, status: {} };

// ---------- helpers ----------

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${res.status})`);
  }
  return data;
}

let toastTimer = null;
function toast(msg, bad = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = bad ? "bad" : "";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 2500);
}

// Send a command and report per-device failures.
async function command(path, body, okMsg) {
  try {
    const r = await api("POST", path, body);
    const errs = Object.entries(r.errors || {});
    if (errs.length) toast(errs.map(([n, e]) => `${n}: ${e}`).join(" · "), true);
    else if (okMsg) toast(okMsg);
    pollDevices();
    return r;
  } catch (e) {
    toast(e.message, true);
  }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
}

function paintStrip(strip, colors, count) {
  const n = Math.max(1, count || (colors ? colors.length : 1));
  if (strip.children.length !== n) {
    strip.replaceChildren(...Array.from({ length: n }, () => el("span")));
  }
  [...strip.children].forEach((s, i) => {
    s.style.backgroundColor = colors && colors.length ? colors[i % colors.length] : "";
  });
}

function targetOptions(select, { includeDevices = true } = {}) {
  const current = select.value;
  select.replaceChildren();
  const groups = Object.keys(S.groups);
  if (groups.length) {
    select.append(el("optgroup", { label: "Groups" }, groups.map(g => el("option", { value: `group:${g}`, text: g }))));
  }
  if (includeDevices && S.devices.length) {
    select.append(el("optgroup", { label: "Lights" }, S.devices.map(d => el("option", { value: `device:${d.id}`, text: d.name }))));
  }
  if ([...select.options].some(o => o.value === current)) select.value = current;
}

function targetLabel(target) {
  const [kind, ...rest] = target.split(":");
  const name = rest.join(":");
  return kind === "group" ? `${name} (group)` : deviceName(name);
}

// ---------- shared control block for a light or a group ----------

function controlBlock(target, { effects = true, scenesFor = null } = {}) {
  const bright = el("input", { type: "range", min: 1, max: 100, value: 100, "aria-label": "Brightness" });
  bright.addEventListener("change", () => command("/api/control/brightness", { target, value: +bright.value }));

  const color = el("input", { type: "color", value: "#ffb46b", "aria-label": "Color" });
  color.addEventListener("change", () => command("/api/control/color", { target, color: color.value }));

  const fx = el("select", { "aria-label": "Effect", "data-fx": "1" });
  fillEffectSelect(fx);

  return el("div", { class: "controls" },
    el("div", { class: "row" },
      el("div", { class: "btn-pair" },
        el("button", { class: "small", text: "On", onclick: () => command("/api/control/power", { target, on: true }) }),
        el("button", { class: "small ghost", text: "Off", onclick: () => command("/api/control/power", { target, on: false }) })),
      el("label", { class: "grow" }, "Brightness", bright)),
    el("div", { class: "swatches" }, color,
      SWATCHES.map(c => el("button", {
        class: "swatch", title: c, "aria-label": `Set color ${c}`, style: `background:${c}`,
        onclick: () => { color.value = c.toLowerCase(); command("/api/control/color", { target, color: c }); },
      }))),
    effects ? el("div", { class: "row" },
      el("label", { class: "grow" }, "Effect", fx),
      el("button", { class: "small", text: "Start", onclick: () => startEffect(target, fx.value) }),
      el("button", { class: "small ghost", text: "Stop", onclick: () => command("/api/control/stop", { target }) }))
      : el("p", { class: "hint", text: "Effects need LAN Control on for this light (Govee Home app → device → settings → LAN Control)." }),
    scenesFor ? sceneRow(target, scenesFor) : null);
}

// Govee's own scenes and your DIY scenes, loaded from the cloud the first time the list is opened.
function sceneRow(target, deviceId) {
  const select = el("select", { "aria-label": "Govee scene" }, el("option", { value: "", text: "Choose a Govee scene…" }));
  let loaded = false;
  const load = async () => {
    if (loaded) return;
    loaded = true;
    try {
      const scenes = await api("GET", `/api/devices/${encodeURIComponent(deviceId)}/scenes`);
      const kinds = [...new Set(scenes.map(x => x.kind))];
      select.replaceChildren(el("option", { value: "", text: scenes.length ? "Choose a Govee scene…" : "No scenes for this light" }),
        ...kinds.map(k => el("optgroup", { label: k }, scenes.filter(x => x.kind === k).map(x => el("option", { value: x.name, text: x.name })))));
    } catch (e) { loaded = false; toast(e.message, true); }
  };
  select.addEventListener("focus", load);
  select.addEventListener("pointerdown", load);
  return el("div", { class: "row" },
    el("label", { class: "grow" }, "Govee scene", select),
    el("button", { class: "small", text: "Run", onclick: () => {
      if (!select.value) return toast("Pick a scene first.", true);
      command("/api/control/scene", { target, scene: select.value }, `Scene: ${select.value}`);
    } }));
}

function fillEffectSelect(select) {
  const current = select.value;
  select.replaceChildren();
  const names = Object.keys(S.presets);
  if (names.length) {
    select.append(el("optgroup", { label: "Saved effects" }, names.map(n => el("option", { value: `preset:${n}`, text: n }))));
  }
  select.append(el("optgroup", { label: "Built in" }, S.patterns.map(p => el("option", { value: `pattern:${p.name}`, text: p.label }))));
  if ([...select.options].some(o => o.value === current)) select.value = current;
}

function defaultParams(patternName) {
  const p = S.patterns.find(x => x.name === patternName);
  return p ? Object.fromEntries(p.params.map(x => [x.key, x.default])) : {};
}

function startEffect(target, choice) {
  const [kind, ...rest] = choice.split(":");
  const name = rest.join(":");
  const body = kind === "preset" ? { target, preset: name } : { target, pattern: name, params: defaultParams(name) };
  return command("/api/control/effect", body, "Effect started");
}

// ---------- devices ----------

const ROUTE_LABELS = { lan: "LAN", cloud: "Cloud", desktop: "Govee Desktop", demo: "Demo" };

function deviceName(id) {
  const d = S.devices.find(x => x.id === id || x.name === id);
  return d ? d.name : id;
}

function renderDevices() {
  const list = document.getElementById("device-list");
  if (!S.devices.length) {
    list.replaceChildren(el("p", { class: "empty", text: S.status.connected
      ? "No lights found. Turn on LAN Control in the Govee Home app or add your Govee API key, then Refresh list."
      : "Not connected to Govee. See Settings." }));
    return;
  }
  list.replaceChildren(...S.devices.map(d => {
    const target = `device:${d.id}`;
    const nameBox = el("div", { class: "card-title", text: d.name });
    const renameInput = el("input", { value: d.name, maxlength: 60, "aria-label": "New name", hidden: true });
    const renameBtn = el("button", { class: "small ghost", text: "Rename" });
    renameBtn.addEventListener("click", async () => {
      if (renameInput.hidden) { renameInput.hidden = false; nameBox.hidden = true; renameBtn.textContent = "Save"; renameInput.focus(); return; }
      try {
        S.devices = await api("POST", `/api/devices/${encodeURIComponent(d.id)}/rename`, { name: renameInput.value });
        lastNames = ""; await pollDevices(); renderGroups();
      } catch (e) { toast(e.message, true); }
    });
    return el("article", { class: "card", "data-device": d.id },
      el("div", { class: "card-head" },
        el("div", {}, nameBox, renameInput,
          el("div", { class: "meta", text: `${d.model || "Govee"} · ${d.segments} segment${d.segments === 1 ? "" : "s"} · via ${d.routes.map(r => ROUTE_LABELS[r] || r).join(", ")}` })),
        el("div", { class: "row" }, el("span", { class: "chip", "data-chip": "1" }), renameBtn)),
      el("div", { class: "strip", "data-strip": "1" }),
      el("div", { class: "meta", "data-source": "1" }),
      controlBlock(target, { effects: d.can_effects, scenesFor: d.can_scenes ? d.id : null }),
      el("div", { class: "err", "data-err": "1" }));
  }));
  updateDeviceLive();
}

function updateDeviceLive() {
  for (const d of S.devices) {
    const card = document.querySelector(`[data-device="${CSS.escape(d.id)}"]`);
    if (!card) continue;
    const chip = card.querySelector("[data-chip]");
    let text = "Unknown", cls = "chip";
    if (d.effect) { text = `Effect: ${d.effect.pattern}`; cls = "chip ok"; }
    else if (d.scene && d.on !== false) { text = `Scene: ${d.scene}`; cls = "chip ok"; }
    else if (d.on === true) { text = d.brightness ? `On · ${d.brightness}%` : "On"; cls = "chip ok"; }
    else if (d.on === false) { text = "Off"; }
    chip.textContent = text;
    chip.className = cls;
    paintStrip(card.querySelector("[data-strip]"), d.on === false ? null : d.frame, d.segments);
    card.querySelector("[data-source]").textContent = d.reported ? "State reported by the light" : (d.on == null ? "" : "State as last sent from here");
    card.querySelector("[data-err]").textContent = d.error || "";
  }
}

let lastNames = "";
async function pollDevices() {
  try {
    S.devices = await api("GET", "/api/devices");
  } catch (_) { return; }
  const names = S.devices.map(d => `${d.id}|${d.name}|${d.routes.join("+")}|${d.can_effects}|${d.can_scenes}`).join(",");
  if (names !== lastNames) {
    lastNames = names;
    renderDevices();
    renderGroupForm();
    refreshTargetSelects();
  } else {
    updateDeviceLive();
  }
  updateGroupLive();
}

// ---------- groups ----------

function renderGroups() {
  const list = document.getElementById("group-list");
  const names = Object.keys(S.groups);
  if (!names.length) {
    list.replaceChildren(el("p", { class: "empty", text: "No groups yet." }));
    return;
  }
  list.replaceChildren(...names.map(name => {
    const g = S.groups[name];
    return el("article", { class: "card", "data-group": name },
      el("div", { class: "card-head" },
        el("div", {}, el("div", { class: "card-title", text: name }),
          el("div", { class: "meta", text: `${g.members.map(deviceName).join(", ")} · ${g.mode === "span" ? "linked" : "side by side"}` })),
        el("button", { class: "small ghost", text: "Delete", onclick: async () => {
          S.groups = await api("DELETE", `/api/groups/${encodeURIComponent(name)}`);
          renderGroups(); refreshTargetSelects();
        } })),
      el("div", { class: "strip", "data-strip": "1" }),
      controlBlock(`group:${name}`));
  }));
  updateGroupLive();
}

function updateGroupLive() {
  for (const [name, g] of Object.entries(S.groups)) {
    const card = document.querySelector(`[data-group="${CSS.escape(name)}"]`);
    if (!card) continue;
    const colors = [];
    let count = 0;
    for (const m of g.members) {
      const d = S.devices.find(x => x.id === m || x.name === m);
      if (!d) continue;
      count += d.segments;
      const frame = d.on === false || !d.frame ? Array(d.segments).fill("") : d.frame;
      colors.push(...frame.slice(0, d.segments), ...Array(Math.max(0, d.segments - frame.length)).fill(""));
    }
    paintStrip(card.querySelector("[data-strip]"), colors, count);
  }
}

function renderGroupForm() {
  const box = document.getElementById("group-members");
  box.replaceChildren(...S.devices.map(d =>
    el("label", {}, el("input", { type: "checkbox", value: d.id }), d.name)));
}

document.getElementById("group-form").addEventListener("submit", async e => {
  e.preventDefault();
  const name = document.getElementById("group-name").value.trim();
  const members = [...document.querySelectorAll("#group-members input:checked")].map(i => i.value);
  if (!members.length) return toast("Pick at least one light.", true);
  try {
    S.groups = await api("POST", "/api/groups", { name, members, mode: document.getElementById("group-mode").value });
    e.target.reset();
    renderGroups(); refreshTargetSelects();
    toast(`Saved group ${name}`);
  } catch (err) { toast(err.message, true); }
});

// ---------- effects builder ----------

const fxPattern = document.getElementById("fx-pattern");
const fxParams = document.getElementById("fx-params");
let previewFrames = [], previewIdx = 0, previewTimer = null, previewDebounce = null;

function renderPatternSelect() {
  fxPattern.replaceChildren(...S.patterns.map(p => el("option", { value: p.name, text: p.label })));
  renderParams();
}

function renderParams(values = null) {
  const p = S.patterns.find(x => x.name === fxPattern.value);
  if (!p) return;
  fxParams.replaceChildren(...p.params.map(spec => {
    const v = values && values[spec.key] !== undefined ? values[spec.key] : spec.default;
    const label = spec.label || spec.key.replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
    let input;
    if (spec.kind === "color") input = el("input", { type: "color", value: String(v).toLowerCase() });
    else if (spec.kind === "number") input = el("input", { type: "number", min: spec.min, max: spec.max, step: spec.step, value: v });
    else input = el("input", { type: "text", value: Array.isArray(v) ? v.join(",") : v, placeholder: "#FF0000,#FFFFFF" });
    input.dataset.key = spec.key;
    input.dataset.kind = spec.kind;
    input.addEventListener("input", schedulePreview);
    return el("label", {}, label, input);
  }));
  schedulePreview();
}

function readParams() {
  const out = {};
  for (const input of fxParams.querySelectorAll("input")) {
    out[input.dataset.key] = input.dataset.kind === "number" ? Number(input.value) : input.value;
  }
  return out;
}

function schedulePreview() {
  clearTimeout(previewDebounce);
  previewDebounce = setTimeout(loadPreview, 250);
}

async function loadPreview() {
  const segs = Math.max(1, Math.min(200, +document.getElementById("fx-segs").value || 20));
  try {
    previewFrames = await api("POST", "/api/preview", { pattern: fxPattern.value, params: readParams(), segments: segs, seconds: 12, fps: 8 });
  } catch (e) { toast(e.message, true); return; }
  previewIdx = 0;
  clearInterval(previewTimer);
  const strip = document.getElementById("fx-preview");
  const step = () => { if (previewFrames.length) paintStrip(strip, previewFrames[previewIdx++ % previewFrames.length]); };
  step();
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) previewTimer = setInterval(step, 125);
}

fxPattern.addEventListener("change", () => renderParams());
document.getElementById("fx-segs").addEventListener("input", schedulePreview);

document.getElementById("fx-run").addEventListener("click", () => {
  const target = document.getElementById("fx-target").value;
  if (!target) return toast("Pick lights to run it on.", true);
  command("/api/control/effect", { target, pattern: fxPattern.value, params: readParams() }, `Running on ${targetLabel(target)}`);
});
document.getElementById("fx-stop").addEventListener("click", () => {
  const target = document.getElementById("fx-target").value;
  if (target) command("/api/control/stop", { target }, "Stopped");
});

document.getElementById("effect-form").addEventListener("submit", async e => {
  e.preventDefault();
  const name = document.getElementById("fx-name").value.trim();
  if (!name) return toast("Give the effect a name first.", true);
  try {
    S.presets = await api("POST", "/api/presets", { name, pattern: fxPattern.value, params: readParams() });
    renderPresets(); refreshEffectSelects();
    toast(`Saved ${name}`);
  } catch (err) { toast(err.message, true); }
});

function renderPresets() {
  const list = document.getElementById("preset-list");
  const names = Object.keys(S.presets);
  if (!names.length) {
    list.replaceChildren(el("li", { class: "empty", text: "Nothing saved yet. Build one and press Save effect." }));
    return;
  }
  list.replaceChildren(...names.map(name => {
    const p = S.presets[name];
    const label = (S.patterns.find(x => x.name === p.pattern) || {}).label || p.pattern;
    return el("li", {},
      el("div", {}, el("div", { class: "card-title", text: name }), el("div", { class: "meta", text: label })),
      el("div", { class: "row" },
        el("button", { class: "small ghost", text: "Edit", onclick: () => {
          fxPattern.value = p.pattern; renderParams(p.params);
          document.getElementById("fx-name").value = name;
        } }),
        el("button", { class: "small ghost", text: "Delete", onclick: async () => {
          S.presets = await api("DELETE", `/api/presets/${encodeURIComponent(name)}`);
          renderPresets(); refreshEffectSelects();
        } })));
  }));
}

// ---------- timers ----------

function describeWhen(w) {
  if (w.type === "time") return `at ${w.time}`;
  const off = Number(w.offset || 0);
  const rel = off === 0 ? "" : off < 0 ? `${-off} min before ` : `${off} min after `;
  return `${rel}${w.type}`;
}

function describeAction(a) {
  switch (a.type) {
    case "on": return "turn on";
    case "off": return "turn off";
    case "brightness": return `brightness ${a.value}%`;
    case "color": return `color ${a.color}`;
    case "preset": return `start “${a.preset}”`;
    case "pattern": return `start ${a.pattern}`;
    case "stop": return "stop effect";
    default: return a.type;
  }
}

function describeDays(days) {
  if (!days || !days.length || days.length === 7) return "every day";
  const key = [...days].sort().join("");
  if (key === "01234") return "weekdays";
  if (key === "56") return "weekends";
  return [...days].sort().map(d => DAYS[d]).join(", ");
}

async function loadTimers() {
  S.schedules = await api("GET", "/api/schedules");
  const list = document.getElementById("timer-list");
  if (!S.schedules.length) {
    list.replaceChildren(el("li", { class: "empty", text: "No timers yet." }));
  } else {
    list.replaceChildren(...S.schedules.map(s => {
      const toggle = el("input", { type: "checkbox", checked: s.enabled, "aria-label": "Enabled" });
      toggle.addEventListener("change", async () => {
        try { await api("POST", "/api/schedules", Object.assign({}, s, { enabled: toggle.checked })); loadTimers(); }
        catch (e) { toast(e.message, true); }
      });
      const next = s.next_run ? new Date(s.next_run).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : (s.enabled ? "needs location (Settings)" : "off");
      return el("li", {},
        el("div", {},
          el("div", { class: "card-title", text: s.name }),
          el("div", { class: "meta", text: `${targetLabel(s.target)}: ${describeAction(s.action)} ${describeWhen(s.when)}, ${describeDays(s.days)} · next: ${next}` })),
        el("div", { class: "row" },
          el("label", { class: "inline" }, toggle, "On"),
          el("button", { class: "small ghost", text: "Run now", onclick: () => command(`/api/schedules/${s.id}/run`, null, "Done") }),
          el("button", { class: "small ghost", text: "Delete", onclick: async () => { await api("DELETE", `/api/schedules/${s.id}`); loadTimers(); } })));
    }));
  }
  const log = await api("GET", "/api/schedules/log");
  document.getElementById("timer-log").replaceChildren(...(log.length ? log.map(l =>
    el("li", {}, el("span", { text: `${new Date(l.at).toLocaleString()} · ${l.name}` }), el("span", { class: l.result === "OK" ? "meta" : "err", text: l.result })))
    : [el("li", { class: "empty", text: "Nothing has run yet." })]));
}

function renderTimerForm() {
  document.getElementById("t-days").replaceChildren(...DAYS.map((d, i) =>
    el("label", {}, el("input", { type: "checkbox", value: i, checked: true }), d)));
  syncTimerForm();
}

function syncTimerForm() {
  const action = document.getElementById("t-action").value;
  document.getElementById("t-value-wrap").hidden = action !== "brightness";
  document.getElementById("t-color-wrap").hidden = action !== "color";
  document.getElementById("t-preset-wrap").hidden = action !== "preset";
  const when = document.getElementById("t-when").value;
  document.getElementById("t-time-wrap").hidden = when !== "time";
  document.getElementById("t-offset-wrap").hidden = when === "time";
}
document.getElementById("t-action").addEventListener("change", syncTimerForm);
document.getElementById("t-when").addEventListener("change", syncTimerForm);

document.getElementById("timer-form").addEventListener("submit", async e => {
  e.preventDefault();
  const type = document.getElementById("t-action").value;
  const action = { type };
  if (type === "brightness") action.value = +document.getElementById("t-value").value;
  if (type === "color") action.color = document.getElementById("t-color").value;
  if (type === "preset") {
    action.preset = document.getElementById("t-preset").value;
    if (!action.preset) return toast("Save an effect first, on the Effects tab.", true);
  }
  const whenType = document.getElementById("t-when").value;
  const when = whenType === "time"
    ? { type: "time", time: document.getElementById("t-time").value }
    : { type: whenType, offset: +document.getElementById("t-offset").value || 0 };
  if (whenType !== "time" && (S.settings.latitude == null || S.settings.longitude == null)) {
    toast("Sunrise and sunset timers need your location. Add it on the Settings tab.", true);
  }
  const days = [...document.querySelectorAll("#t-days input:checked")].map(i => +i.value);
  if (!days.length) return toast("Pick at least one day.", true);
  const target = document.getElementById("t-target").value;
  if (!target) return toast("Pick lights for the timer.", true);
  try {
    await api("POST", "/api/schedules", { name: document.getElementById("t-name").value.trim(), enabled: true, days, when, target, action });
    e.target.reset(); renderTimerForm(); loadTimers();
    toast("Timer saved");
  } catch (err) { toast(err.message, true); }
});

// ---------- settings ----------

function renderSettings() {
  document.getElementById("s-lat").value = S.settings.latitude ?? "";
  document.getElementById("s-lon").value = S.settings.longitude ?? "";
  document.getElementById("s-fps").value = S.settings.default_fps ?? 4;
  document.getElementById("s-desktop").checked = S.settings.use_govee_desktop !== false;
}

document.getElementById("settings-form").addEventListener("submit", async e => {
  e.preventDefault();
  try {
    S.settings = await api("POST", "/api/settings", {
      latitude: document.getElementById("s-lat").value.trim(),
      longitude: document.getElementById("s-lon").value.trim(),
      default_fps: +document.getElementById("s-fps").value || 4,
      use_govee_desktop: document.getElementById("s-desktop").checked,
    });
    renderSettings(); loadTimers();
    toast("Settings saved");
  } catch (err) { toast(err.message, true); }
});

document.getElementById("s-locate").addEventListener("click", () => {
  if (!navigator.geolocation) return toast("This browser can't share its location. Type it in instead.", true);
  navigator.geolocation.getCurrentPosition(
    pos => {
      document.getElementById("s-lat").value = pos.coords.latitude.toFixed(4);
      document.getElementById("s-lon").value = pos.coords.longitude.toFixed(4);
      toast("Location filled in. Press Save settings.");
    },
    () => toast("Couldn't get the location. Type it in instead.", true));
});

document.getElementById("reconnect").addEventListener("click", async () => {
  try {
    await api("POST", "/api/reconnect");
    await loadStatus(); await pollDevices();
    toast(S.status.connected ? "Reconnected" : "Still not connected. See the details below.", !S.status.connected);
  } catch (e) { toast(e.message, true); }
});

async function loadStatus() {
  S.status = await api("GET", "/api/status");
  const backends = Object.values(S.status.backends || {});
  const up = backends.filter(b => b.connected);
  const chip = document.getElementById("conn");
  chip.textContent = up.length ? `${up.map(b => b.label).join(" + ")} · ${S.status.device_count} lights` : "Not connected";
  chip.className = `chip ${up.length === backends.length ? "ok" : up.length ? "warn" : "bad"}`;
  document.getElementById("s-status").replaceChildren(...backends.map(b =>
    el("div", { class: "status-line" },
      el("span", { class: `chip ${b.connected ? "ok" : "bad"}`, text: b.label }),
      el("span", { class: b.connected ? "meta" : "err", text: b.connected ? (b.error || b.note || "Connected") : (b.error || "Not connected") }))));
}

// ---------- tabs and startup ----------

function refreshTargetSelects() {
  targetOptions(document.getElementById("fx-target"));
  targetOptions(document.getElementById("t-target"));
}

function refreshEffectSelects() {
  document.querySelectorAll("select[data-fx]").forEach(fillEffectSelect);
  const tp = document.getElementById("t-preset");
  tp.replaceChildren(...Object.keys(S.presets).map(n => el("option", { value: n, text: n })));
}

function showTab(name) {
  document.querySelectorAll(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab").forEach(t => { t.hidden = t.id !== `tab-${name}`; });
  if (name === "timers") loadTimers().catch(e => toast(e.message, true));
  try { localStorage.setItem("hc-tab", name); } catch (_) { /* storage blocked */ }
}
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));

document.getElementById("refresh").addEventListener("click", async () => {
  try {
    S.devices = await api("POST", "/api/devices/refresh");
    lastNames = ""; await pollDevices(); await loadStatus();
    toast(`${S.devices.length} lights`);
  } catch (e) { toast(e.message, true); }
});

async function start() {
  try {
    [S.patterns, S.groups, S.presets, S.settings] = await Promise.all([
      api("GET", "/api/patterns"), api("GET", "/api/groups"), api("GET", "/api/presets"), api("GET", "/api/settings")]);
    await loadStatus();
  } catch (e) {
    toast(`Can't reach the app: ${e.message}`, true);
    return;
  }
  renderPatternSelect();
  renderPresets();
  renderSettings();
  renderTimerForm();
  await pollDevices();
  renderGroups();
  refreshTargetSelects();
  refreshEffectSelects();
  let tab = "devices";
  try { tab = localStorage.getItem("hc-tab") || tab; } catch (_) { /* storage blocked */ }
  showTab(tab);
  setInterval(pollDevices, 1500);
}

start();
