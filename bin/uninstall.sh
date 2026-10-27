#!/usr/bin/env bash
# Undo bin/install.sh: stop reading, remove the Stop hook, the keys and say-it.
# Keeps the venv and voice model (~/.local/share/hisho/read-aloud) so a reinstall is quick.
set -euo pipefail
ADDON="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$ADDON/read_aloud.py" stop || true
python3 - <<'EOF'
import json, os
path = os.path.expanduser("~/.claude/settings.json")
try:
    data = json.load(open(path))
except FileNotFoundError:
    raise SystemExit
groups = []
for g in data.get("hooks", {}).get("Stop", []):
    g = {**g, "hooks": [h for h in g.get("hooks", []) if "read_aloud.py hook" not in h.get("command", "")]}
    if g["hooks"]:
        groups.append(g)
if groups:
    data["hooks"]["Stop"] = groups
else:
    data.get("hooks", {}).pop("Stop", None)
json.dump(data, open(path + ".tmp", "w"), indent=2)
open(path + ".tmp", "a").write("\n")
os.replace(path + ".tmp", path)
print("Stop hook removed")
EOF
[ "$(readlink -f "$HOME/.local/bin/say-it" 2>/dev/null)" = "$ADDON/bin/say-it" ] && rm -f "$HOME/.local/bin/say-it"
B="$HOME/.config/hypr/bindings.lua"
if [ -f "$B" ]; then
  sed -i '/require("hypr.hisho_read_aloud")/d' "$B"
  rm -f "$HOME/.config/hypr/hisho_read_aloud.lua"
  command -v hyprctl >/dev/null && hyprctl reload >/dev/null || true
  echo "keys removed"
fi
