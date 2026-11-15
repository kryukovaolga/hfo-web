"""Build the browser app into web/dist/ (and optionally serve it on localhost).

    python3 web/build.py                       # -> web/dist/, Pyodide from the jsDelivr CDN
    python3 web/build.py --self-host-pyodide   # Pyodide files copied into dist/pyodide/
    python3 web/build.py --serve               # build, then http://localhost:8000

dist/ = static/* + hfo_port.zip (src/hfo_port and the browser adapter py/hfo_web.py)
+ user-guide.pdf (from docs/, if present, with a link in the page). Placeholders filled in
index.html / worker.js: version, build (git commit), source URL, guide link,
Content-Security-Policy, Pyodide location.

--self-host-pyodide downloads the pinned Pyodide release once (cached in web/.pyodide-cache/)
and checks every file: the core files against the SHA-256 below, the package wheels against
the hashes in the (checked) pyodide-lock.json. Standard library only.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent
SRC = ROOT / "src" / "hfo_port"
GUIDE = ROOT / "docs" / "user-guide.pdf"
DIST = WEB / "dist"
CACHE = WEB / ".pyodide-cache"

PYODIDE_VERSION = "314.0.7"
PYODIDE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
PYODIDE_CORE = {  # SHA-256, checked against the npm package of the same version
    "pyodide.mjs": "6f1d60f7bf529beb300f0f47983c921d3982363640ba20af0e38efdddbc66109",
    "pyodide.asm.mjs": "f7cdc8ece80678ceb712f8e65ebe6d3a83203a180c399865f49612a051693635",
    "pyodide.asm.wasm": "cc36e3cab04fdfc9a63ff13eb52eae2b911bf46c025cc7b281f394bd3de1d5e6",
    "python_stdlib.zip": "fa1957e5777068fc4f7437f96d860ae2fbe9c19732ba06c84e004ec16dd7dd7a",
    "pyodide-lock.json": "5dc2fc119108bc148c7457dc86e7675b5c87e1cafd420b9c34c1eaef7b36c010",
}
PYODIDE_PACKAGES = ("numpy", "scipy", "pyyaml")  # with their dependencies, from the lock file


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(name: str, sha256: str) -> Path:
    """Download one Pyodide file into the cache (once) and check its hash."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / name
    if not path.exists() or _sha256(path) != sha256:
        print(f"  downloading {name}")
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            with urllib.request.urlopen(PYODIDE_CDN + name) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
        except urllib.error.URLError:
            # e.g. python.org Python on macOS without its certificates installed; the
            # hash check below makes the transport irrelevant
            subprocess.run(["curl", "-fsSL", "-o", str(tmp), PYODIDE_CDN + name], check=True)
        if _sha256(tmp) != sha256:
            tmp.unlink()
            raise SystemExit(f"SHA-256 mismatch for {name}: refusing to publish it")
        tmp.replace(path)
    return path


def self_host_pyodide(dest: Path) -> None:
    files = dict(PYODIDE_CORE)
    lock = json.loads(_fetch("pyodide-lock.json", PYODIDE_CORE["pyodide-lock.json"]).read_text())
    todo, seen = list(PYODIDE_PACKAGES), set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        pkg = lock["packages"][name]
        files[pkg["file_name"]] = pkg["sha256"]
        todo += pkg.get("depends", [])
    dest.mkdir()
    for name, sha in files.items():
        shutil.copy2(_fetch(name, sha), dest / name)


def _commit() -> str:
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha[:7]
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "local"


def _csp(self_hosted: bool) -> str:
    """Content-Security-Policy of the page: nothing loaded from or sent to other sites
    (except the Pyodide CDN when it is not self-hosted). Workers are not covered by it:
    a worker's policy can only come from HTTP headers."""
    cdn = "" if self_hosted else " https://cdn.jsdelivr.net"
    return ("default-src 'self'; "
            f"script-src 'self'{cdn}; connect-src 'self'{cdn}; worker-src 'self'{cdn}; "
            "style-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; form-action 'none'")


def build(self_host: bool = False, source_url: str = "") -> Path:
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.copytree(WEB / "static", DIST)
    with zipfile.ZipFile(DIST / "hfo_port.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(SRC.glob("*.py")):
            z.write(p, f"hfo_port/{p.name}")
        z.write(WEB / "py" / "hfo_web.py", "hfo_web.py")
    if GUIDE.exists():
        shutil.copy2(GUIDE, DIST / "user-guide.pdf")
    if self_host:
        self_host_pyodide(DIST / "pyodide")

    version = re.search(r'__version__ = "([^"]+)"', (SRC / "__init__.py").read_text()).group(1)
    fill = {
        "index.html": {"__CSP__": _csp(self_host), "__VERSION__": version,
                       "__BUILD__": _commit(), "__SOURCE__": source_url or "#",
                       "__GUIDE_LINK__": ('<a href="user-guide.pdf" target="_blank" rel="noopener">'
                                          'User guide (PDF)</a> · ') if GUIDE.exists() else ""},
        "worker.js": {"__PYODIDE_URL__": "./pyodide/" if self_host else PYODIDE_CDN},
    }
    for name, values in fill.items():
        text = (DIST / name).read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace(key, value)
        (DIST / name).write_text(text, encoding="utf-8")
    return DIST


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-host-pyodide", action="store_true",
                    help="copy the pinned Pyodide files into dist/ instead of loading them from the CDN")
    ap.add_argument("--source-url", default="", help="link to the source code shown in the page")
    ap.add_argument("--serve", action="store_true", help="serve dist/ on localhost after building")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    out = build(args.self_host_pyodide, args.source_url)
    print(f"built {out}")
    if args.serve:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
        with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
            print(f"open http://localhost:{args.port}  (Ctrl+C to stop)")
            srv.serve_forever()


if __name__ == "__main__":
    main()
