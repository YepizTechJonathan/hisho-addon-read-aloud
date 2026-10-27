"""Read aloud add-on: replies become sentences, the reader highlights and has an opacity slider, keys."""
import json
import os
from importlib import util
from pathlib import Path

from hisho import addons

HERE = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = util.spec_from_file_location(name, path)
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ra = _load("read_aloud", HERE / "read_aloud.py")

CONF = dict(ra.DEFAULTS)


def test_markdown_becomes_sentences_with_a_pause_per_line():
    segs = ra.segments("Done. It works **well**, see [docs](https://x.y).\n\n"
                       "**What changed:**\n1. Full replies\n- Repeat works!\n\n"
                       "| Key | Does |\n|---|---|\n```bash\necho hi\n```")
    assert [s["s"] for s in segs] == ["Done.", "It works well, see docs.", "What changed:",
                                      "Full replies.", "Repeat works!", "Key Does.", "(code block)."]
    assert [s["nl"] for s in segs] == [True, False, True, True, True, True, True]


def test_the_manifest_is_valid_and_goes_to_the_workstation(addon):
    a = addon
    assert a.manifest["host"] == "workstation" and not addons.validate_manifest(a.manifest)
    assert {"repeat", "stop", "mute", "test", "opacity"} <= set(a.actions())


def test_the_question_comes_out_of_a_remote_control_wrapper(tmp_path):
    wrapped = ('<wake reason="mention"><project><message from="human" id="m1">first ask</message></project></wake>')
    lines = [{"type": "user", "message": {"content": [{"type": "text", "text": wrapped}]}},
             {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
             {"type": "user", "message": {"content": "<wake><message id='m2'>lets add it to Hisho</message></wake>"}}]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines))
    assert ra.asked({"transcript_path": str(path)}) == "lets add it to Hisho"


