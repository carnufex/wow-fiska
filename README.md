# Fiska

One-key fishing helper for WoW together with the **Single-Button Fishing (SBF)** addon.
SBF does everything on one key (F10): applies a lure when the old one has run out, casts, and
reels in. `fiska.py` presses that key and decides *when*.

## How a bite is detected

The game has no bite event, so the splash is the only cue. To ignore other players' splashes,
a splash only counts when the signals agree:

| Signal | What it gives |
|---|---|
| **Audio** (WASAPI loopback of the default output) | a loud transient over the recent background |
| **Vision** | after every cast the script finds *our* bobber: what appeared in the picture where nothing changed before the key press (water flicker is discounted). A splash only counts if that small box moves at the same moment |
| **FiskaSignal addon** (`addon/FiskaSignal`) | a 4x4 status pixel in the top-left corner: blue = loaded, red = channeling Fishing, green = loot window opened. Confirms a cast really started (vs. a lure being applied), when the bobber is gone, and whether a reel-in caught something |

Every signal is optional: without the addon the script uses timeouts; if the bobber is not found
on a cast it trusts audio for that cast.

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Copy `addon/FiskaSignal` into `World of Warcraft/_forever_/Interface/AddOns/` and `/reload`.
In SBF's options, enable **focus audio** (mutes music/ambience while fishing).

## Use

```bash
.venv\Scripts\python fiska.py --calibrate     # audio peak meter + addon status
.venv\Scripts\python fiska.py --dry-run --debug-dir debug --record rec   # you fish, it scores itself
.venv\Scripts\python fiska.py                 # fish; Pause = start/pause
```

Keys are only sent while the foreground window title contains "World of Warcraft".
`debug/castNNNN_found.png` shows where the bobber was found each cast (red square).
In `--dry-run` every reel-in is scored (DETECTED / MISSED / false alarms) and the log shows the
loudest sound and largest bobber motion just before you reeled in. `--record` saves per-cast
traces and the pre/post frames (`rec/castNNNN.npz`) for offline tuning.
Tune with `--threshold`, `--margin`, `--search`, `--exclude`, `--bobber-min`; see `--help`.

## Tests

```bash
.venv\Scripts\python -m unittest test_fiska
```
