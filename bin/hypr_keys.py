"""Write read-aloud's Hyprland keys and reader-window rule (bin/install.sh runs this).

The keys come from the add-on's options. A key that Omarchy (or anything else)
already uses is refused, never taken over. The keys live in their own file,
~/.config/hypr/hisho_read_aloud.lua, required once from bindings.lua.
"""
import json, os, subprocess, sys

HOME = os.path.expanduser("~")
HYPR = f"{HOME}/.config/hypr"
LUA = f"{HYPR}/hisho_read_aloud.lua"
BINDINGS = f"{HYPR}/bindings.lua"
REQUIRE = 'require("hypr.hisho_read_aloud")  -- Hisho add-on read-aloud'
SAY_IT = f"{os.environ['ADDON']}/bin/say-it"
PREFIX = "Read aloud: "
DEFAULTS = {"key_read": "SUPER + ALT + T", "key_repeat": "SUPER + ALT + R", "key_stop": "SUPER + ALT + X",
            "key_mute": "SUPER + ALT + M", "key_reader": "SUPER + ALT + V", "key_open": "SUPER + ALT + O",
            "key_pause": "SUPER + ALT + P", "key_rename": "SUPER + ALT + N",
            "key_skip": "SUPER + ALT + J"}
KEYS = [  # option, description, say-it argument
    ("key_read", "read selection (or stop)", ""),
    ("key_repeat", "repeat the last run", " --repeat"),
    ("key_stop", "stop", " --stop"),
    ("key_mute", "mute/unmute Claude replies", " --mute"),
    ("key_reader", "reader window (open/close)", " --toggle"),
    ("key_open", "open the thread being read", " --open-thread"),
    ("key_pause", "pause / resume", " --pause"),
    ("key_rename", "rename the thread", " --rename"),
    ("key_skip", "skip to the next reply", " --skip"),
]
RULE = ('o.window({ class = "^org.omarchy.claude-reader$" }, '
        '{ float = true, pin = true, size = { 720, 420 }, no_initial_focus = true })')
MODS = {1: "SHIFT", 4: "CTRL", 8: "ALT", 64: "SUPER"}


def hyprctl(*args):
    return subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=10).stdout


def combo(keys):
    parts = [p.strip().upper() for p in keys.split("+")]
    return frozenset(parts[:-1]), parts[-1]


def write(lines):
    with open(LUA + ".tmp", "w") as f:
        f.write("-- Written by the Hisho add-on read-aloud (bin/install.sh); edit its settings, not this file.\n")
        f.write("\n".join(lines) + "\n")
    os.replace(LUA + ".tmp", LUA)


def main():
    conf = {**DEFAULTS, **{k: os.environ[f"HISHO_OPT_{k.upper()}"]
                           for k in DEFAULTS if os.environ.get(f"HISHO_OPT_{k.upper()}")}}

    # bindings.lua requires the add-on's file, once, at the end
    text = open(BINDINGS).read().splitlines()
    kept = [l for l in text if l != REQUIRE]
    while kept and not kept[-1].strip():
        kept.pop()
    kept += ["", REQUIRE]
    # first only the window rule, so the check below sees every other key
    write([RULE])
    if kept != text:
        open(BINDINGS + ".tmp", "w").write("\n".join(kept) + "\n")
        os.replace(BINDINGS + ".tmp", BINDINGS)
    hyprctl("reload")

    taken = {}
    binds = json.loads(hyprctl("binds", "-j") or "[]")
    if not binds:
        sys.exit("Hyprland listed no keys at all, so nothing can be checked: keys not bound")
    for b in binds:
        mods = frozenset(v for bit, v in MODS.items() if b["modmask"] & bit)
        taken[(mods, b["key"].upper())] = b.get("description") or b.get("dispatcher") or "?"
    lines, refused = [RULE, ""], []
    for opt, desc, arg in KEYS:
        keys = conf[opt]
        if not keys or keys.lower() == "none":
            continue
        if combo(keys) in taken:
            refused.append(f"{keys} ({opt}) is already used for: {taken[combo(keys)]}")
            continue
        if arg in (" --open-thread", " --pause", " --skip"):   # the key names itself in read-aloud.log
            arg += " " + "+".join(k.strip().capitalize() for k in keys.split("+")) + " key"
        lines.append(f'o.bind("{keys}", "{PREFIX}{desc}", "{SAY_IT}{arg}")')
        print(f"{keys}: {desc}")
    write(lines)
    hyprctl("reload")
    errors = hyprctl("configerrors").strip()
    if errors:
        print("Hyprland config errors:\n" + errors, file=sys.stderr)
        sys.exit(1)
    if refused:
        print("Not bound, pick a free key in the add-on's settings:\n  " + "\n  ".join(refused), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
