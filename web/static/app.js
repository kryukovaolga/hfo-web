// Page logic: file list, parameter form <-> config dict (same keys as the YAML config of
// hfo-port), output folder, run queue. All computation happens in worker.js; the channels
// of a file are shared among several workers (one Python each) to use more CPU cores.
// LabChart .adicht files are converted first (one EDF per block, on the browser's private
// disk), then each EDF is analyzed like a recording added by hand.

const $ = (s) => document.querySelector(s);
const form = $("#cfg");

// ---- workers ----------------------------------------------------------------------------
// Each Engine is one worker with its own Python. engines[0] always runs (it also reads the
// file headers); more are started when a file is analyzed. Cancelling terminates them all
// (Python cannot be interrupted) and starts engines[0] again.
class Engine {
  constructor(clean = false) {
    this.pending = new Map();
    this.nextId = 1;
    this.worker = new Worker("worker.js", { type: "module" });
    this.worker.onerror = (e) => { e.preventDefault(); fail(e.message || "the analysis engine could not start"); };
    this.worker.onmessage = ({ data }) => {
      const p = this.pending.get(data.id);
      if (!p) return;
      if (data.log) return p.onLog?.(data.log);
      this.pending.delete(data.id);
      data.error ? p.reject(new Error(data.error)) : p.resolve(data.result);
    };
    this.ready = this.call("init", { clean });
  }
  call(type, payload = {}, onLog) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, onLog });
      this.worker.postMessage({ type, id, ...payload });
    });
  }
  stop() {
    this.worker.terminate();
    for (const p of this.pending.values()) p.reject(new Error("cancelled"));
    this.pending.clear();
  }
}
let engines = [];
const call = (type, payload, onLog) => engines[0].call(type, payload, onLog);
function startEngines() {
  engines = [new Engine(true)];
  return engines[0].ready;
}
async function useEngines(k) {
  while (engines.length < k) engines.push(new Engine());
  await Promise.all(engines.slice(0, k).map((e) => e.ready));
  return engines.slice(0, k);
}

// ---- default settings (config dict, same keys as the YAML configuration of hfo-port) ----
// Common settings, then one row per band; each enabled band is a separate analysis with its
// own two output files. Not in the form, always used: duplicate channel names as HFOApp
// (only the first is analyzed), HFOApp's header values, header with the parameters used.
const HEADER_DEFAULTS = { onset: 3, inclusion: 5, cycles: 3, baseline: "5 s" };
const DEFAULTS = {
  detector: "hfoapp_hilbert", bp_type: "fir", epoch: 600,
  notch: { enabled: false, freq: 60, type: "fir", bandwidth: 4 },
  channels: "all", duplicate_labels: "hfoapp",
};
const DEFAULT_BANDS = [
  { enabled: true, name: "HFO", freq: [100, 250], event_type: "ripple",
    z_thresh: 2, peak_thresh: 2, nocycle_thresh: 4, sd_thresh: 5, min_dur_ms: 10 },
  { enabled: true, name: "pHFO", freq: [250, 800], event_type: "fastripple",
    z_thresh: 3, peak_thresh: 3, nocycle_thresh: 4, sd_thresh: 5, min_dur_ms: 10 },
];
const DETECTOR_PARAMS = {
  hfoapp_hilbert: ["z_thresh", "peak_thresh", "nocycle_thresh"],
  ripplelab_hilbert: ["sd_thresh", "min_dur_ms"],
};
const EVENT_TYPES = ["gamma", "ripple", "fastripple", "ultrafast", "spike"];

// ---- form <-> config ------------------------------------------------------------------
const f = (name) => form.elements[name];
function number(input, what) {
  const v = input.value.trim();
  if (v === "" || !Number.isFinite(Number(v))) throw new Error(`${what} is not a number`);
  return Number(v);
}
const num = (name) => number(f(name), `“${f(name).closest("label").firstChild.textContent.trim()}”`);
let channelChoice = "all"; // "all" or list of labels

