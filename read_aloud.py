#!/usr/bin/env python3
"""Read Claude's replies aloud with Kokoro, one after another (Hisho add-on read-aloud).

  read_aloud.py hook        Stop hook: queue Claude's last reply (JSON on stdin)
  read_aloud.py forward HOST  Stop hook on another machine (Kihon): send the reply to
                            HOST (the workstation) over ssh, read there
  read_aloud.py queue       queue one reply sent by forward (JSON on stdin)
  read_aloud.py say [text]  queue text (args, stdin, else selection/clipboard);
                            if something is being read, stop it instead
  read_aloud.py repeat      read the last run again from the start
  read_aloud.py stop        stop reading and drop what is queued
  read_aloud.py mute        turn reading of Claude's replies off/on
  read_aloud.py open        open the reader window (it also opens when reading starts)
  read_aloud.py toggle      close the reader window if open, else open it
  read_aloud.py open-thread open the thread being read on claude.ai (pauses reading)
  read_aloud.py pause       pause reading, or resume it from the start of the paused sentence
  read_aloud.py skip        end the reply being read; go to the next one waiting
  read_aloud.py name-thread NAME   name the thread being read (else the last one read)
  read_aloud.py rename      bring the reader forward with its rename prompt (Super+Alt+N)
  read_aloud.py opacity N   set the reader window's opacity, 20-100 (%)
  read_aloud.py test        read a test sentence
  read_aloud.py status      muted / reading / installed / audio output, as JSON
  read_aloud.py calibrate   time the voice on this computer (silently) and pick its cores
  read_aloud.py view        reader window: which thread is being read, with
                            the sentence being spoken highlighted and an
                            opacity slider; closes itself when reading finishes

A "run" is everything read back to back: a reply that arrives while another is
being read is added to the end of the same run, so repeat replays all of it.
Replies are read in full. Settings come from the add-on's options, written by
bin/install.sh to ~/.config/hisho/read-aloud.json.
"""
import fcntl, glob, json, os, re, select, shutil, signal, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
SAY_IT = os.path.join(HERE, "bin", "say-it")
CONFIG = os.path.expanduser("~/.config/hisho/read-aloud.json")
DATA = os.path.expanduser("~/.local/share/hisho/read-aloud")
STATE = os.path.expanduser("~/.local/state/hisho/read-aloud")
QUEUE = os.path.join(STATE, "queue")
RUN = os.path.join(STATE, "last-run.jsonl")
NOW = os.path.join(STATE, "now.json")
LOCK = os.path.join(STATE, "worker.lock")
PIDFILE = os.path.join(STATE, "worker.pid")
MUTE = os.path.join(STATE, "muted")
OPACITY = os.path.join(STATE, "opacity")      # the slider's last value
PY = os.path.join(DATA, "venv", "bin", "python")
MODELS = os.path.join(DATA, "models")
DESKTOP_SESSIONS = os.path.expanduser("~/.config/Claude/claude-code-sessions")
CLI_SESSIONS = os.path.expanduser("~/.claude/sessions")   # a running Claude Code: its claude.ai session (bridgeSessionId)
RATE = 24000
DEFAULTS = {"voice": "af_heart", "speed": 100, "opacity": 90, "auto_open": True,
            "key_read": "SUPER + ALT + T", "key_repeat": "SUPER + ALT + R", "key_stop": "SUPER + ALT + X",
            "key_mute": "SUPER + ALT + M", "key_reader": "SUPER + ALT + V", "key_open": "SUPER + ALT + O",
            "key_pause": "SUPER + ALT + P", "key_rename": "SUPER + ALT + N", "key_skip": "SUPER + ALT + J", "announce_thread": True, "auto_pause": True, "title_size": 2,
            "output": "", "fallback_output": "", "cpu_threads": "auto", "use_gpu": "auto", "low_priority": True}
LAST_THREAD = os.path.join(STATE, "last-thread")
PAUSED = os.path.join(STATE, "paused")
THREAD_WINDOW = os.path.join(STATE, "thread-window.json")
CALIBRATION = os.path.join(STATE, "calibration.json")   # how many cores the voice needs here, measured
RENAME = os.path.join(STATE, "rename")   # asks the reader to show its rename prompt   # the one window a thread opens in
NAMES = os.path.join(STATE, "thread-names.json")   # names typed in the reader or on the dashboard   # the thread the last reply came from
# Background colours for the thread band (256-colour terminal), one per thread
BANDS = (24, 89, 28, 130, 54, 30, 94, 125, 22, 60)


def settings():
    return {**DEFAULTS, **(read_json(CONFIG) or {})}


def key_label(keys):
    """"SUPER + ALT + R" -> "Super+Alt+R"."""
    return "+".join(k.strip().capitalize() for k in keys.split("+"))


