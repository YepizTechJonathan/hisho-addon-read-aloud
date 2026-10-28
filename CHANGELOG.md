# Changelog

## 0.9.1

- `fallback_output` is optional: left empty (the default) it no longer stops install and update with "fallback_output is required".

## 0.9.0

- A dead audio output no longer hangs reading silently. Before playing, the outputs are read from PipeWire (pw-dump): when the default one is in error (Dante without its clock, say) the voice reads on another and says so once, e.g. "Dante output isn't working. Reading on Arctis Nova Pro Wireless instead." New setting `fallback_output` (a PipeWire node name) picks that output; empty picks a USB or Bluetooth headset before HDMI.
- Stall watchdog: audio is handed to the player in chunks, and a player that takes none for 4 seconds (not paused or skipping) is killed and the sentence said again on another output. With none working: "No working audio output", reading stops and the run (with what was waiting) stays for Repeat.
- No more leftover processes: stopping, finishing or skipping always reaps the player (a player on a dead output never ended, and the reader waited for it forever); Stop kills it too; the reader window's launcher no longer stays as <defunct>.
- Status (dashboard button, `say-it --status`) shows the output in use and why, and any output in error.
## 0.8.3

- Install works before Claude Code is installed (a new machine's first boot): ~/.claude is created for the Stop hook.

## 0.8.2

- README says what you need (addon.yaml you_need) and how to use it; the README check requires both.

## 0.8.1

- The reader never opens off-screen: after a reboot the monitors can come back arranged differently, and the saved spot was below every screen, so the window opened where it couldn't be seen. A spot off every screen is now replaced by the default (by the Claude app), a spot hanging off an edge is pulled back onto its monitor, and an off-screen spot is never saved.

## 0.8.0

- Skip (Super+Alt+J, or the dashboard's "Skip to the next reply"): ends the reply being read and goes straight to the next one waiting. The skipped reply stays in the run, so Repeat still has it. Skipping while paused goes on with the next reply.
- Stop no longer loses the replies that were waiting: they are added to the run, so Repeat reads them.

## 0.7.1

- Rename prompt stays on one line: a name longer than the window wrapped, and every Backspace left another copy of it on screen. Long names now scroll to their end.

## 0.7.0

- Rename a thread with Super+Alt+N: the reader comes forward with the thread's current name ready to edit at its bottom line; Enter saves, Esc cancels, Ctrl+U clears. Ctrl+N inside the reader does the same. The reader's hints show the rename key.

## 0.6.0

- Opening a thread uses one window: the same thread already open is brought forward (clicking the band to pause again and again no longer opens a tab each time); a different thread replaces that window.
- read-aloud.log names what paused or opened: the key by name ("Super+Alt+O key"), the band click, the dashboard, or for anything else the program that ran it.

## 0.5.1

- Moved out of Hisho into its own repository (hisho-addon-read-aloud); installed from the add-on store.