function bandRow(b) {
  const keyed = (e, key) => { e.dataset.key = key; return e; };
  const input = (key, value, type = "number") => keyed(el("input", { type, step: "any", value }), key);
  const cell = (child, det) => { const td = el("td", {}, [child]); if (det) td.dataset.det = det; return td; };
  const type = keyed(el("select", {}, EVENT_TYPES.map((t) => el("option", { value: t, textContent: t }))), "event_type");
  type.value = b.event_type;
  const cells = [cell(keyed(el("input", { type: "checkbox", checked: b.enabled }), "enabled")),
    cell(input("name", b.name, "text")), cell(input("lo", b.freq[0])), cell(input("hi", b.freq[1])), cell(type)];
  for (const [det, keys] of Object.entries(DETECTOR_PARAMS)) for (const k of keys) cells.push(cell(input(k, b[k]), det));
  return el("tr", {}, cells);
}

function setForm() {
  f("detector").value = DEFAULTS.detector;
  f("bp_type").value = DEFAULTS.bp_type;
  f("epoch_mode").value = DEFAULTS.epoch === "whole" ? "whole" : "sec";
  f("epoch_sec").value = DEFAULTS.epoch === "whole" ? "" : DEFAULTS.epoch;
  f("notch").checked = DEFAULTS.notch.enabled;
  f("notch_freq").value = DEFAULTS.notch.freq;
  f("notch_bw").value = DEFAULTS.notch.bandwidth;
  f("notch_type").value = DEFAULTS.notch.type;
  $("#bands tbody").replaceChildren(...DEFAULT_BANDS.map(bandRow));
  channelChoice = DEFAULTS.channels;
  renderChannels();
  syncForm();
}

// One config dict per enabled band: [{name, freq, cfg}]
function getConfigs() {
  const det = f("detector").value;
  const epoch = f("epoch_mode").value === "whole" ? "whole" : num("epoch_sec");
  const common = {
    detector: det,
    notch: { enabled: f("notch").checked, freq: num("notch_freq"), type: f("notch_type").value, bandwidth: num("notch_bw") },
    channels: readChannels(), duplicate_labels: DEFAULTS.duplicate_labels, header: HEADER_DEFAULTS,
  };
  const bands = [];
  for (const row of $("#bands tbody").rows) {
    const get = (k) => row.querySelector(`[data-key="${k}"]`);
    if (!get("enabled").checked) continue;
    const name = get("name").value.trim().replace(/[^A-Za-z0-9-]+/g, "-");
    if (!name) throw new Error("Each band needs a name");
    if (bands.some((b) => b.name === name)) throw new Error(`Two bands are called “${name}”`);
    const lo = number(get("lo"), `${name}: low frequency`), hi = number(get("hi"), `${name}: high frequency`);
    if (!(lo < hi)) throw new Error(`${name}: the low frequency must be below the high one`);
    const params = { epoch };
    for (const k of DETECTOR_PARAMS[det]) params[k] = number(get(k), `${name}: ${k}`);
    bands.push({ name, freq: [lo, hi], cfg: {
      ...common, event_type: get("event_type").value,
      bandpass: { freq: [lo, hi], type: f("bp_type").value }, params } });
  }
  if (!bands.length) throw new Error("Select at least one band");
  return bands;
}

// Output names: <recording>_<band name>_<low>-<high>Hz_events.txt / _summary.txt
function outputNames(name, band) {
  const stem = `${name.replace(/\.[^.]*$/, "")}_${band.name}_${band.freq[0]}-${band.freq[1]}Hz`;
  return { events: `${stem}_events.txt`, summary: `${stem}_summary.txt` };
}

function syncForm() {
  const det = f("detector").value;
  form.querySelectorAll("[data-det]").forEach((el) => (el.hidden = el.dataset.det !== det));
  $(".epoch-sec").hidden = f("epoch_mode").value !== "sec";
  for (const n of ["notch_freq", "notch_bw", "notch_type"]) f(n).disabled = !f("notch").checked;
}
form.addEventListener("change", syncForm);

