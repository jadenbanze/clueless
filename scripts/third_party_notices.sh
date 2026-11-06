#!/usr/bin/env bash
# Writes THIRD_PARTY_NOTICES.txt for everything that ships inside Clueless.app,
# and fails if any shipped dependency's license is not in
# .github/license-allowlist.txt.
#
# What ships, and therefore what is listed:
#   - Python packages installed in the bundled runtime (native/build/python),
#     read with importlib.metadata under that interpreter, so dev-only tools
#     on the build machine never appear;
#   - the CPython runtime itself;
#   - Electron, plus production npm dependencies of overlay/ (currently none).
#
# Usage: scripts/third_party_notices.sh [--check]   (--check: verify only, write nothing)
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
OUT="$ROOT/THIRD_PARTY_NOTICES.txt"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

PYTHON="${CLUELESS_PYTHON:-$ROOT/native/build/python/bin/python3}"
if [ ! -x "$PYTHON" ]; then
    echo "third_party_notices: $PYTHON not found; run native/build-python.sh first" >&2
    echo "  (or set CLUELESS_PYTHON to the interpreter that will be bundled)" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Python: one JSON object per distribution.
# -B: this is the interpreter that ships; don't leave bytecode in it.
"$PYTHON" -B - > "$WORK/python.json" <<'PY'
import json
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

CLASSIFIERS = {
    "MIT License": "MIT",
    "BSD License": "BSD-3-Clause",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "GNU General Public License (GPL)": "GPL",
    "GNU General Public License v2 (GPLv2)": "GPL-2.0",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0",
    "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.0-or-later",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "GNU Affero General Public License v3": "AGPL-3.0",
}
SHORT = {"mit": "MIT", "bsd": "BSD-3-Clause", "bsd-3-clause": "BSD-3-Clause",
         "bsd-2-clause": "BSD-2-Clause", "apache 2.0": "Apache-2.0", "apache-2.0": "Apache-2.0",
         "apache license 2.0": "Apache-2.0", "apache software license": "Apache-2.0",
         "isc": "ISC", "mpl-2.0": "MPL-2.0", "psf": "PSF-2.0", "psf-2.0": "PSF-2.0"}

def license_of(dist):
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or []
                   if c.startswith("License :: ")]
    mapped = sorted({CLASSIFIERS.get(c, c) for c in classifiers})
    if mapped:
        return " OR ".join(mapped)
    raw = (meta.get("License") or "").strip()
    if raw and "\n" not in raw and len(raw) < 60:
        return SHORT.get(raw.lower(), raw)
    return "UNKNOWN"

def license_texts(dist):
    texts = []
    for f in dist.files or []:
        name = Path(str(f)).name.upper()
        if name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")) and ".dist-info/" in str(f):
            try:
                texts.append(Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return texts

out = []
for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
    name = dist.metadata["Name"]
    if name.lower() in {"pip", "setuptools", "wheel"}:
        continue
    out.append({"ecosystem": "python", "name": name, "version": dist.version,
                "license": license_of(dist), "texts": license_texts(dist)})

stdlib = Path(sysconfig.get_paths()["stdlib"])
cpython = stdlib / "LICENSE.txt"
out.append({"ecosystem": "runtime", "name": "CPython", "version": sys.version.split()[0],
            "license": "Python-2.0",
            "texts": [cpython.read_text(encoding="utf-8", errors="replace")] if cpython.exists() else []})
json.dump(out, sys.stdout)
PY

# npm: Electron (a devDependency, but its runtime is the app) plus production deps.
node - "$ROOT/overlay" > "$WORK/node.json" <<'JS'
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const overlay = process.argv[2];
const read = (dir) => JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'));
const texts = (dir) => fs.readdirSync(dir)
  .filter((f) => /^(licen[cs]e|copying|notice)/i.test(f))
  .map((f) => fs.readFileSync(path.join(dir, f), 'utf8'));

let dirs = [];
try {
  dirs = execFileSync('npm', ['ls', '--omit=dev', '--all', '--parseable'], { cwd: overlay, encoding: 'utf8' })
    .split('\n').filter(Boolean).slice(1);
} catch (error) {
  process.stderr.write(`npm ls failed: ${error.message}\n`);
  process.exit(1);
}
dirs.unshift(path.join(overlay, 'node_modules', 'electron'));
const out = dirs.map((dir) => {
  const pkg = read(dir);
  const license = typeof pkg.license === 'string' ? pkg.license : 'UNKNOWN';
  return { ecosystem: 'npm', name: pkg.name, version: pkg.version, license, texts: texts(dir) };
});
process.stdout.write(JSON.stringify(out));
JS

python3 - "$WORK" "$ROOT/.github/license-allowlist.txt" "$OUT" "$CHECK_ONLY" <<'PY'
import json
import re
import sys
from pathlib import Path

work, allowlist_path, out_path, check_only = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4] == "1"

allowed, overrides = set(), {}
for line in allowlist_path.read_text().splitlines():
    line = line.split("#", 1)[0].strip()
    if not line:
        continue
    if "=" in line:
        name, spdx = (part.strip() for part in line.split("=", 1))
        overrides[name.lower()] = spdx
    else:
        allowed.add(line)

def permitted(expr: str) -> bool:
    expr = expr.strip("() ")
    alternatives = re.split(r"\s+OR\s+", expr)
    for alt in alternatives:
        parts = [p.strip("() ") for p in re.split(r"\s+AND\s+", alt)]
        if all(p in allowed for p in parts):
            return True
    return False

packages = json.loads((work / "python.json").read_text()) + json.loads((work / "node.json").read_text())
failures = []
for pkg in packages:
    pkg["license"] = overrides.get(pkg["name"].lower(), pkg["license"])
    if not permitted(pkg["license"]):
        failures.append(f'{pkg["ecosystem"]}: {pkg["name"]} {pkg["version"]} — {pkg["license"]}')

if failures:
    print("third_party_notices: disallowed or unknown licenses:", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    print("Allow the license in .github/license-allowlist.txt only after checking it is compatible,", file=sys.stderr)
    print("or add a `package = SPDX` override if the metadata is just unclear.", file=sys.stderr)
    sys.exit(1)

print(f"third_party_notices: {len(packages)} components, all licenses allowed")
if check_only:
    sys.exit(0)

rule = "=" * 78
lines = [
    "Third-party software in Clueless",
    "",
    "Clueless is MIT-licensed (see LICENSE). It bundles the components below,",
    "each under its own license. Chromium's notices ship with the Electron",
    "framework inside the app (LICENSES.chromium.html).",
    "",
]
for pkg in sorted(packages, key=lambda p: (p["ecosystem"], p["name"].lower())):
    lines += [rule, f'{pkg["name"]} {pkg["version"]} ({pkg["ecosystem"]}) — {pkg["license"]}', rule, ""]
    for text in pkg["texts"] or ["(No license file was distributed with this package; see its license identifier above.)"]:
        lines += [text.rstrip(), ""]
Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {out_path}")
PY