def test_the_slider_maps_clicks_to_20_to_100_percent():
    line, first, last = ra.slider(60, 60)
    assert line.startswith("Opacity ") and line.endswith(" 60%")
    assert ra.slider_value(first, first, last) == 20
    assert ra.slider_value(last, first, last) == 100
    assert ra.slider_value(last + 10, first, last) == 100
    assert ra.slider_value((first + last) // 2, first, last) in range(58, 63)
    # the knob sits where a click would put that value
    for value in (20, 55, 100):
        line, first, last = ra.slider(80, value)
        assert ra.slider_value(first + line[len("Opacity "):].index("●"), first, last) in range(value - 2, value + 3)


def test_the_reader_highlights_the_sentence_being_read_and_shows_the_slider():
    now = {"source": "Remote Control session", "asked": "add a slider", "number": 2, "waiting": 1, "current": 1,
           "segments": [{"s": "One.", "nl": True}, {"s": "Two is read.", "nl": False}, {"s": "Three.", "nl": True}]}
    screen = ra.render(now, [], 60, 12, CONF, 70)
    assert "Remote Control session" in screen and "You asked: add a slider" in screen
    assert "reply 2 of this run  ·  1 waiting" in screen
    assert f"{ra.REV}Two{ra.OFF}" in screen and f"{ra.DIM}One.{ra.OFF}" in screen
    assert "\033[11;1HOpacity " in screen and " 70%" in screen
    assert "Super+Alt: P pause · J skip · O thread · R repeat" in screen


def test_when_idle_the_reader_lists_the_last_run():
    screen = ra.render(None, [{"source": "hisho"}, {"source": ""}], 60, 12, CONF, 90)
    assert "Not reading right now." in screen
    assert "1. hisho" in screen and "2. Selected text" in screen


def test_key_combos_compare_whatever_the_spacing_and_case():
    os.environ.setdefault("ADDON", str(HERE))
    hypr_keys = _load("hypr_keys", HERE / "bin" / "hypr_keys.py")
    assert hypr_keys.combo("SUPER + ALT + R") == hypr_keys.combo("super+alt+r")
    assert hypr_keys.combo("SUPER + ALT + R") == (frozenset({"SUPER", "ALT"}), "R")
    assert ra.key_label("SUPER + ALT + V") == "Super+Alt+V"


class _Audio:
    """Just enough of a numpy array for the worker: clip, scale, astype, tobytes, len."""
    def __init__(self, n):
        self.n = n

    def __mul__(self, _):
        return self

    def astype(self, _):
        return self

    def tobytes(self):
        return b"\0\0" * self.n

    def __len__(self):
        return self.n


def test_a_reply_arriving_while_the_last_sentence_still_plays_waits_in_the_same_run(tmp_path, monkeypatch):
    """The reader used to let go as soon as its last sentence was handed to the player; a reply
    landing in those seconds started a second reader that talked over it and reset the run."""
    import sys
    import threading
    import time
    import types

    state = tmp_path / "state"
    for name, value in {"STATE": state, "QUEUE": state / "queue", "RUN": state / "last-run.jsonl",
                        "NOW": state / "now.json", "LOCK": state / "worker.lock",
                        "PIDFILE": state / "worker.pid", "CONFIG": tmp_path / "none.json"}.items():
        monkeypatch.setattr(ra, name, str(value))
    monkeypatch.setattr(ra, "settings", lambda: {**ra.DEFAULTS, "auto_open": False})
    monkeypatch.setattr(ra, "notify", lambda *a, **k: None)
    monkeypatch.setattr(ra.signal, "signal", lambda *a: None)
    np = types.SimpleNamespace(clip=lambda a, lo, hi: a, int16="int16")
    kokoro = types.SimpleNamespace(Kokoro=lambda *a: types.SimpleNamespace(
        create=lambda text, **k: (_Audio(int(ra.RATE * 0.6)), ra.RATE)))   # every sentence: 0.6 s
    monkeypatch.setitem(sys.modules, "numpy", np)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", kokoro)
    heard = []

    class Player:  # takes the audio at once, like a pipe with room to spare
        def __init__(self, *a, **k):
            self.stdin = types.SimpleNamespace(write=lambda b: heard.append(time.monotonic()),
                                               flush=lambda: None, close=lambda: None)

        def wait(self):
            return 0
    monkeypatch.setattr(ra.subprocess, "Popen", Player)   # also swallows enqueue's worker start

    ra.enqueue({"source": "A", "segments": ra.segments("First thread.")})
    worker = threading.Thread(target=ra.worker)
    worker.start()
    while not heard:
        time.sleep(0.01)
    time.sleep(0.2)                       # A is handed over and still playing
    ra.enqueue({"source": "B", "segments": ra.segments("Second thread.")})
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert [i["source"] for i in ra.run_items()] == ["A", "B"]   # one run: Repeat replays both
    assert ra.queued() == [] and len(heard) == 2
    assert heard[1] - heard[0] >= 0.6 - 0.2                       # B after A, not over it


def _session(tmp_path, monkeypatch, title, remote):
    """A Claude desktop session record and its transcript, as the Stop hook finds them."""
    sessions = tmp_path / "desktop" / "acct" / "org"
    sessions.mkdir(parents=True)
    (sessions / "local_1.json").write_text(json.dumps(
        {"cliSessionId": "s1", "title": title, "cwd": "/home/u/code/hisho",
         "remoteControlSpawn": {"ccrSessionId": remote} if remote else {}, "bridgeSessionIds": [remote]}))
    monkeypatch.setattr(ra, "DESKTOP_SESSIONS", str(tmp_path / "desktop"))
    transcript = tmp_path / "t.jsonl"
    lines = [{"type": "user", "message": {"content": "<relay from='coordinator'><note>brief</note></relay>"}},
             {"type": "user", "message": {"content": "<wake><message id='m1'>Text to speech for the responses please</message></wake>"}},
             {"type": "user", "message": {"content": "<wake><message id='m2'>which thread am I hearing?</message></wake>"}}]
    transcript.write_text("\n".join(json.dumps(l) for l in lines))
    return {"session_id": "s1", "transcript_path": str(transcript), "cwd": "/home/u/code/hisho"}


def test_a_project_thread_is_named_by_its_first_message_and_opens_on_claude_ai(tmp_path, monkeypatch):
    data = _session(tmp_path, monkeypatch, "Remote Control session (from a project)", "cse_01ABC")
    t = ra.thread(data)
    assert t == {"name": "Text to speech for the responses please", "key": "s1",
                 "url": "https://claude.ai/code/session_01ABC"}
    assert ra.asked(data) == "which thread am I hearing?"


def test_a_titled_session_keeps_its_title_and_link(tmp_path, monkeypatch):
    data = _session(tmp_path, monkeypatch, "Souko monitor setup", "session_01XYZ")
    assert ra.thread(data)["name"] == "Souko monitor setup"
    assert ra.thread(data)["url"] == "https://claude.ai/code/session_01XYZ"


def test_the_thread_is_said_when_the_voice_moves_to_another_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "STATE", str(tmp_path))
    monkeypatch.setattr(ra, "LAST_THREAD", str(tmp_path / "last-thread"))
    conf = dict(ra.DEFAULTS)
    a = {"source": "Text to speech for the responses please", "thread": "s1", "segments": ra.segments("Hi.")}
    b = {"source": "Souko monitor setup", "thread": "s2", "segments": ra.segments("Done.")}
    assert ra.announced(a, conf)["segments"][0]["s"] == "From: Text to speech for the responses please."
    assert ra.announced(a, conf)["segments"] == a["segments"]           # same thread again: nothing said
    assert ra.announced(b, conf)["segments"][0]["s"] == "From: Souko monitor setup."
    assert ra.announced(a, {**conf, "announce_thread": False})["segments"] == a["segments"]
    selected = {"source": "", "segments": ra.segments("Some text.")}
    assert ra.announced(selected, conf) == selected                    # read-selection has no thread


