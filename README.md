# Read aloud (Hisho add-on)

## What it does

Every Claude Code reply on your workstation is read aloud with Kokoro, a local voice model that
runs on the CPU. A reply that arrives while another is being read waits its turn; Repeat replays
the last one; "From: <thread>." is said when the voice moves to another thread. A reader window
opens next to the Claude app while reading: the thread in a band of its own colour (a click opens
it), what was asked, the sentence being spoken highlighted, and an opacity slider. Keys are
Hyprland (Omarchy) binds, refused if already in use; without Hyprland replies are still read
aloud, with no keys or reader window.

When the default audio output is broken (PipeWire shows it in error, as Dante is without its
clock) the voice reads on another output instead and says so once: the `fallback_output` setting
if that one works, else a USB or Bluetooth headset before HDMI. A player that takes no audio for
4 seconds is dropped and the sentence is said again on another output; with no working output
at all you get "No working audio output" and reading stops, the run kept for Repeat. It never
hangs silently. The dashboard's **Status** shows the output in use and any output in error.

## What it needs

- You need:
  - Nothing beyond Hisho on the workstation: Claude Code, PipeWire and about 600 MB of disk for the local voice
  - Optional: Hyprland (Omarchy) for the keys and the reader window
- Runs on: the workstation (Fleet.yaml `workstation`)
- Commands on that machine: `python3`, `pw-cat`, `curl`, `sha256sum`
- Settings in `~/Hisho/System/Config/Addons/read-aloud.yaml`:
  - `voice` (str, default `af_heart`)
  - `speed` (int, default `100`)
  - `opacity` (int, default `90`)
  - `auto_open` (bool, default `True`)
  - `announce_thread` (bool, default `True`)
  - `auto_pause` (bool, default `True`)
  - `title_size` (int, default `2`)
  - `fallback_output` (str, default ``)
  - `key_read` (str, default `SUPER + ALT + T`)
  - `key_repeat` (str, default `SUPER + ALT + R`)
  - `key_stop` (str, default `SUPER + ALT + X`)
  - `key_mute` (str, default `SUPER + ALT + M`)
  - `key_reader` (str, default `SUPER + ALT + V`)
  - `key_open` (str, default `SUPER + ALT + O`)
  - `key_pause` (str, default `SUPER + ALT + P`)
  - `key_rename` (str, default `SUPER + ALT + N`)
  - `key_skip` (str, default `SUPER + ALT + J`)
  - `kokoro_onnx` (str, default `0.4.7`)

## How to install

On the dashboard: **Add-ons > Store > Read aloud > Install**. Or from a terminal:

```bash
hisho addon install read-aloud --on souko
```

Hisho takes the newest version from this repository and copies it to the machine it runs on, runs its install and turns it on. Updates show as **Update
available** on its Store card; **Roll back** returns to the version before.

The Souko installer installs it by itself (`HISHO_READ_ALOUD=no` skips it). The venv and the voice
model (sha256-checked) are downloaded on install into `~/.local/share/hisho/read-aloud`.

## How to use

Once installed, every Claude Code reply is read aloud. Keys (Hyprland): Super+Alt+T reads the highlighted text, R repeats, X stops, M mutes, V shows or hides the reader window, O opens the thread, P pauses. Its page (**Add-ons > Read aloud**) sets the voice, speed, opacity and keys.

## Settings

On its page or in `~/Hisho/System/Config/Addons/read-aloud.yaml`: the Kokoro voice, speed, the
reader's opacity, whether the reader opens by itself, whether the thread is announced, the output to read on
when the default one is broken (`fallback_output`, a PipeWire node name such as
`alsa_output.usb-SteelSeries_Arctis_Nova_Pro_Wireless-00.analog-stereo` from `wpctl status` /
`pw-dump`; empty picks one), and the keys (`none` leaves a key out). Defaults: Super+Alt+T read the highlighted text, R repeat, X stop, M
mute, V reader window, O open the thread, P pause / resume.

## How to remove

**Add-ons > Store > Read aloud > Remove**, or `hisho addon remove read-aloud`. Removes the Claude Code hook and the keys; the venv and voice model stay in
`~/.local/share/hisho/read-aloud`. Your settings stay in
`~/Hisho/System/Config/Addons/read-aloud.yaml`.

## License

Source-available under the [PolyForm Shield License 1.0.0](LICENSE.md): free to use, change and share,
for any purpose except offering a product that competes with Hisho (reselling it, for example).