def notify(msg, title="Read aloud"):
    try:
        subprocess.run(["notify-send", "-t", "3000", title, msg],
                       stderr=subprocess.DEVNULL, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # no notification daemon / notify-send: reading still works


def log(event):
    """One line per event in read-aloud.log (kept short), to see afterwards what happened."""
    path = os.path.join(STATE, "read-aloud.log")
    try:
        if os.path.getsize(path) > 200_000:
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {event}\n")
    except OSError:
        pass


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.rename(tmp, path)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def segments(text):
    """Make markdown speakable and split it into sentences.

    Returns [{"s": sentence, "nl": starts a new line on screen}]. Each line of
    the reply (list item, heading, table row) ends in a pause.
    """
    text = re.sub(r"```.*?```", " (code block) ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", " a link ", text)
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[#>]+|[-*+]|\d+\.)\s+", "", line)  # heading, quote, list marker
        line = re.sub(r"[*_#|]", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(r" ([,.;:!?])", r"\1", line)
        if not line or re.fullmatch(r"[-:\s]+", line):
            continue
        if not re.search(r"[.!?:;,]$", line):
            line += "."
        for i, s in enumerate(re.split(r"(?<=[.!?])\s+(?=\S)", line)):
            out.append({"s": s, "nl": i == 0})
    return out


def last_reply(data):
    text = data.get("last_assistant_message") or ""
    if text:
        return text
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    # Walk back to the last assistant message that has text
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        if isinstance(content, str):
            parts = [content]
        else:
            parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        text = "\n".join(p for p in parts if p.strip())
        if text:
            return text
    return ""


POST = re.compile(r"__(post_message|post_widget|send_message)$")   # what a project thread says to the person


def said(call):
    """What a thread's tool call says to the person, or "". A channel's thread speaks through
    send_message posted to the shared channel; a message only for the coordinator, or a
    progress checklist (live_status), is not read."""
    args = call.get("input") or {}
    if call.get("name", "").endswith("__send_message"):
        if args.get("visibility") != "posted_to_shared_channel" or args.get("live_status"):
            return ""
        return args.get("message") or ""
    return args.get("text") or ""


def is_thread(data):
    """A claude.ai project thread: it talks to the person through its hearthbot tools."""
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        return any("hearthbot__" in line or '"posted_to_shared_channel"' in line for line in f)


def reply_text(data):
    """What the person was told in this turn. A claude.ai project thread speaks only through
    its post_message tool (its own text is never shown), so a turn that posted nothing has
    nothing to read; any other session's reply is its last text."""
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return last_reply(data)
    if not is_thread(data):
        return last_reply(data)
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    posts = []
    failed = set()   # posts the person never saw (refused, then usually sent again)
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        content = entry.get("message", {}).get("content", "")
        if entry.get("type") == "user" and not entry.get("isMeta") and (
                isinstance(content, str) or not any(c.get("type") == "tool_result" for c in content)):
            break   # the start of this turn
        if entry.get("type") == "user":
            if isinstance(content, list):
                failed.update(c.get("tool_use_id") for c in content
                              if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"))
            continue
        if entry.get("type") != "assistant" or isinstance(content, str):
            continue
        for c in reversed(content):
            if c.get("type") == "tool_use" and POST.search(c.get("name", "")) and c.get("id") not in failed:
                text = said(c)
                if text.strip():
                    posts.append(text)
    return "\n\n".join(reversed(posts))


def forward(host, data):
    """Send this reply to the workstation to be read there. Never holds Claude up: ssh runs on
    its own and gives up after a few seconds when the workstation is off. Only replies meant for
    the person: a claude.ai project thread, or a session typed into in a terminal; never Hisho's own
    scripted `claude -p` runs (rating mail, writing notes) on that machine."""
    if not is_thread(data) and os.environ.get("CLAUDE_CODE_ENTRYPOINT") != "cli":
        return
    segs = segments(reply_text(data))
    if not segs:
        return
    t = thread(data)
    item = {"source": t["name"], "thread": t["key"], "url": t["url"], "asked": asked(data), "segments": segs}
    proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host,
                             "~/.local/bin/say-it --queue"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    proc.stdin.write(json.dumps(item).encode())
    proc.stdin.close()


def queue_item(item):
    """A reply sent by forward from another machine: queued like this machine's own."""
    if os.path.exists(MUTE) or not isinstance(item, dict):
        return False
    segs = [s for s in item.get("segments") or [] if isinstance(s, dict) and isinstance(s.get("s"), str)]
    if not segs:
        return False
    enqueue({"source": str(item.get("source") or ""), "thread": str(item.get("thread") or ""),
             "url": str(item.get("url") or ""), "asked": str(item.get("asked") or ""), "segments": segs})
    return True


GENERIC_TITLE = re.compile(r"^(Remote Control session|New session|Untitled)", re.I)


def shorten(text, limit):
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def prompts(data):
    """What was asked in this session, in order, as plain text."""
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"type":"user"' not in line.replace(" ", ""):
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, list):
                if any(c.get("type") == "tool_result" for c in content):
                    continue
                content = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
            if not content.strip() or entry.get("isMeta"):
                continue
            # Remote Control prompts arrive wrapped: keep only the person's message text
            messages = re.findall(r"<message\b[^>]*>(.*?)</message>", content, flags=re.S)
            if messages:
                content = messages[-1]
            elif content.lstrip().startswith("<"):
                continue  # a relay, notice or command output, not something asked
            content = re.sub(r"<system-reminder>.*?</system-reminder>", " ", content, flags=re.S)
            content = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", content)).strip()
            if content:
                out.append(content)
    return out


def asked(data):
    """The last thing asked in this session, shortened."""
    asked_ = prompts(data)
    return shorten(asked_[-1], 140) if asked_ else ""


def relay_title(data):
    """The latest "Thread: <title>" line in a note from the project's assistant (a relay),
    so the reader can use the name the thread has on claude.ai, renames included."""
    import html
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return ""
    title = ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "<relay" not in line or "Thread:" not in line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") != "user":
                continue
            content = entry.get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
            for note in re.findall(r"<relay\b[^>]*>.*?<note>(.*?)</note>", content, flags=re.S):
                found = re.search(r"^\s*Thread:\s*(.+?)\s*$", html.unescape(note), flags=re.M)
                if found:
                    title = found.group(1)
    return shorten(title, 120)


def thread_names():
    return read_json(NAMES) or {}


def name_thread(name, key=None):
    """Give the thread being read (else the last one read) a name that wins over everything."""
    if key is None:
        now = read_json(NOW) or {}
        key = now.get("thread") or next((i.get("thread") for i in reversed(run_items()) if i.get("thread")), "")
    name = re.sub(r"\s+", " ", name or "").strip()
    if not key or not name:
        return False
    names = thread_names()
    names[key] = name[:120]
    os.makedirs(STATE, exist_ok=True)
    write_json(NAMES, names)
    log(f"thread named: {name}")
    return True


def thread(data):
    """Which thread a reply comes from: {"name", "key", "url"}.

    The name is the session's title in the Claude desktop app. Sessions started from a
    claude.ai project thread are all titled "Remote Control session (from a project)" there
    (the thread's own title isn't kept on this machine). So the name is, first found: one
    typed in the reader or on the dashboard, the latest "Thread: <title>" line from the
    project's assistant, the desktop title, the thread's first message. The url opens the
    session on claude.ai."""
    sid = data.get("session_id") or ""
    record = {}
    for path in glob.glob(f"{DESKTOP_SESSIONS}/*/*/local_*.json"):
        d = read_json(path) or {}
        if sid and d.get("cliSessionId") == sid:
            record = d
            break
    title = thread_names().get(sid) or relay_title(data) or record.get("title") or ""
    if not title or GENERIC_TITLE.match(title):
        first = prompts(data)
        title = shorten(first[0], 70) if first else ""
    name = title or os.path.basename(record.get("cwd") or data.get("cwd") or "") or "Claude"
    remote = ((record.get("remoteControlSpawn") or {}).get("ccrSessionId")
              or next(iter(record.get("bridgeSessionIds") or []), ""))
    if not remote and sid:   # no desktop app here (Kihon): the running Claude Code says its claude.ai session
        for path in glob.glob(f"{CLI_SESSIONS}/*.json"):
            d = read_json(path) or {}
            if d.get("sessionId") == sid and d.get("bridgeSessionId"):
                remote = d["bridgeSessionId"]
                break
    url = ""
    if remote.startswith("cse_"):
        url = "https://claude.ai/code/session_" + remote[4:]
    elif remote.startswith("session_"):
        url = "https://claude.ai/code/" + remote
    return {"name": name, "key": sid or name, "url": url}


def worker_pid():
    try:
        pid = int(open(PIDFILE).read())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def enqueue(item):
    os.makedirs(QUEUE, exist_ok=True)
    name = str(time.time_ns())
    tmp = os.path.join(QUEUE, f".{name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(item, f)
    os.rename(tmp, os.path.join(QUEUE, f"{name}.json"))
    # Start a worker; if one is already reading it just exits and the
    # running one picks this up after the current text.
    subprocess.Popen([PY, os.path.abspath(__file__), "worker"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def queued():
    if not os.path.isdir(QUEUE):
        return []
    return sorted(os.path.join(QUEUE, n) for n in os.listdir(QUEUE) if n.endswith(".json"))


def stop(why="stop"):
    """Stop the worker and the replies waiting. They are added to the run, so Repeat
    still reads them: nothing is lost."""
    keep_waiting()
    try:
        os.remove(PAUSED)
    except OSError:
        pass
    pid = worker_pid()
    if not pid:
        return False
    log(f"stop asked by: {why}")
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        pass
    for _ in range(40):  # wait until it has let go of the lock
        if not worker_pid():
            break
        time.sleep(0.05)
    return True


def next_item():
    paths = queued()
    if not paths:
        return None
    item = read_json(paths[0])
    os.remove(paths[0])
    return item


def keep_waiting():
    """Add the replies waiting to the run and drop them from the queue, so Repeat still
    reads them: nothing is lost when reading stops."""
    dropped = [read_json(p) for p in queued()]
    if dropped and os.path.exists(RUN):
        with open(RUN, "a", encoding="utf-8") as f:
            for item in dropped:
                if item:
                    f.write(json.dumps(item) + "\n")
    for path in queued():
        os.remove(path)


def sinks(dump=None):
    """PipeWire's audio outputs from pw-dump, in its order: [{"name", "label", "state",
    "error", "default", "usb", "hdmi"}]. [] when pw-dump is missing or doesn't answer."""
    if dump is None:
        try:
            r = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=3, check=False)
            dump = json.loads(r.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return []
    default = ""
    for o in dump:
        if o.get("type", "").endswith("Metadata") and (o.get("props") or {}).get("metadata.name") == "default":
            for entry in o.get("metadata") or []:
                if entry.get("key") == "default.audio.sink":
                    value = entry.get("value")
                    default = value.get("name", "") if isinstance(value, dict) else str(value or "")
    out = []
    for o in dump:
        info = o.get("info") or {}
        props = info.get("props") or {}
        if not o.get("type", "").endswith("Node") or props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name") or ""
        where = " ".join(str(props.get(k) or "") for k in ("node.name", "api.alsa.path", "node.description")).lower()
        out.append({"name": name, "label": props.get("node.nick") or props.get("node.description") or name,
                    "state": info.get("state") or "", "error": info.get("error") or "",
                    "default": name == default, "usb": props.get("device.bus") in ("usb", "bluetooth")
                    or name.startswith("bluez"), "hdmi": "hdmi" in where or "iec958" in where})
    return out


def healthy(sink):
    return sink["state"] not in ("error", "")


def pick_output(outputs, fallback="", avoid=(), preferred=""):
    """Where to play: (sink or None, why). The output setting (preferred) whenever it is there
    and works, so an app that makes itself the default (a game stream's virtual speaker) does not
    take the voice away. Else the default output while it works (None: pw-cat
    follows the default); when it is in error, missing or stalled (avoid), the fallback_output
    setting if that works, else the first working output, a USB or Bluetooth headset before
    HDMI. (None, why) with why "none" when nothing works. With no outputs to see (pw-dump missing
    or PipeWire not answering) the default is tried, but only until a player has stalled: then
    there is nothing known to work, and trying the default again would loop forever."""
    if not outputs:
        return (None, "none") if avoid else (None, "default")
    mine = next((s for s in outputs if preferred and s["name"] == preferred), None)
    if mine and healthy(mine) and mine["name"] not in avoid:
        return (None, "default") if mine["default"] else (mine, "the output setting")
    default = next((s for s in outputs if s["default"]), None)
    if default and healthy(default) and default["name"] not in avoid:
        return None, "default"
    usable = [s for s in outputs if healthy(s) and s["name"] not in avoid and not s["default"]]
    chosen = next((s for s in usable if fallback and s["name"] == fallback), None)
    if not chosen and usable:
        chosen = sorted(usable, key=lambda s: 0 if s["usb"] else 2 if s["hdmi"] else 1)[0]
    if not chosen:
        return None, "none"
    if not default:
        return chosen, "no default output"
    if default["name"] in avoid:
        return chosen, f"{default['label']} took no audio"
    return chosen, f"{default['label']} is in error ({default['error'] or 'no reason given'})"


AHEAD = 4     # sentences made ahead of the one playing
GAPS = 3      # more "voice behind" gaps than this in a run: more threads next run
LAG = 0.3     # s: waiting longer than this for the next sentence is a gap you hear, so it is logged
STALL = 4.0   # s: a player that takes no audio this long is stuck (its output is dead)


class Stalled(Exception):
    """The player took no audio for STALL seconds."""


class NoOutput(Exception):
    """No audio output works: the run stops (and stays for Repeat)."""


def reap(proc, wait=2.0):
    """Make sure a player is gone: let it end by itself within wait seconds, else kill it."""
    if proc is None:
        return
    try:
        proc.wait(timeout=wait)
        return
    except Exception:
        pass
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


class Speaker:
    """The audio player, and the sentence it was last given, so a pause can silence it at
    once and Resume can start that sentence again from its beginning."""

    def __init__(self, fallback="", preferred=""):
        self.proc, self.until, self.last = None, time.monotonic(), None
        self.skipping = False   # set by Skip: drop the rest of the reply being read
        self.fallback = fallback   # the fallback_output setting (node.name), "" = pick one
        self.preferred = preferred   # the output setting (node.name), "" = the default output
        self.avoid = set()         # outputs that took no audio this run
        self.told = False          # the fallback is notified once per run
        self.output = None         # the sink played on, None = the default output
        self.start()

    def choose(self):
        """Pick the output, log it when it isn't the default, tell once per run."""
        outputs = sinks()
        self.output, why = pick_output(outputs, self.fallback, self.avoid, self.preferred)
        if why == "none":
            found = ", ".join(f"{s['label']} {s['error'] or s['state']}" for s in outputs)
            log(f"no working audio output ({found or 'PipeWire lists no outputs or does not answer'})")
            notify("No working audio output")
            raise NoOutput()
        if self.output and why == "the output setting":
            log(f"reading on {self.output['label']} ({self.output['name']}): {why}")
        elif self.output:
            log(f"reading on {self.output['label']} ({self.output['name']}): {why}")
            if not self.told:
                self.told = True
                default = next((s["label"] for s in outputs if s["default"]), "The default")
                notify(f"{default} output isn't working. Reading on {self.output['label']} instead.")
        write_json(os.path.join(STATE, "output.json"),
                   {"output": self.output["label"] if self.output else "default", "why": why})

    def start(self):
        if self.proc is not None:
            self.cut()          # never two players at once
        self.choose()
        target = ["--target", self.output["name"]] if self.output else []
        self.proc = subprocess.Popen(["pw-cat", "--playback", "--format", "s16", "--rate", str(RATE),
                                      "--channels", "1", "--raw", *target, "-"],
                                     stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            os.set_blocking(self.proc.stdin.fileno(), False)   # feed() waits with select, never in write
        except (AttributeError, OSError, ValueError):
            pass
        self.until = time.monotonic()   # when the audio handed over so far has been heard

    def cut(self):
        """Silence now (also from the pause and skip signals): kill the player and reap it."""
        try:
            self.proc.kill()
        except Exception:
            pass
        reap(self.proc, wait=1)

    def close(self):
        """Let the player finish what it was given, then make sure it is gone: a player on a
        dead output never finishes, and waiting for it left the worker hanging forever."""
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        reap(self.proc, wait=max(0, self.until - time.monotonic()) + STALL)

    def feed(self, data):
        """Hand data to the player in chunks as its pipe takes them. Raises Stalled when it
        takes nothing for STALL seconds while not paused or skipping, BrokenPipeError when it
        is cut off (pause, skip) or dies."""
        fd = self.proc.stdin.fileno()
        view, last = memoryview(data), time.monotonic()
        while view:
            if self.skipping or os.path.exists(PAUSED):
                raise BrokenPipeError("cut off")
            if not select.select([], [fd], [], 0.1)[1]:
                if time.monotonic() - last > STALL:
                    raise Stalled()
                continue
            try:
                n = os.write(fd, view[:65536])
            except BlockingIOError:
                continue
            view, last = view[n:], time.monotonic()

    def unheard(self):
        """Bytes handed over that the player hasn't taken from its pipe yet."""
        import array, termios
        try:
            n = array.array("i", [0])
            fcntl.ioctl(self.proc.stdin.fileno(), termios.FIONREAD, n)
            return n[0]
        except Exception:
            return 0

    def stalled(self, status, pcm):
        """The player took no audio: drop it and its output, say the sentence again on
        another output (NoOutput when there is none)."""
        name = self.output["name"] if self.output else next((s["name"] for s in sinks() if s["default"]), "default")
        log(f"player stalled on {name}: no audio taken for {STALL:.0f} s")
        self.avoid.add(name)
        self.cut()
        self.proc = None
        self.start()
        self.write(status, pcm)

    def skip(self):
        """From the SIGUSR2 handler: silence now; read() drops the rest of the reply."""
        self.skipping = True
        self.cut()

    def hold(self):
        """While paused, wait; then start again from the sentence that was cut off."""
        if self.skipping or not os.path.exists(PAUSED):
            return
        cut_off = self.last if time.monotonic() < self.until else None
        self.cut()
        try:
            who = open(PAUSED).read().strip()
        except OSError:
            who = ""
        if not who:   # not made by pause(), which says who asked
            log("paused by: unknown (the paused marker appeared without saying who made it)")
        write_json(NOW, {**((cut_off or self.last or [None])[0] or read_json(NOW) or {}),
                         "paused": True, "waiting": len(queued())})
        while os.path.exists(PAUSED):
            time.sleep(0.1)
        try:
            self.proc.wait(timeout=2)
        except Exception:
            pass
        self.start()
        if cut_off and not self.skipping:   # skipped while paused: don't say it again
            self.write(*cut_off)

    def busy(self):
        if self.skipping:
            return False
        self.hold()
        if time.monotonic() < self.until:
            return True
        # all heard by now, unless the player stopped taking audio after its pipe took the
        # last of it (a short last sentence fits in the pipe): say it again elsewhere
        if self.last and self.unheard() and time.monotonic() > self.until + STALL:
            self.stalled(*self.last)
            return True
        return bool(self.last and self.unheard())

    def play(self, status, pcm):
        """Hand a sentence over just before the one playing ends, and only then move the
        highlight, so the screen follows the voice."""
        while True:
            if self.skipping:
                return
            self.hold()
            wait = self.until - time.monotonic() - 0.15
            if wait <= 0:
                break
            time.sleep(min(wait, 0.05))
        self.write(status, pcm)

    def write(self, status, pcm, tries=2):
        write_json(NOW, {**status, "paused": False, "waiting": len(queued())})
        start = max(self.until, time.monotonic())
        self.last, self.until = (status, pcm), start + len(pcm) / RATE
        try:
            self.feed(pcm.tobytes())
        except Stalled:
            self.stalled(status, pcm)
        except (OSError, ValueError):
            if self.skipping:            # cut off by Skip: read() moves on
                return
            if os.path.exists(PAUSED):   # cut off by a pause while handing it over
                self.hold()
            elif tries:                  # the player died: a new one says it again
                self.start()
                self.write(status, pcm, tries - 1)


def skip(source=None):
    """End the reply being read and go straight to the next one waiting. The skipped
    reply stays in the run, so Repeat still has it."""
    pid = worker_pid()
    if not pid:
        notify("Nothing is being read")
        return False
    try:
        os.remove(PAUSED)   # skipping while paused goes on with the next reply
    except OSError:
        pass
    log(f"skipped by: {source or caller()}")
    try:
        os.kill(pid, signal.SIGUSR2)
    except OSError:
        return False
    return True


def pause(source="pause key"):
    """Pause, or resume if paused. Replies that arrive meanwhile keep queuing. Every pause
    and resume is logged with what asked for it."""
    who = f"{source} [pid {os.getpid()}, {caller().removeprefix('command line ')}]"
    if os.path.exists(PAUSED):
        os.remove(PAUSED)
        log(f"resumed by: {who}")
        return "resumed"
    pid = worker_pid()
    if not pid:
        notify("Nothing is being read")
        return ""
    with open(PAUSED, "w") as f:
        f.write(who)   # the worker logs any pause whose marker says nothing (made by something else)
    log(f"paused by: {who}")
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        pass
    return "paused"


NICE = 10        # low priority: other work comes first, yet a fully busy PC can't starve the voice
                 # (nice 19 weighs 15 against 1024 and can't be undone without root)
FAST_ENOUGH = 5.0   # speech made this many times faster than it plays, and
FIRST_IN = 0.6      # s: a typical first sentence (about 15 words) ready this soon: the fewest cores doing both win
TOO_SLOW = 1.5      # below this even the best count can't keep up without gaps
RULE = 2            # how the cores are chosen; a calibration made by another rule is redone
GPUS = ("CUDAExecutionProvider", "ROCMExecutionProvider", "DmlExecutionProvider", "CoreMLExecutionProvider")
CALIBRATE_TEXT = ("This sentence times the voice on this computer without playing it, to pick how many cores.",
                  "The fewest processor cores that start a reply quickly and keep well ahead of speech are used.")


def lower_priority():
    """Everything else on the PC comes first: nice 10, idle disk priority."""
    try:
        os.nice(max(0, NICE - os.nice(0)))
    except OSError:
        pass
    # With autogroup scheduling (on by default on Ubuntu and Omarchy) nice only ranks a process
    # against its own session, and the worker is a session of its own: lower the whole group too.
    try:
        with open("/proc/self/autogroup", "w") as f:
            f.write(str(NICE))
    except OSError:
        pass  # no autogroup (or not Linux): nice alone does it
    try:
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # no ionice: CPU priority alone


def cores():
    """(logical, physical) CPUs this process may run on (a container's or taskset's limit counts);
    hyperthread siblings are one physical core."""
    try:
        cpus = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = list(range(os.cpu_count() or 1))
    siblings = set()
    for c in cpus:
        try:
            with open(f"/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list") as f:
                siblings.add(f.read().strip())
        except OSError:
            siblings.add(str(c))   # unknown: count it as a core of its own
    return len(cpus), len(siblings)


def cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith(("model name", "Hardware", "Model")):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    import platform
    return platform.processor() or platform.machine()


def thread_steps(physical):
    """1, 2, 4 ... up to half the physical cores (at least 1): the voice never takes more."""
    cap = max(1, physical // 2)
    steps, n = [], 1
    while n < cap:
        steps.append(n)
        n *= 2
    return steps + [cap]


def good(result):
    speed, first = result
    return speed >= FAST_ENOUGH and first <= FIRST_IN


def choose(results):
    """{threads: (times faster than speech, first sentence in s)} -> the fewest threads that start soon
    enough and keep ahead, else the one that starts soonest (the fastest on a tie)."""
    fast = [t for t in sorted(results) if good(results[t])]
    return fast[0] if fast else min(results, key=lambda t: (results[t][1], -results[t][0], t))


def providers(conf, available):
    """ONNX_PROVIDER wins (as in kokoro_onnx); else a GPU onnxruntime offers, if use_gpu allows; CPU after."""
    if os.getenv("ONNX_PROVIDER"):
        return [os.getenv("ONNX_PROVIDER")]
    use = str(conf.get("use_gpu", "auto")).lower()
    gpu = [p for p in GPUS if p in available][:1] if use not in ("no", "off", "false", "0") else []
    return gpu + ["CPUExecutionProvider"]


def session(threads, chosen):
    import onnxruntime as rt
    opts = rt.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
    opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return rt.InferenceSession(f"{MODELS}/kokoro-v1.0.onnx", sess_options=opts, providers=chosen)


def machine(conf):
    """What a calibration is good for: it is redone when any of this changes."""
    import onnxruntime as rt
    logical, physical = cores()
    return {"cpu": cpu_model(), "physical": physical, "logical": logical, "onnxruntime": rt.__version__,
            "providers": providers(conf, rt.get_available_providers())}


def speed_of(threads, chosen, conf):
    """(times faster than speech, seconds to make the first sentence) with this many threads
    (silent: the audio is never played)."""
    from kokoro_onnx import Kokoro
    kokoro = Kokoro.from_session(session(threads, chosen), f"{MODELS}/voices-v1.0.bin")
    kokoro.create("Warm up.", voice=conf["voice"], speed=1.0, lang="en-us")
    t0, audio, first = time.monotonic(), 0.0, None
    for text in CALIBRATE_TEXT:
        samples, rate = kokoro.create(text, voice=conf["voice"], speed=1.0, lang="en-us")
        audio += len(samples) / rate
        first = first or time.monotonic() - t0
    return audio / max(time.monotonic() - t0, 1e-6), first


def calibrate(conf, measure=speed_of, here=None):
    """Time the voice at 1, 2, 4 ... threads, keep the fewest fast enough (calibration.json)."""
    here = here or machine(conf)
    results = {}
    for t in thread_steps(here["physical"]):
        speed, first = measure(t, here["providers"], conf)
        results[t] = (round(speed, 1), round(first, 2))
        if good(results[t]):
            break   # more threads would only take cores from everything else
    threads = choose(results)
    speed, first = results[threads]
    cal = {**here, "rule": RULE, "threads": threads, "speed": speed, "first_sentence_s": first,
           "tried": {t: {"speed": v[0], "first_sentence_s": v[1]} for t, v in results.items()},
           "too_slow": speed < TOO_SLOW, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    os.makedirs(STATE, exist_ok=True)
    write_json(CALIBRATION, cal)
    log(f"calibrated: {threads} thread{'' if threads == 1 else 's'}, {speed}x faster than speech, first sentence in {first} s "
        f"({here['cpu']}, {here['physical']} cores, {here['providers'][0]}; tried {results})"
        + ("; too slow to read without gaps" if cal["too_slow"] else ""))
    return cal


def calibration(conf, measure=speed_of):
    """The saved calibration if it was made on this machine as it is now, else a new one."""
    here = machine(conf)
    cal = read_json(CALIBRATION) or {}
    if all(cal.get(k) == v for k, v in here.items()) and cal.get("threads") and cal.get("rule") == RULE:
        return cal
    return calibrate(conf, measure, here)


def step_up(conf):
    """Many gaps in a run: the next run gets one more step of threads (up to the cap)."""
    cal = read_json(CALIBRATION) or {}
    if not cal.get("threads") or not auto_threads(conf):
        return None
    bigger = [t for t in thread_steps(cal.get("physical") or 1) if t > cal["threads"]]
    if not bigger:
        return None
    cal["threads"], cal["raised"] = bigger[0], time.strftime("%Y-%m-%d %H:%M:%S")
    write_json(CALIBRATION, cal)
    log(f"voice fell behind often: {cal['threads']} threads from the next run")
    return cal["threads"]


def auto_threads(conf):
    v = str(conf.get("cpu_threads", "auto")).strip().lower()
    return v in ("", "auto", "0") or not v.lstrip("-").isdigit() or int(v) <= 0


def free_memory_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def voice_engine(conf, measure=speed_of):
    """Kokoro on an onnxruntime session we build: by default onnxruntime uses every core and keeps
    them busy-waiting between steps (about 25 of 32 busy on Souko), which froze the PC while reading.
    The threads are measured per machine (calibration) unless cpu_threads names a number."""
    import onnxruntime as rt
    from kokoro_onnx import Kokoro
    if conf.get("low_priority", True):
        lower_priority()
    free = free_memory_mb()
    if free is not None and free < 2048:
        log(f"low memory: {free} MB available; the voice needs about 350 MB and may be slow or fail")
    if auto_threads(conf):
        cal = calibration(conf, measure)
        threads, chosen, how = cal["threads"], cal["providers"], f"measured {cal['speed']}x"
    else:
        logical, _ = cores()
        threads = max(1, min(int(conf["cpu_threads"]), logical))
        chosen, how = providers(conf, rt.get_available_providers()), "set"
    log(f"voice: {threads} threads ({how}), no spinning, {chosen[0]}, nice {os.nice(0)}")
    return Kokoro.from_session(session(threads, chosen), f"{MODELS}/voices-v1.0.bin")


def worker():
    os.makedirs(QUEUE, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another worker is reading; it will get to our item
    open(PIDFILE, "w").write(str(os.getpid()))
    speaker = None

    def on_term(*_):
        log("stopped")
        try:
            os.remove(NOW)
        except OSError:
            pass
        if speaker is not None:   # never leave the player behind
            speaker.cut()
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)

    import queue, threading
    import numpy as np
    conf = settings()
    voice, speed = conf["voice"], int(conf["speed"]) / 100
    kokoro = voice_engine(conf)
    try:
        os.remove(PAUSED)  # a new run never starts paused
    except OSError:
        pass

    def no_output():
        """Nothing can play: stop the run, keeping it (and what waits) for Repeat."""
        keep_waiting()
        fcntl.flock(lock, fcntl.LOCK_UN)
        for path in (NOW, PIDFILE):
            try:
                os.remove(path)
            except OSError:
                pass
        log("reading stopped: no working audio output (Repeat reads the run again)")
        if speaker is not None:
            speaker.cut()

    open(RUN, "w").close()  # a new run starts (what waits joins it if nothing can play)
    try:
        speaker = Speaker(conf.get("fallback_output") or "", conf.get("output") or "")
    except NoOutput:
        return no_output()
    signal.signal(signal.SIGUSR1, lambda *_: speaker.cut())   # pause: silence at once
    signal.signal(signal.SIGUSR2, lambda *_: speaker.skip())  # skip: drop the rest of this reply

    def read(item, number):
        segs = item["segments"]
        ready, done = queue.Queue(maxsize=AHEAD), threading.Event()

        def make():  # make the next sentence's audio while this one plays
            for seg in segs:
                if done.is_set():
                    return
                samples, _sr = kokoro.create(seg["s"], voice=voice, speed=speed, lang="en-us")
                pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
                while not done.is_set():
                    try:
                        ready.put(pcm, timeout=0.2)
                        break
                    except queue.Full:
                        pass
        threading.Thread(target=make, daemon=True).start()

        try:
            for i in range(len(segs)):
                status = {"source": item.get("source", ""), "asked": item.get("asked", ""),
                          "thread": item.get("thread", ""), "url": item.get("url", ""), "segments": segs,
                          "current": i, "number": number}
                pcm, asked = None, time.monotonic()
                while pcm is None and not speaker.skipping:
                    try:
                        pcm = ready.get(timeout=0.2)
                    except queue.Empty:
                        pass
                if speaker.skipping:
                    break
                waited = time.monotonic() - max(asked, speaker.until)   # silence after the last sentence ended
                if waited > LAG and not os.path.exists(PAUSED):
                    gaps.append(waited)
                    log(f"voice behind: {waited:.1f} s of silence before sentence {i + 1} of {len(segs)}"
                        f" ({len(segs[i]['s'])} characters){' (first of the reply)' if i == 0 else ''}")
                speaker.play(status, pcm)
        finally:
            done.set()
        if speaker.skipping:      # Skip: on to the next reply, on a fresh player
            speaker.skipping = False
            speaker.start()

    if conf["auto_open"]:
        reader = subprocess.Popen(["python3", os.path.abspath(__file__), "open", "--auto"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        threading.Thread(target=reader.wait, daemon=True).start()   # reaped: no <defunct> left
    number, gaps = 0, []
    log(f"reading starts (worker {os.getpid()})")
    while True:
        item = next_item()
        if item is None:
            # Keep the lock until the audio already handed to the player has
            # been heard: a reply arriving now joins this run and waits its
            # turn, instead of a second reader talking over the last sentence.
            try:
                still = speaker.busy()
            except NoOutput:
                return no_output()
            if still:
                time.sleep(0.05)
                continue
            try:
                os.remove(NOW)
            except OSError:
                pass
            # Let go of the lock, then look once more so an item queued
            # in that moment isn't left behind.
            os.remove(PIDFILE)
            fcntl.flock(lock, fcntl.LOCK_UN)
            if not os.listdir(QUEUE):
                break
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                break
            open(PIDFILE, "w").write(str(os.getpid()))
            continue
        number += 1
        with open(RUN, "a", encoding="utf-8") as f:
            f.write(json.dumps(item) + "\n")
        item = announced(item, conf)
        if item.get("source") and item.get("notify", True):
            notify(item["source"], "Reading aloud")
        log(f"reply {number}: {item.get('source') or 'selected text'} ({len(item['segments'])} sentences)")
        try:
            read(item, number)
        except NoOutput:
            return no_output()
    log("reading finished")
    if len(gaps) > GAPS:
        step_up(conf)
    speaker.close()


READER = "org.omarchy.claude-reader"
READER_POS = os.path.join(STATE, "reader-pos.json")


def has_hyprland():
    """The keys and the reader window need Hyprland (Omarchy); elsewhere only the voice runs."""
    return bool(shutil.which("hyprctl") and shutil.which("omarchy-launch-tui")
                and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def hypr(*args):
    try:
        r = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout


def clients():
    try:
        return json.loads(hypr("clients", "-j"))
    except Exception:
        return []


def fullscreen_focused():
    """True while the focused window is fullscreen (a game): the reader would
    take the mouse from it, so reading starts without opening it."""
    try:
        return bool(json.loads(hypr("activewindow", "-j") or "{}").get("fullscreen"))
    except Exception:
        return False


def open_reader(auto=False):
    """Open the reader window (or bring it forward) where it was last left;
    the first time, at the bottom right of the Claude app. Opened by itself
    (auto), it stays closed while a fullscreen window has focus."""
    if not has_hyprland():
        return
    if auto and fullscreen_focused():
        log("reader window not opened: a fullscreen window has focus")
        return
    pos = read_json(READER_POS)
    win = next((c for c in clients() if c["class"] == READER), None)
    if not win:
        # kitty can draw the thread's name larger (Ghostty and foot can't); else Omarchy's terminal
        launch = (["uwsm-app", "--", "kitty", "--class", READER, "-o", "confirm_os_window_close=0",
                   "-o", "remember_window_size=no", SAY_IT, "--view"] if shutil.which("kitty")
                  else ["omarchy-launch-tui", f"--app-id={READER}", SAY_IT, "--view"])
        subprocess.Popen(launch,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(50):
            time.sleep(0.1)
            win = next((c for c in clients() if c["class"] == READER), None)
            if win:
                break
        if not win:
            return
    try:
        monitors = json.loads(hypr("monitors", "-j"))
    except Exception:
        return
    w, h = (pos or {}).get("size") or win["size"]
    spot = fit_on_screen(*pos["at"], w, h, monitors) if pos else None
    if pos and not spot:
        log(f"saved reader spot {pos['at']} is off every screen (monitors moved?): placed by the Claude app")
    if spot:
        x, y, w, h = spot
    else:
        claude = next((c for c in clients() if c["class"] == "com.anthropic.Claude"), None)
        box = (claude["at"], claude["size"]) if claude else None
        if not box:  # no Claude window: the focused monitor
            m = next((m for m in monitors if m["focused"]), monitors[0])
            box = ([m["x"], m["y"]], [m["width"] / m["scale"], m["height"] / m["scale"]])
        (bx, by), (bw, bh) = box
        x, y = int(bx + bw - w - 30), int(by + bh - h - 30)
    # Pinned windows stay on their monitor, so first move it to the
    # workspace showing on the monitor it is going to.
    for m in monitors:
        if m["x"] <= x < m["x"] + m["width"] / m["scale"] and m["y"] <= y < m["y"] + m["height"] / m["scale"]:
            if m["id"] != win["monitor"]:
                hypr("dispatch", f'hl.dsp.window.move({{ window = "address:{win["address"]}", '
                                 f'workspace = "{m["activeWorkspace"]["name"]}", follow = false }})')
            break
    hypr("dispatch", f'hl.dsp.window.resize({{ window = "address:{win["address"]}", x = {int(w)}, y = {int(h)} }})')
    hypr("dispatch", f'hl.dsp.window.move({{ window = "address:{win["address"]}", x = {int(x)}, y = {int(y)} }})')
    set_opacity(opacity(), save=False)


def screens(monitors):
    """Each monitor as (x, y, width, height) in Hyprland's layout coordinates."""
    return [(m["x"], m["y"], m["width"] / m["scale"], m["height"] / m["scale"]) for m in monitors]


def fit_on_screen(x, y, w, h, monitors):
    """The saved spot, kept inside the monitor holding the window's centre, or None when
    that centre is on no monitor (after a reboot the monitors can come back arranged
    differently, and the reader opened below the bottom of every screen)."""
    cx, cy = x + w / 2, y + h / 2
    for mx, my, mw, mh in screens(monitors):
        if mx <= cx < mx + mw and my <= cy < my + mh:
            w, h = min(w, mw - 20), min(h, mh - 20)
            x = max(mx + 10, min(x, mx + mw - w - 10))
            y = max(my + 10, min(y, my + mh - h - 10))
            return int(x), int(y), int(w), int(h)
    return None


def toggle_reader():
    """Super+Alt+V: close the reader if it is open, else open it."""
    win = next((c for c in clients() if c["class"] == READER), None)
    if win:
        hypr("dispatch", f'hl.dsp.window.close({{ window = "address:{win["address"]}" }})')
    else:
        open_reader()


def remember_reader_position():
    """Called from the reader itself: keep where it was dragged."""
    win = next((c for c in clients() if c["class"] == READER), None)
    try:
        monitors = json.loads(hypr("monitors", "-j"))
    except Exception:
        monitors = []
    if win and win["floating"] and fit_on_screen(*win["at"], *win["size"], monitors):   # never keep an off-screen spot
        pos = {"at": win["at"], "size": win["size"]}
        if pos != read_json(READER_POS):
            write_json(READER_POS, pos)


def opacity():
    """The slider's value if it has been moved, else the add-on's setting."""
    try:
        value = int(open(OPACITY).read())
    except Exception:
        value = int(settings()["opacity"])
    return max(20, min(100, value))


def set_opacity(value, save=True):
    value = max(20, min(100, int(value)))
    if save:
        os.makedirs(STATE, exist_ok=True)
        open(OPACITY, "w").write(str(value))
    win = next((c for c in clients() if c["class"] == READER), None)
    if win:
        for prop in ("opacity", "opacity_inactive"):
            hypr("dispatch", f'hl.dsp.window.set_prop({{ window = "address:{win["address"]}", '
                             f'prop = "{prop}", value = "{value / 100:.2f}" }})')
    return value


def run_items():
    items = []
    if os.path.exists(RUN):
        for line in open(RUN, encoding="utf-8"):
            try:
                items.append(json.loads(line))
            except Exception:
                pass
    return items


REV, DIM, BOLD, OFF = "\033[7m", "\033[2m", "\033[1m", "\033[0m"
SLIDER_LABEL = "Opacity "


def slider(cols, value):
    """The opacity slider line, and the columns (1-based) its bar spans."""
    width = max(10, cols - len(SLIDER_LABEL) - 6)
    first = len(SLIDER_LABEL) + 1
    knob = round((value - 20) / 80 * (width - 1))
    bar = "━" * knob + "●" + "─" * (width - 1 - knob)
    return f"{SLIDER_LABEL}{bar} {value:3d}%", first, first + width - 1


def slider_value(x, first, last):
    """Where a click at column x on the bar puts the opacity (20-100)."""
    x = max(first, min(last, x))
    return round(20 + (x - first) / max(1, last - first) * 80)


def announced(item, conf):
    """Starts the reply with "From <thread>." when it comes from a different thread than
    the one read last (a setting, on by default), so the ear knows where it is too."""
    key = item.get("thread") or ""
    try:
        last = open(LAST_THREAD).read()
    except OSError:
        last = ""
    if key:
        os.makedirs(STATE, exist_ok=True)
        open(LAST_THREAD, "w").write(key)
    if not key or key == last or not conf.get("announce_thread", True):
        return item
    name = " ".join((thread_names().get(key) or item.get("source", "")).rstrip("…").split()[:10])
    return {**item, "segments": [{"s": f"From: {name}.", "nl": True, "intro": True}, *item["segments"]]}


def band(key):
    """The thread's colour, the same every time for the same thread."""
    import zlib
    return BANDS[zlib.crc32(key.encode()) % len(BANDS)] if key else 238


def caller():
    """Who ran this command, for the log: "command line (from bash < claude)"."""
    names, pid = [], os.getppid()
    for _ in range(2):
        try:
            names.append(open(f"/proc/{pid}/comm").read().strip())
            pid = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return f"command line (from {' < '.join(names) or '?'})"


def open_thread(source=None):
    """Open the thread being read (else the last one read) on claude.ai, in ONE window: the
    same thread already open is only brought forward; another thread's window is closed
    first. Opening pauses reading (auto_pause), so the reply waits while it is looked at."""
    source = source or caller()
    now = read_json(NOW) or {}
    url = now.get("url") or next((i.get("url") for i in reversed(run_items()) if i.get("url")), "")
    if not url:
        notify("This reply has no thread to open")
        return False
    if settings().get("auto_pause", True) and worker_pid() and not os.path.exists(PAUSED):
        pause(f"opening the thread ({source})")
    win = read_json(THREAD_WINDOW) or {}
    live = {c["address"]: c for c in clients()}
    if win.get("address") in live:
        if win.get("url") == url:
            hypr("dispatch", f'hl.dsp.focus({{ window = "address:{win["address"]}" }})')
            log(f"thread window brought forward ({source})")
            return True
        hypr("dispatch", f'hl.dsp.window.close({{ window = "address:{win["address"]}" }})')
    log(f"thread opened ({source})")
    if not has_hyprland() or not shutil.which("omarchy-launch-webapp"):
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    # its own app window, remembered by Hyprland address (found in the background, so the
    # reader doesn't wait for the browser)
    subprocess.Popen(["python3", os.path.abspath(__file__), "thread-window", url, json.dumps(sorted(live))],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def thread_window(url, before):
    """Open url as an app window and remember which window it is."""
    subprocess.Popen(["omarchy-launch-webapp", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(100):
        time.sleep(0.1)
        new = [c for c in clients() if c["address"] not in before]
        if new:
            write_json(THREAD_WINDOW, {"address": new[0]["address"], "url": url})
            return True
    return False


def footer(conf, cols):
    """The keys, on one line: "Super+Alt: P pause · J skip · O thread …" when they share
    their modifiers; most used first, as a narrow reader cuts the end."""
    keys = [(key_label(conf[k]), what) for k, what in
            (("key_pause", "pause"), ("key_skip", "skip"), ("key_open", "thread"), ("key_repeat", "repeat"),
             ("key_stop", "stop"), ("key_rename", "rename"), ("key_mute", "mute"))
            if conf.get(k) and conf[k].lower() != "none"]
    mods = {k.rsplit("+", 1)[0] for k, _ in keys if "+" in k}
    if len(mods) == 1:
        line = f"{mods.pop()}: " + " · ".join(f"{k.rsplit('+', 1)[-1]} {what}" for k, what in keys)
    else:
        line = " · ".join(f"{k} {what}" for k, what in keys)
    return shorten(line, cols - 1)


def title_band(now, cols, conf):
    """The thread's name in a band of its own colour. In kitty the name is drawn
    title_size times larger (kitty's text sizing, OSC 66), up to two lines; other
    terminals can't draw bigger text, so there it is one bold line."""
    colour = f"\033[48;5;{band(now.get('thread', ''))}m\033[97m"
    big = max(1, min(4, int(conf.get("title_size") or 1))) if os.environ.get("KITTY_WINDOW_ID") else 1
    name = thread_names().get(now.get("thread", "")) or now.get("source") or "Selected text"
    if now.get("paused"):
        hint = f"▌▌ PAUSED · {key_label(conf['key_pause'])} or click resumes"
    elif now.get("url"):
        hint = (f"{key_label(conf['key_open'])} or click: open thread · {key_label(conf['key_pause'])} pause"
                f" · {key_label(conf['key_rename'])} rename")
    else:
        hint = f"{key_label(conf['key_pause'])} pause"
    out = [f"{colour}{' ' * cols}{OFF}"]
    per = max(4, (cols - 1) // big) if big > 1 else max(4, cols - 2)
    left = max(0, cols - 1 - big * per)   # columns the large text leaves free on the right
    import textwrap
    lines = textwrap.wrap(name, per) or [""]
    if len(lines) > 2:                    # two lines at most, the rest cut with …
        lines = [lines[0], shorten(" ".join(lines[1:]), per - 1)]
    for line in lines:
        if big > 1:
            # the large text covers `big` rows; colour the cells around it on each of them
            out.append(f"{colour}{BOLD} \033]66;s={big};{line.ljust(per)}\a{' ' * left}{OFF}")
            out += [f"{colour} \033[{2 + big * per}G{' ' * left}{OFF}"] * (big - 1)
        else:
            out.append(f"{colour}{BOLD}  {line.ljust(cols - 2)}{OFF}")
    pause_style = "\033[1;30;43m" if now.get("paused") else colour
    out.append(f"{pause_style}  {shorten(hint, cols - 3).ljust(cols - 2)}{OFF}")
    return out


def render(now, items, cols, rows, conf, opac):
    out = []
    if not now:
        out.append(f"{BOLD}Not reading right now.{OFF}")
        if items:
            out.append("")
            out.append(f"Last run ({key_label(conf['key_repeat'])} reads it again):")
            out += [f"  {n}. {it.get('source') or 'Selected text'}" for n, it in enumerate(items, 1)]
    else:
        waiting = f"  ·  {now['waiting']} waiting" if now.get("waiting") else ""
        out += title_band(now, cols, conf)
        if now.get("asked"):
            out.append(f"{DIM}You asked: {now['asked']}{OFF}")
        out.append(f"{DIM}reply {now.get('number', 1)} of this run{waiting}{OFF}")
        out.append("")
        # Wrap the reply word by word, highlighting the sentence being read
        lines, line, width, cur_line, last = [], "", 0, 0, None
        for i, seg in enumerate(now["segments"]):
            if seg["nl"] and line:
                lines.append(line); line, width = "", 0
            style = REV if i == now["current"] else (DIM if i < now["current"] else "")
            for word in seg["s"].split():
                if width and width + 1 + len(word) > cols - 2:
                    lines.append(line); line, width = "", 0
                if i == now["current"] and not cur_line:
                    cur_line = len(lines) + 1
                sep = " " if width else ""
                # the space between two words of the same sentence takes its style
                same = sep and last == i
                text = (sep + word) if same else word
                line += ("" if same else sep) + (f"{style}{text}{OFF}" if style else text)
                width += len(sep) + len(word)
                last = i
        lines.append(line)
        room = max(1, rows - len(out) - 3)
        top = max(0, min(cur_line - 1 - room // 3, len(lines) - room))
        out += lines[top:top + room]
    bar = slider(cols, opac)[0]
    keys = footer(conf, cols)
    body = "\n".join(out[:rows - 2])
    return f"\033[H\033[2J{body}\033[{rows - 1};1H{bar}\033[{rows};1H{DIM}{keys}{OFF}"


def reader_input(typed, rows, cols, band_rows, opac):
    """What the reader does with what reached it: (opacity, actions).

    On Omarchy keyboard focus follows the mouse, so whatever is typed while the pointer
    rests on the reader lands here. Plain keys therefore do nothing (a space used to pause,
    "o" to open the thread); only mouse clicks, the wheel and Ctrl+N (name the thread) act."""
    want, actions = opac, []
    for m in re.finditer(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])", typed):
        button, x, y, press = int(m[1]), int(m[2]), int(m[3]), m[4] == "M"
        if button in (0, 32) and press and y == rows - 1:     # click or drag on the slider
            _, first, last = slider(cols, want)
            want = slider_value(x, first, last)
        elif button == 0 and press and y <= band_rows:         # click on the thread band
            actions.append("band")
        elif button == 64:
            want += 5
        elif button == 65:
            want -= 5
    if "\x0e" in re.sub(r"\x1b\[<[\d;]*[Mm]", "", typed):    # Ctrl+N
        actions.append("name")
    return max(20, min(100, want)), actions


def current_thread():
    """(key, name) of the thread being read, else the last one read."""
    now = read_json(NOW) or {}
    item = now if now.get("thread") else next((i for i in reversed(run_items()) if i.get("thread")), {})
    key = item.get("thread", "")
    return key, (thread_names().get(key) or item.get("source", "")) if key else ""


def edit_line(read_key, write, prompt, initial):
    """A one-line editor at the bottom of the reader: starts with the current name;
    Enter saves, Esc cancels, Backspace deletes, Ctrl+U clears. Returns the text or None."""
    text = initial
    while True:
        write(prompt, text)
        ch = read_key()
        if ch in ("\r", "\n"):
            return text.strip()
        if ch == "\x1b" or ch is None:          # Esc (arrow keys and mouse never get here)
            return None
        if ch in ("\x7f", "\b"):
            text = text[:-1]
        elif ch == "\x15":                       # Ctrl+U
            text = ""
        elif ch.isprintable():
            text += ch


def prompt_line(prompt, text, cols):
    """The prompt and the text on ONE line of the reader: never wider than the window
    (a wrapped line left a copy behind on every key). A long name scrolls to its end."""
    if len(prompt) > cols // 2:
        prompt = "Name: "
    room = max(1, cols - len(prompt) - 1)
    shown = text if len(text) <= room else "…" + text[-(room - 1):]
    return f"{BOLD}{prompt}{OFF}{shown}"


def rename_prompt(fd, rows, cols):
    """Ask for the thread's name (as on the Threads tab) and keep it."""
    key, name = current_thread()
    if not key:
        notify("No thread to name yet")
        return
    sys.stdout.write(f"\033[?1000l\033[?1002l\033[?1006l\033[{rows};1H\033[?25h")

    def read_key():
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == "\x1b" and select.select([fd], [], [], 0.05)[0]:   # an escape sequence, not Esc
            os.read(fd, 32)
            return ""
        return ch

    def write(prompt, text):
        sys.stdout.write(f"\033[{rows};1H\033[2K" + prompt_line(prompt, text, cols))
        sys.stdout.flush()
    try:
        new = edit_line(read_key, write, "Thread name (Enter saves, Esc cancels): ", name)
    finally:
        sys.stdout.write("\033[?25l\033[?1000h\033[?1002h\033[?1006h")
    if new:
        name_thread(new, key)


def request_rename():
    """Super+Alt+N: bring the reader forward with its rename prompt open."""
    os.makedirs(STATE, exist_ok=True)
    open(RENAME, "w").close()
    open_reader()
    win = next((c for c in clients() if c["class"] == READER), None)
    if win:
        hypr("dispatch", f'hl.dsp.focus({{ window = "address:{win["address"]}" }})')


def view():
    """Reader window: redraws when the sentence being read changes. Click or drag
    on the slider (or scroll) to change the window's opacity."""
    import termios, tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd) if os.isatty(fd) else None
    if saved:
        tty.setcbreak(fd)
    # hide the cursor, report mouse clicks, drags and wheel (SGR encoding)
    sys.stdout.write("\033[?25l\033[?1000h\033[?1002h\033[?1006h")
    conf, opac = settings(), opacity()
    shown, checked = None, time.monotonic() + 2  # let open_reader place it first
    was_reading, idle_since, typed = False, None, ""
    try:
        while True:
            if time.monotonic() - checked > 2:
                checked = time.monotonic()
                remember_reader_position()
            now = read_json(NOW)
            # Close once reading has finished (not between replies, and not
            # while a repeat is starting up)
            if now or worker_pid():
                was_reading, idle_since = was_reading or bool(now), None
            elif was_reading:
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since > 1:
                    remember_reader_position()
                    return
            cols, rows = shutil.get_terminal_size()
            key = (json.dumps(now), cols, rows, opac)
            if key != shown:
                shown = key
                sys.stdout.write(render(now, run_items() if not now else [], cols, rows, conf, opac))
                sys.stdout.flush()
            if saved and os.path.exists(RENAME):     # Super+Alt+N
                os.remove(RENAME)
                rename_prompt(fd, rows, cols)
                shown = None
                continue
            if not saved or not select.select([fd], [], [], 0.1)[0]:
                if not saved:
                    time.sleep(0.1)
                continue
            typed += os.read(fd, 1024).decode(errors="ignore")
            band_rows = len(title_band(now, cols, conf)) if now else 0
            want, actions = reader_input(typed, rows, cols, band_rows, opac)
            for action in actions:
                if action == "band" and now and now.get("paused"):
                    pause("click on the reader band")     # click on the band: resume when paused,
                elif action == "band" and now:
                    open_thread("click on the reader band")  # else open the thread (which pauses)
                elif action == "name":
                    rename_prompt(fd, rows, cols)
                    shown = None
            # keep an escape sequence that hasn't fully arrived yet
            tail = typed.rfind("\x1b")
            typed = typed[tail:] if tail >= 0 and not re.match(r"\x1b\[<[\d;]*[Mm]", typed[tail:]) else ""
            if len(typed) > 32:
                typed = ""
            if want != opac:
                opac = set_opacity(want)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?1006l\033[?1002l\033[?1000l\033[?25h\033[0m\n")
        sys.stdout.flush()
        if saved:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def voice_status():
    cal = read_json(CALIBRATION) or {}
    if not cal:
        return "not measured yet (it is, silently, before the first reading)"
    out = (f"{cal['threads']} core{'' if cal['threads'] == 1 else 's'}, {cal['speed']}x faster than speech"
           + (f", first sentence in {cal['first_sentence_s']} s" if "first_sentence_s" in cal else "")
           + f" on {cal.get('providers', ['CPU'])[0]}")
    return ("This computer is too slow for the local voice to read without gaps (" + out + ")"
            if cal.get("too_slow") else out)


def status(outputs=None):
    """muted / reading / installed, the output in use (or that would be used) and any
    output in error, for the dashboard's Status button."""
    outputs = sinks() if outputs is None else outputs
    reading = bool(read_json(NOW))
    playing = (read_json(os.path.join(STATE, "output.json")) or {}) if reading and worker_pid() else {}
    if not playing:
        sink, why = pick_output(outputs, settings().get("fallback_output") or "", (), settings().get("output") or "")
        default = next((s["label"] for s in outputs if s["default"]), "default")
        playing = {"output": sink["label"] if sink else (default if why != "none" else "none"), "why": why}
    return {"muted": os.path.exists(MUTE), "reading": reading, "waiting": len(queued()), "opacity": opacity(),
            "output": playing.get("output"), "output_why": playing.get("why"),
            "outputs_in_error": [f"{s['label']}: {s['error'] or 'error'}" for s in outputs if s["state"] == "error"],
            "voice": voice_status(),
            "venv": os.path.exists(PY), "models": all(os.path.exists(f"{MODELS}/{f}")
                                                     for f in ("kokoro-v1.0.onnx", "voices-v1.0.bin"))}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "say"
    if cmd == "hook":
        if os.path.exists(MUTE):
            return
        try:
            data = json.load(sys.stdin)
        except Exception:
            return
        segs = segments(reply_text(data))
        if segs:
            t = thread(data)
            enqueue({"source": t["name"], "thread": t["key"], "url": t["url"],
                     "asked": asked(data), "segments": segs})
    elif cmd == "forward":
        try:
            forward(sys.argv[2], json.load(sys.stdin))
        except Exception:
            pass   # a Stop hook never fails Claude
    elif cmd == "queue":
        try:
            queue_item(json.load(sys.stdin))
        except ValueError:
            sys.exit("queue: JSON on stdin")
    elif cmd == "worker":
        worker()
    elif cmd == "view":
        view()
    elif cmd == "open":
        open_reader(auto="--auto" in sys.argv[2:])
    elif cmd == "open-thread":
        open_thread(" ".join(sys.argv[2:]) or None)
    elif cmd == "thread-window":
        thread_window(sys.argv[2], set(json.loads(sys.argv[3])))
    elif cmd == "skip":
        skip(" ".join(sys.argv[2:]) or None)
    elif cmd == "pause":
        print(pause(" ".join(sys.argv[2:]) or caller()))
    elif cmd == "rename":
        request_rename()
    elif cmd == "name-thread":
        ok = name_thread(" ".join(sys.argv[2:]))
        print("named" if ok else "no thread being read, or no name given")
        sys.exit(0 if ok else 1)
    elif cmd == "toggle":
        toggle_reader()
    elif cmd == "opacity":
        print(f"{set_opacity(sys.argv[2])}%")
    elif cmd == "test":
        enqueue({"source": "Read aloud test", "notify": False,
                 "segments": segments("This is the read aloud voice. If you can hear this, it works.")})
    elif cmd == "status":
        print(json.dumps(status()))
    elif cmd == "calibrate":
        conf = settings()
        if conf.get("low_priority", True):
            lower_priority()   # measured as the voice will run
        cal = calibrate(conf)
        print(f"voice: {voice_status()} ({cal['cpu']})")   # says plainly when the computer is too slow
    elif cmd == "stop":
        stop()
    elif cmd == "repeat":
        stop("repeat")
        items = run_items()
        for item in items:
            enqueue(item)
        if not items:
            notify("Nothing to repeat yet")
    elif cmd == "mute":
        if os.path.exists(MUTE):
            os.remove(MUTE)
            notify("Claude replies: reading aloud ON")
        else:
            os.makedirs(STATE, exist_ok=True)
            open(MUTE, "w").close()
            stop("mute")
            notify("Claude replies: muted")
    elif cmd == "say":
        if stop("read-selection key"):
            return  # it was reading: the key stops it
        text = " ".join(sys.argv[2:])
        # Piped text, but don't wait on a stdin nobody writes to (keybinds)
        if not text and not sys.stdin.isatty() and select.select([sys.stdin], [], [], 0.2)[0]:
            text = sys.stdin.read()
        if not text:
            for args in (["wl-paste", "-p", "-n"], ["wl-paste", "-n"]):
                try:
                    text = subprocess.run(args, capture_output=True, text=True,
                                          timeout=2, check=False).stdout
                except (OSError, subprocess.TimeoutExpired):
                    text = ""
                if text.strip():
                    break
        segs = segments(text)
        if segs:
            enqueue({"source": "", "segments": segs, "notify": False})
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
