# Changelog

## 0.10.0

- Reading no longer pauses by itself. Hisho's hourly code sync runs these tests on Souko, and a test that opens the thread found the real worker.pid and paused the real reading (SIGUSR1, with its log line swallowed), the "random" pauses seen on Souko (Nov 5 20:25:57 and 22:04:51). Every test now runs in a scratch state folder, checked by a test. Every pause and resume logs what asked for it with the pid and the processes it came from, and a paused marker made by anything else is logged as "paused by: unknown".
- The voice no longer makes the PC unusable while it reads. onnxruntime used every core and kept them busy-waiting (about 25 of 32 busy on Souko, 74 CPU-seconds for 14 s of speech, and the PC unusable). Now there is no busy-waiting on any computer, and the cores fit each one: on install and update the voice is timed silently (`read_aloud.py calibrate`, also redone when the processor, core count or onnxruntime changes) at 1, 2, 4 ... threads, up to half the real cores (hyperthreads not counted, only the CPUs the worker may use), and the fewest that make speech 3 times faster than it plays are kept in `~/.local/state/hisho/read-aloud/calibration.json`. New setting `cpu_threads`: `auto` (default) or a number that overrides it.
- A computer too slow for the voice (under 1.5 times faster than speech even at its best) is told so at install and in Status: "This computer is too slow for the local voice to read without gaps". Install doesn't fail.
- More than 3 "voice behind" gaps in a run move the next run up one step of threads (up to the cap), noted in calibration.json and the log.
- Low priority (`low_priority`, default on) is nice 10 and idle disk priority, set on the worker's autogroup too (with autogroup scheduling, on by default on Ubuntu and Omarchy, nice alone only ranks it against itself). Not nice 19: on a fully busy PC that starves the voice into gaps, and a process can't raise its priority back.
- A graphics card is used when onnxruntime offers one (CUDA, ROCm, DirectML, Core ML), the CPU after it; `use_gpu: no` keeps it on the CPU. onnxruntime-gpu isn't installed for you (a large download).
- Under 2 GB of free memory when reading starts is logged as a warning.
- Up to 4 sentences are made ahead of the one playing (was 2), and more than 0.3 s of silence because the next sentence wasn't made yet is logged ("voice behind: 0.8 s of silence before sentence 3 of 12 (240 characters)"), so a silence that isn't a pause can be explained afterwards.

## 0.9.3

- Every field on its setup form says in plain words what it is, and links to where to get it (addon.yaml `help:`, shown by Hisho's dashboard).

## 0.9.2

- A stalled player while PipeWire itself doesn't answer (Dante's driver blocking it, found testing 0.9.1 on Souko) now ends with "No working audio output" instead of trying the default output again every 10 seconds forever.

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