def test_the_reader_shows_the_thread_in_its_own_colour_and_offers_to_open_it():
    now = {"source": "Text to speech for the responses please", "thread": "s1", "current": 0, "number": 1,
           "url": "https://claude.ai/code/session_01ABC", "segments": [{"s": "Hi.", "nl": True}]}
    screen = ra.render(now, [], 60, 12, CONF, 90)
    colour = f"\033[48;5;{ra.band('s1')}m"
    assert colour in screen and "Text to speech for the responses please" in screen
    assert "Super+Alt+O or click: open thread" in screen
    assert ra.band("s1") == ra.band("s1") and len({ra.band(f"s{i}") for i in range(30)}) > 3


def test_open_thread_opens_the_thread_being_read(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "NOW", str(tmp_path / "now.json"))
    monkeypatch.setattr(ra, "RUN", str(tmp_path / "run.jsonl"))
    opened = []
    monkeypatch.setattr(ra.subprocess, "Popen", lambda cmd, **k: opened.append(cmd))
    monkeypatch.setattr(ra, "notify", lambda *a, **k: None)
    monkeypatch.setattr(ra, "has_hyprland", lambda: False)    # no Hyprland: the default browser
    monkeypatch.setattr(ra, "clients", lambda: [])
    monkeypatch.setattr(ra, "log", lambda *a: None)
    monkeypatch.setattr(ra, "THREAD_WINDOW", str(tmp_path / "win.json"))
    assert not ra.open_thread()
    (tmp_path / "now.json").write_text(json.dumps({"url": "https://claude.ai/code/session_01ABC"}))
    assert ra.open_thread() and opened == [["xdg-open", "https://claude.ai/code/session_01ABC"]]