// ---- channels ---------------------------------------------------------------------------
function allLabels() {
  const seen = [];
  for (const it of items) for (const l of it.info?.labels ?? []) if (!seen.includes(l)) seen.push(l);
  return seen;
}
function renderChannels() {
  const labels = allLabels();
  const box = $("#channels");
  if (!labels.length) {
    box.innerHTML = channelChoice === "all" ? "<em>All channels. Add a recording to choose.</em>"
      : `<em>From config: ${channelChoice.join(", ")}</em>`;
    return;
  }
  const chosen = channelChoice === "all" ? labels : channelChoice;
  box.innerHTML = "";
  const all = el("label", {}, [el("input", { type: "checkbox", id: "chAll" }), "all"]);
  box.append(all);
  for (const l of labels) {
    box.append(el("label", {}, [el("input", { type: "checkbox", value: l, checked: chosen.includes(l) }), l]));
  }
  const boxes = () => [...box.querySelectorAll("input[value]")];
  const syncAll = () => ($("#chAll").checked = boxes().every((b) => b.checked));
  $("#chAll").addEventListener("change", (e) => boxes().forEach((b) => (b.checked = e.target.checked)));
  boxes().forEach((b) => b.addEventListener("change", syncAll));
  syncAll();
}
function readChannels() {
  const boxes = [...$("#channels").querySelectorAll("input[value]")];
  if (!boxes.length) return channelChoice;
  const on = boxes.filter((b) => b.checked).map((b) => b.value);
  if (!on.length) throw new Error("Select at least one channel");
  return on.length === boxes.length ? "all" : on;
}

// ---- files ------------------------------------------------------------------------------
const items = []; // {file, info, row, status}
function el(tag, props = {}, children = []) {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...children);
  return e;
}
const size = (b) => (b > 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`);
const isAdicht = (file) => /\.adicht$/i.test(file.name);
// The .adicht reader keeps about 2.1 times the file in memory (measured);
// WebAssembly has at most 4 GB.
const ADICHT_WARN = 1.2e9, ADICHT_MAX = 1.85e9;
const hms = (s) => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min ${Math.round(s % 60)} s`;
};

async function addFiles(list) {
  for (const file of list) {
    if (!/\.(edf|adicht)$/i.test(file.name) || items.some((i) => i.file.name === file.name)) continue;
    const it = { file, info: null };
    it.status = el("td", { textContent: "reading…" });
    const rm = el("button", { type: "button", textContent: "✕", title: "Remove" });
    it.row = el("tr", {}, [el("td", { textContent: file.name }), el("td", { textContent: size(file.size) }),
      el("td"), el("td"), it.status, el("td", {}, [rm])]);
    rm.onclick = () => { items.splice(items.indexOf(it), 1); it.row.remove(); refresh(); };
    items.push(it);
    $("#files tbody").append(it.row);
    $("#files").hidden = false;
    try {
      it.info = isAdicht(file) ? adichtInfo(file, await call("inspectAdicht", { file })) : await call("inspect", { file });
      it.row.children[2].textContent = hms(it.info.duration_s);
      it.row.children[3].textContent = `${it.info.labels.length} · ${it.info.srate ?? "?"} Hz`;
      setStatus(it, it.info.problem ? `cannot process: ${it.info.problem}` : it.info.note ?? "ready",
        it.info.problem ? "err" : it.info.warn ? "warn" : "");
    } catch (e) {
      setStatus(it, `not a readable ${isAdicht(file) ? "LabChart" : "EDF"} file: ${e.message}`, "err");
    }
    refresh();
  }
}
function setStatus(it, text, cls = "") { it.status.textContent = text; it.status.className = cls; }

// What the list shows for a .adicht: its blocks become separate EDF files when it runs.
function adichtInfo(file, a) {
  const b = a.blocks, first = b[0];
  const info = { adicht: a, labels: first?.labels ?? [], srate: first?.srate, problem: a.problem,
    duration_s: b.reduce((s, x) => s + x.duration_s, 0) };
  const comments = b.reduce((s, x) => s + x.n_comments, 0);
  info.note = `ready · LabChart${b.length > 1 ? `, ${b.length} blocks` : ""}, ${comments} comments`;
  if (!info.problem && b.some((x) => x.labels.join() !== first.labels.join() || x.srate !== first.srate)) {
    info.problem = "blocks with different channels or sampling rates";
  }
  if (!info.problem && file.size > ADICHT_MAX) {
    info.problem = `too large for the browser (${size(file.size)}, limit ${size(ADICHT_MAX)}): use the command line (hfo-pipeline)`;
  } else if (file.size > ADICHT_WARN) {
    info.warn = true;
    info.note += ` · large file: needs about ${size(2.1 * file.size)} of memory to convert`;
  }
  return info;
}

