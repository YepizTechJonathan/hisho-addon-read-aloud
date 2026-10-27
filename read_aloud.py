#!/usr/bin/env python3
"""Read Claude's replies aloud with Kokoro, one after another (Hisho add-on read-aloud).

  read_aloud.py hook        Stop hook: queue Claude's last reply (JSON on stdin)
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
  read_aloud.py status      muted / reading / installed, as JSON
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
RATE = 24000
DEFAULTS = {"voice": "af_heart", "speed": 100, "opacity": 90, "auto_open": True,
            "key_read": "SUPER + ALT + T", "key_repeat": "SUPER + ALT + R", "key_stop": "SUPER + ALT + X",
            "key_mute": "SUPER + ALT + M", "key_reader": "SUPER + ALT + V", "key_open": "SUPER + ALT + O",
            "key_pause": "SUPER + ALT + P", "key_rename": "SUPER + ALT + N", "key_skip": "SUPER + ALT + J", "announce_thread": True, "auto_pause": True, "title_size": 2}
LAST_THREAD = os.path.join(STATE, "last-thread")
PAUSED = os.path.join(STATE, "paused")
THREAD_WINDOW = os.path.join(STATE, "thread-window.json")
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
    dropped = [read_json(p) for p in queued()]
    if dropped and os.path.exists(RUN):
        with open(RUN, "a", encoding="utf-8") as f:
            for item in dropped:
                if item:
                    f.write(json.dumps(item) + "\n")
    for path in queued():
        os.remove(path)
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


class Speaker:
    """The audio player, and the sentence it was last given, so a pause can silence it at
    once and Resume can start that sentence again from its beginning."""

    def __init__(self):
        self.proc, self.until, self.last = None, time.monotonic(), None
        self.skipping = False   # set by Skip: drop the rest of the reply being read
        self.start()

    def start(self):
        if self.proc is not None:
            self.cut()          # never two players at once
        self.proc = subprocess.Popen(["pw-cat", "--playback", "--format", "s16", "--rate", str(RATE),
                                      "--channels", "1", "--raw", "-"],
                                     stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.until = time.monotonic()   # when the audio handed over so far has been heard

    def cut(self):
        try:
            self.proc.kill()
        except Exception:
            pass

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait()
        except Exception:
            pass

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
        return time.monotonic() < self.until

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
            self.proc.stdin.write(pcm.tobytes())
            self.proc.stdin.flush()
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
    if os.path.exists(PAUSED):
        os.remove(PAUSED)
        log(f"resumed by: {source}")
        return "resumed"
    pid = worker_pid()
    if not pid:
        notify("Nothing is being read")
        return ""
    open(PAUSED, "w").close()
    log(f"paused by: {source}")
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        pass
    return "paused"


def worker():
    os.makedirs(QUEUE, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another worker is reading; it will get to our item
    open(PIDFILE, "w").write(str(os.getpid()))

    def on_term(*_):
        log("stopped")
        try:
            os.remove(NOW)
        except OSError:
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)

    import queue, threading
    import numpy as np
    from kokoro_onnx import Kokoro

    conf = settings()
    voice, speed = conf["voice"], int(conf["speed"]) / 100
    kokoro = Kokoro(f"{MODELS}/kokoro-v1.0.onnx", f"{MODELS}/voices-v1.0.bin")
    try:
        os.remove(PAUSED)  # a new run never starts paused
    except OSError:
        pass
    speaker = Speaker()
    signal.signal(signal.SIGUSR1, lambda *_: speaker.cut())   # pause: silence at once
    signal.signal(signal.SIGUSR2, lambda *_: speaker.skip())  # skip: drop the rest of this reply

    def read(item, number):
        segs = item["segments"]
        ready, done = queue.Queue(maxsize=2), threading.Event()

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

        for i in range(len(segs)):
            status = {"source": item.get("source", ""), "asked": item.get("asked", ""),
                      "thread": item.get("thread", ""), "url": item.get("url", ""), "segments": segs,
                      "current": i, "number": number}
            pcm = None
            while pcm is None and not speaker.skipping:
                try:
                    pcm = ready.get(timeout=0.2)
                except queue.Empty:
                    pass
            if speaker.skipping:
                break
            speaker.play(status, pcm)
        done.set()
        if speaker.skipping:      # Skip: on to the next reply, on a fresh player
            speaker.skipping = False
            speaker.start()

    open(RUN, "w").close()  # a new run starts
    if conf["auto_open"]:
        subprocess.Popen(["python3", os.path.abspath(__file__), "open"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    number = 0
    log(f"reading starts (worker {os.getpid()})")
    while True:
        item = next_item()
        if item is None:
            # Keep the lock until the audio already handed to the player has
            # been heard: a reply arriving now joins this run and waits its
            # turn, instead of a second reader talking over the last sentence.
            if speaker.busy():
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
        read(item, number)
    log("reading finished")
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


def open_reader():
    """Open the reader window (or bring it forward) where it was last left;
    the first time, at the bottom right of the Claude app."""
    if not has_hyprland():
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


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "say"
    if cmd == "hook":
        if os.path.exists(MUTE):
            return
        try:
            data = json.load(sys.stdin)
        except Exception:
            return
        segs = segments(last_reply(data))
        if segs:
            t = thread(data)
            enqueue({"source": t["name"], "thread": t["key"], "url": t["url"],
                     "asked": asked(data), "segments": segs})
    elif cmd == "worker":
        worker()
    elif cmd == "view":
        view()
    elif cmd == "open":
        open_reader()
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
        print(json.dumps({"muted": os.path.exists(MUTE), "reading": bool(read_json(NOW)),
                          "waiting": len(queued()), "opacity": opacity(),
                          "venv": os.path.exists(PY), "models": all(os.path.exists(f"{MODELS}/{f}")
                          for f in ("kokoro-v1.0.onnx", "voices-v1.0.bin"))}))
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
