// Runs Pyodide + hfo_port off the main thread. The EDF File is mounted read-only (WORKERFS):
// Python reads it in pieces, the browser never uploads it anywhere.
//
// Long recordings use block processing, whose full-length scratch arrays go to the
// browser's private disk (Origin Private File System), one file per array, through
// synchronous access handles (only available in workers).

// The Pyodide location in the two lines below is filled by build.py: "./pyodide/" (files
// served with the app) or the CDN.
import { loadPyodide, version as PYODIDE_VERSION } from "__PYODIDE_URL__pyodide.mjs";
// Pyodide 314.0.7: Python 3.14, numpy 2.4.6, scipy 1.18.0, PyYAML 6.0.3

const SCRATCH_PREFIX = "hfo-scratch-";

let py = null;
let api = null;
let scratch = null; // {name: FileSystemSyncAccessHandle}, opened at the first run

async function init() {
  await removeOldScratch();
  py = await loadPyodide({ indexURL: "__PYODIDE_URL__" });
  await py.loadPackage(["numpy", "scipy", "pyyaml"]);
  const zip = await (await fetch("hfo_port.zip", { cache: "no-cache" })).arrayBuffer();
  py.unpackArchive(zip, "zip");
  api = py.pyimport("hfo_web");
  const v = py.runPython("import numpy, scipy, sys; f'{sys.version.split()[0]}, numpy {numpy.__version__}, scipy {scipy.__version__}'");
  return { hfo_port: api.VERSION, pyodide: PYODIDE_VERSION, python: v };
}

// Scratch folders left by runs that were cancelled or crashed (a cancelled run is a
// terminated worker, which releases its handles). Folders still in use by another tab
// cannot be removed and are skipped.
async function removeOldScratch() {
  try {
    const root = await navigator.storage.getDirectory();
    for await (const name of root.keys()) {
      if (name.startsWith(SCRATCH_PREFIX)) {
        try { await root.removeEntry(name, { recursive: true }); } catch { /* in use */ }
      }
    }
  } catch { /* no OPFS: block processing will use memory */ }
}

async function openScratch() {
  if (scratch) return scratch;
  try {
    const root = await navigator.storage.getDirectory();
    const dir = await root.getDirectoryHandle(SCRATCH_PREFIX + crypto.randomUUID(), { create: true });
    const handles = {};
    for (const name of api.SCRATCH_NAMES) {
      const fh = await dir.getFileHandle(name, { create: true });
      handles[name] = await fh.createSyncAccessHandle();
    }
    scratch = handles;
  } catch {
    scratch = null;
  }
  return scratch;
}

// One file at a time (messages are handled in order). The path appears in the
// "Data file:" header line: the browser does not reveal the real folder.
function withFile(file, fn) {
  const dir = "/input";
  py.FS.mkdirTree(dir);
  py.FS.mount(py.FS.filesystems.WORKERFS, { files: [file] }, dir);
  try {
    return fn(`${dir}/${file.name}`);
  } finally {
    py.FS.unmount(dir);
  }
}

const handlers = {
  init: () => init(),
  inspect: ({ file }) => JSON.parse(withFile(file, (p) => api.inspect(p))),
  scratchBytes: ({ n, notch }) => api.scratch_bytes(n, notch),
  plan: ({ file, cfg }) => JSON.parse(withFile(file, (p) => api.plan(p, JSON.stringify(cfg)))),
  // The result stays a JSON string (it may contain NaN): it goes back to writeResults as is.
  runChannels: async ({ id, file, cfg, labels }) => {
    const handles = await openScratch();
    const post = (s) => self.postMessage({ id, log: JSON.parse(s) });
    return withFile(file, (p) => api.run_channels(p, JSON.stringify(cfg), JSON.stringify(labels), post, handles));
  },
  writeResults: ({ file, cfg, strict, parts }) =>
    JSON.parse(withFile(file, (p) => api.write_results(p, JSON.stringify(cfg), strict, "/output", parts))),
};

self.onmessage = async ({ data }) => {
  try {
    const result = await handlers[data.type](data);
    self.postMessage({ id: data.id, result });
  } catch (err) {
    // Python exceptions: keep only the last line ("ValueError: ...")
    const msg = String(err.message || err).trim().split("\n").pop();
    self.postMessage({ id: data.id, error: msg });
  }
};