const pick = $("#pick"), drop = $("#drop");
pick.onchange = () => { addFiles([...pick.files]); pick.value = ""; };
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); addFiles([...e.dataTransfer.files]); };

// ---- output -----------------------------------------------------------------------------
let outDir = null;
const canPickDir = "showDirectoryPicker" in window;
if (!canPickDir) { $("#outPick").hidden = true; $("#outNote").hidden = false; }
$("#outPick").onclick = async () => {
  try {
    outDir = await window.showDirectoryPicker({ mode: "readwrite", id: "hfo-out" });
    $("#outName").textContent = outDir.name;
  } catch { /* cancelled */ }
};

async function exists(dir, name) {
  try { await dir.getFileHandle(name); return true; } catch { return false; }
}
// data: text, or a Blob / File (a converted EDF, streamed from the private disk)
async function saveFile(name, data) {
  if (outDir) {
    const h = await outDir.getFileHandle(name, { create: true });
    const w = await h.createWritable();
    await w.write(data);
    await w.close();
    return;
  }
  const blob = data instanceof Blob ? data : new Blob([data], { type: "text/plain" });
  const a = el("a", { href: URL.createObjectURL(blob), download: name });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 60_000);
}

async function convertedFile(dirName, name) {
  const root = await navigator.storage.getDirectory();
  return (await (await root.getDirectoryHandle(dirName)).getFileHandle(name)).getFile();
}
async function removeConverted(dirName) {
  try { await (await navigator.storage.getDirectory()).removeEntry(dirName, { recursive: true }); } catch { /* gone */ }
}

// ---- run --------------------------------------------------------------------------------
const logBox = $("#log");
function log(line) { logBox.textContent += `${line}\n`; logBox.scrollTop = logBox.scrollHeight; }

const startBtn = $("#start"), cancelBtn = $("#cancel"), overall = $("#overall");
let ready = false, running = false, cancelled = false;
function refresh() {
  renderChannels();
  const todo = items.filter((i) => i.info && !i.info.problem).length;
  startBtn.disabled = !ready || running || !todo;
  startBtn.textContent = running ? "Running…" : todo ? `Start (${todo} file${todo === 1 ? "" : "s"})` : "Start";
  cancelBtn.hidden = !running;
}

cancelBtn.onclick = () => {
  if (!confirm("Stop the analysis? The file being processed will have no results.")) return;
  cancelled = true;
  engines.forEach((e) => e.stop());
  ready = false; refresh();
  engineStatus("Restarting the analysis engine…");
  startEngines().then(() => { ready = true; refresh(); engineStatus(""); }).catch((e) => fail(e.message));
};

