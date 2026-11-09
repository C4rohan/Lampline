#!/usr/bin/env bash
# The version lives in VERSION at the repo root; the packages carry copies of it.
#
#   scripts/version.sh              print it
#   scripts/version.sh check        fail if any copy differs (CI runs this)
#   scripts/version.sh set 0.6.0    write it everywhere (and the citation's release date)
#
# Merging into prod releases VERSION if it isn't tagged yet (.github/workflows/release.yml),
# so bumping it on dev is how a release starts.
set -euo pipefail
cd "$(dirname "$0")/.."                  # the repo root, wherever this is run from

case "${1:-}" in
  ""|print) cat VERSION ;;
  check|set)
    command -v python3 >/dev/null || { echo "version.sh: needs python3" >&2; exit 1; }
    python3 - "$@" <<'EOF'
import datetime, json, re, sys
from pathlib import Path

mode = sys.argv[1]
want = Path("VERSION").read_text().strip()
if mode == "set":
    if len(sys.argv) < 3 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[2]):
        sys.exit("usage: scripts/version.sh set 1.2.3")
    want = sys.argv[2]

# file -> (pattern whose group 2 is the version, replacement template)
TEXT = {
    "cli/pyproject.toml": r'(?m)^(version = ")([^"]+)(")',
    "engine/pyproject.toml": r'(?m)^(version = ")([^"]+)(")',
    "cli/src/vk/__init__.py": r'(?m)^(__version__ = ")([^"]+)(")',
    "engine/src/vpengine/__init__.py": r'(?m)^(__version__ = ")([^"]+)(")',
    "CITATION.cff": r"(?m)^(version: )(\S+)()",
}
bad = []
for name, pattern in TEXT.items():
    path = Path(name)
    text = path.read_text()
    found = re.search(pattern, text)
    if not found:
        sys.exit(f"version.sh: no version line in {name}")
    if mode == "set":
        text = re.sub(pattern, lambda m: m.group(1) + want + m.group(3), text, count=1)
        if name == "CITATION.cff":
            text = re.sub(r"(?m)^date-released: .*$", f"date-released: {datetime.date.today()}", text, count=1)
        path.write_text(text)
    elif found.group(2) != want:
        bad.append(f"{name}: {found.group(2)}")

for name in ("engine/ui/package.json", "engine/ui/package-lock.json"):
    path = Path(name)
    doc = json.loads(path.read_text())
    have = [doc.get("version")] + ([doc["packages"][""].get("version")] if "packages" in doc else [])
    if mode == "set":
        doc["version"] = want
        if "packages" in doc:
            doc["packages"][""]["version"] = want
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    elif any(v != want for v in have):
        bad.append(f"{name}: {', '.join(map(str, have))}")

if mode == "set":
    Path("VERSION").write_text(want + "\n")
    print(f"version {want} written; commit it on dev, and merging it into prod releases v{want}")
elif bad:
    sys.exit(f"version.sh: VERSION says {want}, but:\n  " + "\n  ".join(bad) + "\nrun: scripts/version.sh set " + want)
else:
    print(f"version {want}: every copy matches")
EOF
    ;;
  *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