def test_pause_silences_at_once_and_resume_starts_that_sentence_again(tmp_path, monkeypatch):
    """Pause cuts the player off mid-sentence and holds the queue (a reply arriving meanwhile
    waits); Resume says the cut-off sentence again from its start, then carries on."""
    import sys
    import threading
    import time
    import types

    state = tmp_path / "state"
    for name, value in {"STATE": state, "QUEUE": state / "queue", "RUN": state / "last-run.jsonl",
                        "NOW": state / "now.json", "LOCK": state / "worker.lock", "PAUSED": state / "paused",
                        "PIDFILE": state / "worker.pid", "LAST_THREAD": state / "last-thread"}.items():
        monkeypatch.setattr(ra, name, str(value))
    monkeypatch.setattr(ra, "settings", lambda: {**ra.DEFAULTS, "auto_open": False, "announce_thread": False})
    monkeypatch.setattr(ra, "notify", lambda *a, **k: None)
    handlers = {}
    monkeypatch.setattr(ra.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))

    class Audio(_Audio):
        def __init__(self, text):
            super().__init__(int(ra.RATE * 0.4))   # every sentence: 0.4 s
            self.text = text
    np = types.SimpleNamespace(clip=lambda a, lo, hi: a, int16="int16")
    kokoro = types.SimpleNamespace(Kokoro=lambda *a: types.SimpleNamespace(create=lambda text, **k: (Audio(text), ra.RATE)))
    monkeypatch.setitem(sys.modules, "numpy", np)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", kokoro)
    heard = []   # (player number, sentence) in the order handed to a player

    class Player:
        made = 0

        def __init__(self, *a, **k):
            Player.made += 1
            n, self.dead = Player.made, False

            def write(b, me=self, n=n):
                if me.dead:
                    raise BrokenPipeError
            self.stdin = types.SimpleNamespace(write=write, flush=lambda: None, close=lambda: None)
            self.n = n

        def kill(self):
            self.dead = True

        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(ra.subprocess, "Popen", Player)
    real_write = ra.Speaker.write

    def spy(self, status, pcm, tries=2):
        heard.append((self.proc.n, status["segments"][status["current"]]["s"]))
        return real_write(self, status, pcm, tries)
    monkeypatch.setattr(ra.Speaker, "write", spy)
    monkeypatch.setattr(ra.os, "kill", lambda pid, sig: handlers[ra.signal.SIGUSR1]())

    ra.enqueue({"source": "A", "segments": ra.segments("One. Two. Three.")})
    worker = threading.Thread(target=ra.worker)
    worker.start()
    while [h for h in heard if h[1] == "Two."] == []:
        time.sleep(0.01)
    monkeypatch.setattr(ra, "worker_pid", lambda: 1)
    assert ra.pause() == "paused"                      # in the middle of "Two."
    ra.enqueue({"source": "B", "segments": ra.segments("Later.")})
    time.sleep(0.6)
    assert ra.read_json(ra.NOW)["paused"] and ra.read_json(ra.NOW)["segments"][1]["s"] == "Two."
    assert [h[1] for h in heard] == ["One.", "Two."]   # nothing more while paused
    assert ra.pause() == "resumed"
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert [h[1] for h in heard] == ["One.", "Two.", "Two.", "Three.", "Later."]
    assert heard[2][0] > heard[1][0]                     # "Two." again, on a fresh player
    assert [i["source"] for i in ra.run_items()] == ["A", "B"]



def test_skip_ends_the_reply_and_goes_on_with_the_next_one_waiting(tmp_path, monkeypatch):
    """Skip silences the reply being read and drops its other sentences; the next reply
    waiting is read at once on a fresh player, and the skipped one stays in the run."""
    import sys
    import threading
    import time
    import types

    state = tmp_path / "state"
    for name, value in {"STATE": state, "QUEUE": state / "queue", "RUN": state / "last-run.jsonl",
                        "NOW": state / "now.json", "LOCK": state / "worker.lock", "PAUSED": state / "paused",
                        "PIDFILE": state / "worker.pid", "LAST_THREAD": state / "last-thread"}.items():
        monkeypatch.setattr(ra, name, str(value))
    monkeypatch.setattr(ra, "settings", lambda: {**ra.DEFAULTS, "auto_open": False, "announce_thread": False})
    monkeypatch.setattr(ra, "notify", lambda *a, **k: None)
    handlers = {}
    monkeypatch.setattr(ra.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))

    class Audio(_Audio):
        def __init__(self, text):
            super().__init__(int(ra.RATE * 0.4))   # every sentence: 0.4 s
            self.text = text
    np = types.SimpleNamespace(clip=lambda a, lo, hi: a, int16="int16")
    kokoro = types.SimpleNamespace(Kokoro=lambda *a: types.SimpleNamespace(create=lambda text, **k: (Audio(text), ra.RATE)))
    monkeypatch.setitem(sys.modules, "numpy", np)
    monkeypatch.setitem(sys.modules, "kokoro_onnx", kokoro)
    heard = []   # (player number, sentence) in the order handed to a player

    class Player:
        made = 0

        def __init__(self, *a, **k):
            Player.made += 1
            n, self.dead = Player.made, False

            def write(b, me=self, n=n):
                if me.dead:
                    raise BrokenPipeError
            self.stdin = types.SimpleNamespace(write=write, flush=lambda: None, close=lambda: None)
            self.n = n

        def kill(self):
            self.dead = True

        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(ra.subprocess, "Popen", Player)
    real_write = ra.Speaker.write

    def spy(self, status, pcm, tries=2):
        heard.append((self.proc.n, status["segments"][status["current"]]["s"]))
        return real_write(self, status, pcm, tries)
    monkeypatch.setattr(ra.Speaker, "write", spy)
    monkeypatch.setattr(ra.os, "kill", lambda pid, sig: handlers[sig]())

    ra.enqueue({"source": "A", "segments": ra.segments("One. Two. Three.")})
    worker = threading.Thread(target=ra.worker)
    worker.start()
    while [h for h in heard if h[1] == "Two."] == []:
        time.sleep(0.01)
    monkeypatch.setattr(ra, "worker_pid", lambda: 1)
    ra.enqueue({"source": "B", "segments": ra.segments("Later.")})
    assert ra.skip("test")                                   # in the middle of "Two."
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert [h[1] for h in heard] == ["One.", "Two.", "Later."]
    assert heard[2][0] > heard[1][0]                         # B on a fresh player
    assert [i["source"] for i in ra.run_items()] == ["A", "B"]

