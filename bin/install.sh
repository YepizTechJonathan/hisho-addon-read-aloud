#!/usr/bin/env bash
# Install read-aloud on this machine (run by `hisho addon install read-aloud`, in the add-on folder).
# Safe to run again: it only redoes what is missing or changed.
#   - Kokoro in its own venv, the voice model (checked by sha256)
#   - ~/.config/hisho/read-aloud.json from the add-on's options
#   - say-it on PATH, the Claude Code Stop hook, the Hyprland keys and reader-window rule
set -euo pipefail
ADDON="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$HOME/.local/share/hisho/read-aloud"
MODELS="$DATA/models"
KOKORO_ONNX="${HISHO_OPT_KOKORO_ONNX:-0.4.7}"
MODEL_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
declare -A SHA=(
  [kokoro-v1.0.onnx]=7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5
  [voices-v1.0.bin]=bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d
)

echo "== Kokoro venv"
if ! "$DATA/venv/bin/python" -c "import kokoro_onnx, importlib.metadata as m; assert m.version('kokoro-onnx') == '$KOKORO_ONNX'" 2>/dev/null; then
  mkdir -p "$DATA"
  python3 -m venv "$DATA/venv"
  "$DATA/venv/bin/pip" install -q --disable-pip-version-check "kokoro-onnx==$KOKORO_ONNX"
fi
echo "kokoro-onnx $KOKORO_ONNX"

echo "== Voice model"
mkdir -p "$MODELS"
for f in "${!SHA[@]}"; do
  if [ "$(sha256sum "$MODELS/$f" 2>/dev/null | cut -d' ' -f1)" = "${SHA[$f]}" ]; then
    echo "$f ok"; continue
  fi
  curl -fsSL -m 900 -o "$MODELS/$f.part" "$MODEL_URL/$f"
  if [ "$(sha256sum "$MODELS/$f.part" | cut -d' ' -f1)" != "${SHA[$f]}" ]; then
    rm -f "$MODELS/$f.part"; echo "$f: sha256 does not match, not installed" >&2; exit 1
  fi
  mv "$MODELS/$f.part" "$MODELS/$f"; echo "$f installed"
done

echo "== Settings, say-it, Claude Code hook"
mkdir -p "$HOME/.local/bin"
ln -sfn "$ADDON/bin/say-it" "$HOME/.local/bin/say-it"
ADDON="$ADDON" python3 - <<'EOF'
import json, os
addon = os.environ["ADDON"]
home = os.path.expanduser("~")

# Settings for read_aloud.py
keys = ["voice", "speed", "opacity", "auto_open", "announce_thread", "auto_pause", "title_size",
        "key_read", "key_repeat", "key_stop", "key_mute", "key_reader", "key_open", "key_pause", "key_rename", "key_skip"]
conf = {}
for k in keys:
    v = os.environ.get(f"HISHO_OPT_{k.upper()}")
    if v is None:
        continue
    conf[k] = (v.lower() in ("1", "true", "yes", "on")) if k in ("auto_open", "announce_thread", "auto_pause") else (int(v) if k in ("speed", "opacity", "title_size") else v)
path = f"{home}/.config/hisho/read-aloud.json"
try:
    before = json.load(open(path))
except Exception:
    before = {}
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(conf, open(path, "w"), indent=2)
# A new opacity in the add-on's settings wins over the slider's last value
if "opacity" in conf and conf.get("opacity") != before.get("opacity"):
    try:
        os.remove(f"{home}/.local/state/hisho/read-aloud/opacity")
    except OSError:
        pass
print("settings:", conf)

# Claude Code Stop hook (replaces an earlier read-aloud hook, wherever its folder was)
path = f"{home}/.claude/settings.json"
try:
    data = json.load(open(path))
except FileNotFoundError:
    data = {}
command = f"python3 {addon}/read_aloud.py hook"
mine = lambda h: "read_aloud.py hook" in h.get("command", "")
groups = []
for g in data.setdefault("hooks", {}).get("Stop", []):
    g = {**g, "hooks": [h for h in g.get("hooks", []) if not mine(h)]}
    if g["hooks"]:
        groups.append(g)
groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
data["hooks"]["Stop"] = groups
tmp = path + ".tmp"
os.makedirs(os.path.dirname(path), exist_ok=True)   # a new machine: Claude Code comes later, the hook waits for it
json.dump(data, open(tmp, "w"), indent=2)
open(tmp, "a").write("\n")
os.replace(tmp, path)
print("Stop hook:", command)
EOF

echo "== Hyprland keys"
if ! command -v hyprctl >/dev/null || [ ! -f "$HOME/.config/hypr/bindings.lua" ]; then
  echo "no Hyprland (Omarchy) here: no keys; say-it still works"; exit 0
fi
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export HYPRLAND_INSTANCE_SIGNATURE="${HYPRLAND_INSTANCE_SIGNATURE:-$(ls -t "$XDG_RUNTIME_DIR/hypr" 2>/dev/null | head -1)}"
if [ -z "$HYPRLAND_INSTANCE_SIGNATURE" ] || ! hyprctl version >/dev/null 2>&1; then
  # keys are only bound when Hyprland can say which keys are taken
  echo "Hyprland isn't running (not logged in yet?): keys skipped; Apply again after logging in"; exit 0
fi
ADDON="$ADDON" python3 "$ADDON/bin/hypr_keys.py"
