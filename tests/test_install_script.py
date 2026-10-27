"""bin/install.sh on a machine where Claude Code isn't installed yet (a new Souko's first boot)."""
from pathlib import Path

INSTALL = (Path(__file__).resolve().parents[1] / "bin" / "install.sh").read_text()


def test_the_stop_hook_is_written_even_before_claude_code_exists():
    """2026-10-27, the Souko disk image: ~/.claude did not exist yet and the install stopped there."""
    assert INSTALL.index("os.makedirs(os.path.dirname(path), exist_ok=True)") < INSTALL.index("json.dump(data, open(tmp")