def test_the_thread_name_is_drawn_larger_in_kitty_on_two_lines_at_most(monkeypatch):
    now = {"source": "did we add the text to speech that I had on portable AI here for the responses",
           "thread": "s1", "url": "https://claude.ai/code/session_01ABC", "current": 0, "segments": []}
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    plain = ra.title_band(now, 40, CONF)
    assert "\033]66;" not in "".join(plain)
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    big = ra.title_band(now, 40, CONF)
    texts = [l for l in big if "\033]66;s=2;" in l]
    assert len(texts) == 2 and texts[-1].split(";", 2)[2].rstrip("\a\033[0m ").endswith("…")
    assert len(big) == 1 + 2 * 2 + 1                     # pad, 2 large lines of 2 rows each, hint
    assert "\033]66;s=3;" in "".join(ra.title_band(now, 40, {**CONF, "title_size": 3}))


def test_paused_is_shown_in_the_band():
    now = {"source": "A thread", "thread": "s1", "paused": True, "current": 0, "segments": []}
    assert "PAUSED · Super+Alt+P or click resumes" in "".join(ra.title_band(now, 60, CONF))


def test_opening_the_thread_pauses_reading_unless_turned_off(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "NOW", str(tmp_path / "now.json"))
    monkeypatch.setattr(ra, "PAUSED", str(tmp_path / "paused"))
    (tmp_path / "now.json").write_text(json.dumps({"url": "https://claude.ai/code/session_01ABC"}))
    monkeypatch.setattr(ra.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(ra, "worker_pid", lambda: 123)
    monkeypatch.setattr(ra, "has_hyprland", lambda: False)
    monkeypatch.setattr(ra, "clients", lambda: [])
    monkeypatch.setattr(ra, "log", lambda *a: None)
    monkeypatch.setattr(ra, "THREAD_WINDOW", str(tmp_path / "win.json"))
    paused = []
    monkeypatch.setattr(ra, "pause", lambda source="": paused.append(source))
    monkeypatch.setattr(ra, "settings", lambda: {**ra.DEFAULTS})
    ra.open_thread("Super+Alt+O key")
    monkeypatch.setattr(ra, "settings", lambda: {**ra.DEFAULTS, "auto_pause": False})
    ra.open_thread("Super+Alt+O key")
    assert paused == ["opening the thread (Super+Alt+O key)"]


def _relay(title):
    return {"type": "user", "message": {"content": (
        '<relay from="coordinator" session="session_x" current-time="t">\n  The note below...\n  <note>\n'
        f"      Thread: {title}\n      do the thing\n  </note>\n</relay>")}}


def test_the_assistants_thread_line_names_the_thread_and_a_rename_wins(tmp_path, monkeypatch):
    data = _session(tmp_path, monkeypatch, "Remote Control session (from a project)", "cse_01ABC")
    monkeypatch.setattr(ra, "NAMES", str(tmp_path / "names.json"))
    path = data["transcript_path"]
    with open(path, "a") as f:
        f.write("\n" + json.dumps(_relay("Text to speech for responses")))
    assert ra.thread(data)["name"] == "Text to speech for responses"
    with open(path, "a") as f:                       # renamed on claude.ai: the latest line wins
        f.write("\n" + json.dumps(_relay("Read aloud &amp; reader window")))
    assert ra.thread(data)["name"] == "Read aloud & reader window"
    assert ra.prompts(data) == ["Text to speech for the responses please", "which thread am I hearing?"]


def test_a_name_typed_for_a_thread_wins_over_everything(tmp_path, monkeypatch):
    data = _session(tmp_path, monkeypatch, "Souko monitor setup", "session_01XYZ")
    for name in ("NAMES", "NOW", "RUN", "STATE"):
        monkeypatch.setattr(ra, name, str(tmp_path / name.lower()))
    monkeypatch.setattr(ra, "log", lambda *a: None)
    assert not ra.name_thread("Monitors")             # nothing being read, nothing named
    (tmp_path / "now").write_text(json.dumps({"thread": "s1"}))
    assert ra.name_thread("  Monitors   and   displays ")
    assert ra.thread(data)["name"] == "Monitors and displays"
    band = "".join(ra.title_band({"source": "old name", "thread": "s1", "segments": []}, 60, CONF))
    assert "Monitors and displays" in band and "old name" not in band


def test_typing_into_the_reader_never_pauses_opens_or_closes_it():
    """Keyboard focus follows the mouse on Omarchy: words typed while the pointer rests on
    the reader land in it. They used to pause (space), open the thread (o) and close it (q)."""
    typed = "so I want to open the thread now, ok? -- quick +++ test"
    assert ra.reader_input(typed, 24, 80, 4, 78) == (78, [])
    assert ra.reader_input("\x0e", 24, 80, 4, 78) == (78, ["name"])            # Ctrl+N
    assert ra.reader_input("\x1b[<0;10;2M", 24, 80, 4, 78)[1] == ["band"]       # a click on the band
    assert ra.reader_input("\x1b[<0;10;9M", 24, 80, 4, 78)[1] == []             # a click elsewhere
    assert ra.reader_input("\x1b[<64;10;9M", 24, 80, 4, 78)[0] == 83            # wheel still sets opacity


def test_every_pause_and_resume_says_what_asked_for_it(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(ra, "log", logged.append)
    monkeypatch.setattr(ra, "PAUSED", str(tmp_path / "paused"))
    monkeypatch.setattr(ra, "worker_pid", lambda: 99999999)
    monkeypatch.setattr(ra.os, "kill", lambda *a: None)
    ra.pause("dashboard")
    ra.pause("click on the reader band")
    assert logged == ["paused by: dashboard", "resumed by: click on the reader band"]


def test_a_thread_opens_in_one_window_brought_forward_or_replaced(tmp_path, monkeypatch):
    """Pausing by clicking the band again and again never piles up tabs: the same thread's
    window is only brought forward; a different thread replaces the old window."""
    for name in ("NOW", "RUN", "PAUSED", "THREAD_WINDOW"):
        monkeypatch.setattr(ra, name, str(tmp_path / name.lower()))
    monkeypatch.setattr(ra, "has_hyprland", lambda: True)
    monkeypatch.setattr(ra.shutil, "which", lambda cmd: "/usr/bin/" + cmd)
    monkeypatch.setattr(ra, "worker_pid", lambda: None)
    monkeypatch.setattr(ra, "log", lambda *a: None)
    windows = {"0xa": {"address": "0xa"}}                     # the reader, say
    monkeypatch.setattr(ra, "clients", lambda: list(windows.values()))
    dispatched, launched = [], []
    monkeypatch.setattr(ra, "hypr", lambda *a: dispatched.append(a[-1]) or "")

    def popen(cmd, **k):
        launched.append(cmd)
        if cmd[1:3] == [os.path.abspath(ra.__file__), "thread-window"]:   # run the helper in place
            windows["0xnew%d" % len(launched)] = {"address": "0xnew%d" % len(launched)}
            ra.thread_window(cmd[3], set(json.loads(cmd[4])))
    monkeypatch.setattr(ra.subprocess, "Popen", popen)
    monkeypatch.setattr(ra.time, "sleep", lambda s: None)

    def reading(url):
        (tmp_path / "now").write_text(json.dumps({"url": url}))

    reading("https://claude.ai/code/session_A")
    ra.open_thread("click")
    first = ra.read_json(ra.THREAD_WINDOW)
    assert first["url"].endswith("session_A") and first["address"] != "0xa"
    for _ in range(3):                                        # clicked again and again
        ra.open_thread("click")
    assert sum("omarchy-launch-webapp" in c for c in launched) == 1
    assert dispatched[-1] == f'hl.dsp.focus({{ window = "address:{first["address"]}" }})'
    reading("https://claude.ai/code/session_B")               # another thread
    ra.open_thread("click")
    assert f'hl.dsp.window.close({{ window = "address:{first["address"]}" }})' in dispatched
    assert ra.read_json(ra.THREAD_WINDOW)["url"].endswith("session_B")
    assert sum("omarchy-launch-webapp" in c for c in launched) == 2


def test_keys_name_themselves_and_anything_else_names_its_caller(monkeypatch):
    os.environ.setdefault("ADDON", str(HERE))
    assert ra.caller().startswith("command line (from ")
    src = (HERE / "bin" / "hypr_keys.py").read_text()
    assert '" --open-thread", " --pause"' in src and '" key"' in src


def test_the_rename_editor_starts_with_the_name_and_enter_saves_esc_cancels():
    def run(keys, initial="did we add the text…"):
        keys = list(keys)
        return ra.edit_line(lambda: keys.pop(0) if keys else None, lambda p, t: None, "Name: ", initial)
    assert run("\r") == "did we add the text…"                                  # Enter keeps it
    assert run("\x15Text to speech for responses\r") == "Text to speech for responses"
    assert run("\x7f\x7f\x7f\x7fX\r", "abcdef") == "abX"
    assert run("abc\x1b") is None                                               # Esc cancels


def test_rename_works_on_the_last_thread_when_nothing_is_read(tmp_path, monkeypatch):
    for name in ("NOW", "RUN", "NAMES"):
        monkeypatch.setattr(ra, name, str(tmp_path / name.lower()))
    (tmp_path / "run").write_text(json.dumps({"source": "first message", "thread": "s9"}) + "\n")
    assert ra.current_thread() == ("s9", "first message")
    (tmp_path / "names").write_text(json.dumps({"s9": "Monitors"}))
    assert ra.current_thread() == ("s9", "Monitors")


def test_the_hints_show_the_rename_key():
    assert "N rename" in ra.footer(CONF, 120)
    now = {"source": "A", "thread": "s1", "url": "https://claude.ai/code/session_1", "segments": []}
    assert "Super+Alt+N rename" in "".join(ra.title_band(now, 120, CONF))


def test_the_rename_line_never_wraps_however_long_the_name():
    """A line wider than the reader wrapped, and every Backspace left one more copy behind."""
    import re as _re
    prompt = "Thread name (Enter saves, Esc cancels): "
    for cols, text in ((60, "did we add the text to speech that I had on portable AI here"), (30, "x" * 200), (90, "short")):
        visible = _re.sub(r"\x1b\[[0-9;]*m", "", ra.prompt_line(prompt, text, cols))
        assert len(visible) < cols
        assert visible.endswith(text[-5:])               # the end, where the typing is, stays in view


def test_stop_keeps_the_replies_that_were_waiting_for_repeat(tmp_path, monkeypatch):
    for name in ("QUEUE", "RUN", "PAUSED", "PIDFILE", "STATE"):
        monkeypatch.setattr(ra, name, str(tmp_path / name.lower()))
    monkeypatch.setattr(ra.subprocess, "Popen", lambda *a, **k: None)
    (tmp_path / "run").write_text(json.dumps({"source": "A", "segments": []}) + "\n")
    ra.enqueue({"source": "B", "segments": []})
    ra.enqueue({"source": "C", "segments": []})
    ra.stop("test")
    assert ra.queued() == []
    assert [i["source"] for i in ra.run_items()] == ["A", "B", "C"]


MONITORS = [  # Souko after the 2026-10-21 reboot (scale 1.6: 3840x2160 shows as 2400x1350)
    {"x": 3327, "y": 3568, "width": 3840, "height": 2160, "scale": 1.6},
    {"x": 5727, "y": 2218, "width": 3840, "height": 2160, "scale": 1.6},
    {"x": 6171, "y": 3464, "width": 3840, "height": 2160, "scale": 1.6},
]


def test_a_saved_spot_below_every_screen_is_not_used():
    """The monitors came back arranged differently after a reboot; the saved spot (y 5407)
    was below the bottom of every screen, so the reader opened where no one could see it."""
    assert ra.fit_on_screen(3780, 5407, 771, 639, MONITORS) is None


def test_a_spot_hanging_off_an_edge_is_pulled_back_onto_its_monitor():
    x, y, w, h = ra.fit_on_screen(5300, 4400, 771, 639, MONITORS)    # centre on the first, bottom cut off
    assert (w, h) == (771, 639) and y + h <= 3568 + 1350 and x + w <= 3327 + 2400
    assert ra.fit_on_screen(4000, 4000, 771, 639, MONITORS) == (4000, 4000, 771, 639)   # fine as it is
