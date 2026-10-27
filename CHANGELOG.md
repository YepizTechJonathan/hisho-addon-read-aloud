# Changelog

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