const mmss = (s) => (s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`
  : s >= 60 ? `${Math.round(s / 60)} min` : `${Math.round(s)} s`);

// How many workers for one file: CPU cores, memory and, for long recordings, temporary disk.
// - up to 10 M samples per channel a channel is processed in memory: ~180 bytes per sample;
// - above, block processing: ~2 GB of memory per worker (measured: 3 workers on a 24 h,
//   4 kHz file took 5.9 GB) and 16 bytes per sample of disk (32 with notch), freed after
//   each channel.
// Memory budget: 60% of navigator.deviceMemory (GB, rounded by the browser; Chrome 157
// reports 16 on a 16 GB Mac; browsers without it are treated as 8 GB) → 16 GB: 3 workers
// for 6 channels, 8 GB: 2.
const WHOLE_MAX = 10_000_000;
const MAX_WORKERS = Number(new URLSearchParams(location.search).get("workers")) || 4; // ?workers=1 for tests
// The result is the fewest workers that need the same number of rounds (channels / workers,
// rounded up): with 6 channels and room for 4, 3 workers take 2 rounds like 4 would, with
// less memory and disk.
async function workersFor(it, nChannels, notch) {
  const cap = await workerCap(it, nChannels, notch);
  const rounds = Math.ceil(nChannels / cap);
  return Math.ceil(nChannels / rounds);
}
async function workerCap(it, nChannels, notch) {
  const n = it.info.n_samples;
  let k = Math.max(1, Math.min(MAX_WORKERS, (navigator.hardwareConcurrency || 2) - 1, nChannels));
  if (n <= WHOLE_MAX) {
    const budget = (navigator.deviceMemory || 8) * 0.4e9;
    return Math.max(1, Math.min(k, Math.floor(budget / (180 * n))));
  }
  const budget = (navigator.deviceMemory || 8) * 0.6e9;
  k = Math.max(1, Math.min(k, Math.floor(budget / 2e9)));
  if (!navigator.storage?.estimate) return k;
  const need = await call("scratchBytes", { n, notch });
  const { quota = 0, usage = 0 } = await navigator.storage.estimate();
  const fit = Math.floor((quota - usage) / need);
  if (fit < 1) throw new Error(`not enough disk space: needs ${size(need)} of temporary space, the browser allows ${size(quota - usage)}`);
  return Math.min(k, fit);
}

// One EDF ({file, info}): plan the channels, give them out one at a time to the workers, merge.
async function analyze(it, cfg, strict, onProgress) {
  const labels = await call("plan", { file: it.file, cfg });
  const k = await workersFor(it, labels.length, cfg.notch.enabled);
  const pool = await useEngines(k);
  log(`    ${labels.length} channels, ${k} worker${k > 1 ? "s" : ""}`);
  const frac = Object.fromEntries(labels.map((l) => [l, 0]));
  const seen = new Set();
  const onLog = (m) => {
    if (m.progress) { frac[m.progress.channel] = m.progress.fraction; return onProgress(frac); }
    const line = `    ${m.level === "INFO" ? "" : m.level + " "}${m.msg}`;
    if (!seen.has(line)) { seen.add(line); log(line); }
  };
  const queue = [...labels], parts = [];
  await Promise.all(pool.map(async (engine) => {
    while (queue.length && !cancelled) {
      const label = queue.shift();
      parts.push(await engine.call("runChannels", { file: it.file, cfg, labels: [label] }, onLog));
      frac[label] = 1; onProgress(frac);
    }
  }));
  if (cancelled) throw new Error("cancelled");
  return call("writeResults", { file: it.file, cfg, strict, parts });
}

// Files written for one input: for a .adicht, the converted EDF (if kept) and its tables per
// block, then the event files of every block and band.
function plannedNames(it, bands) {
  const edfs = it.info.adicht ? it.info.adicht.blocks.map((b) => b.edf) : [it.file.name];
  const names = [];
  for (const edf of edfs) {
    const stem = edf.replace(/\.edf$/i, "");
    if (it.info.adicht) {
      if ($("#saveEdf").checked) names.push(edf);
      names.push(...["events.tsv", "events.json", "channels.tsv", "recording.json"].map((x) => `${stem}_${x}`));
    }
    for (const band of bands) { const n = outputNames(edf, band); names.push(n.events, n.summary); }
  }
  return names;
}

// All bands on one EDF ({file, info}); label prefixes the status for blocks of a .adicht.
async function runBands(it, edf, bands, strict, t, label = "") {
  const counts = [];
  for (const [b, band] of bands.entries()) {
    log(`  ${label}${band.name} (${band.freq[0]}–${band.freq[1]} Hz)`);
    const res = await analyze(edf, band.cfg, strict, (frac) => {
      const v = Object.values(frac), part = v.reduce((x, y) => x + y, 0) / v.length;
      const all = (b + part) / bands.length, secs = (performance.now() - t) / 1000;
      const eta = all > 0.02 ? ` · about ${mmss(secs * (1 - all) / all)} left` : "";
      setStatus(it, `${label}${band.name}: ${Math.floor(part * 100)}% · total ${Math.floor(all * 100)}%${eta}`);
      overall.textContent = `${edf.file.name}: ${Math.floor(all * 100)}%${eta}`;
    });
    const names = outputNames(edf.file.name, band);
    await saveFile(names.events, res.events);
    await saveFile(names.summary, res.summary);
    log(`    ${res.n_events} events in ${res.n_channels} channels → ${names.events}, ${names.summary}`);
    counts.push(`${label}${band.name} ${res.n_events}`);
  }
  return counts;
}

// .adicht: convert (EDF per block on the private disk), save the tables (and the EDF), analyze.
async function runAdicht(it, bands, strict, t) {
  setStatus(it, "converting…");
  const names = it.info.adicht.blocks.map((b) => b.edf);
  const conv = await call("convertAdicht", { file: it.file, names }, (m) => {
    if (m.convert) {
      const pct = Math.floor(m.convert.fraction * 100);
      setStatus(it, `converting${names.length > 1 ? ` block ${m.convert.block}` : ""}: ${pct}%`);
      overall.textContent = `${it.file.name}: converting ${pct}%`;
    } else if (m.msg) log(`    ${m.level === "INFO" ? "" : m.level + " "}${m.msg}`);
  });
  try {
    log(`  converted in ${mmss((performance.now() - t) / 1000)}: ${conv.blocks.map((b) => `${b.edf} (${b.n_comments} comments)`).join(", ")}`);
    const counts = [];
    for (const b of conv.blocks) {
      if (cancelled) throw new Error("cancelled");
      const file = await convertedFile(conv.dir, b.edf);
      for (const [name, text] of Object.entries(b.sidecars)) await saveFile(name, text);
      if ($("#saveEdf").checked) await saveFile(b.edf, file);
      counts.push(...await runBands(it, { file, info: { n_samples: b.n_samples } }, bands, strict, t,
        conv.blocks.length > 1 ? `${b.edf}: ` : ""));
    }
    return counts;
  } finally {
    await removeConverted(conv.dir);
  }
}

startBtn.onclick = async () => {
  let bands;
  try { bands = getConfigs(); } catch (e) { return alert(e.message); }
  const strict = false;
  const todo = items.filter((i) => i.info && !i.info.problem);
  if (outDir) {
    const clash = [];
    for (const it of todo) for (const n of plannedNames(it, bands)) if (await exists(outDir, n)) clash.push(n);
    if (clash.length && !confirm(`These files already exist in “${outDir.name}” and will be replaced:\n\n${clash.join("\n")}`)) return;
  }
  running = true; cancelled = false; refresh();
  const lock = await navigator.wakeLock?.request("screen").catch(() => null);
  const t0 = performance.now();
  let done = 0;
  for (const it of todo) {
    if (cancelled) { setStatus(it, "cancelled"); continue; }
    setStatus(it, "starting…");
    log(`▶ ${it.file.name}`);
    const t = performance.now();
    try {
      const counts = it.info.adicht ? await runAdicht(it, bands, strict, t) : await runBands(it, it, bands, strict, t);
      setStatus(it, `✓ ${counts.join(", ")} events`, "ok");
      log(`  done in ${mmss((performance.now() - t) / 1000)}`);
      done++;
    } catch (e) {
      setStatus(it, cancelled ? "cancelled" : `error: ${e.message}`, cancelled ? "" : "err");
      log(cancelled ? "  cancelled" : `  ERROR ${e.message}`);
    }
  }
  log(`Done: ${done}/${todo.length} files in ${mmss((performance.now() - t0) / 1000)}` +
      (outDir ? ` (saved in “${outDir.name}”)` : " (saved in Downloads)"));
  overall.textContent = "";
  lock?.release();
  running = false; refresh();
};

// ---- start ------------------------------------------------------------------------------
function engineStatus(text) { const e = $("#engine"); e.textContent = text; e.hidden = !text; }
function fail(msg) {
  engineStatus(`The analysis engine could not start: ${msg}. An internet connection is needed the first time.`);
}

setForm();
if (location.protocol === "file:") {
  $("#fileWarning").hidden = false;
  engineStatus("");
} else {
  startEngines().then((v) => {
    ready = true; refresh(); engineStatus("");
    $("#versions").textContent = `Analysis engine: Pyodide ${v.pyodide} (Python ${v.python})`;
  }).catch((e) => fail(e.message));
}
