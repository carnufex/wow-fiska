# Fiska

One-key fishing helper for WoW together with the **Single-Button Fishing (SBF)** addon.
SBF does everything on one key (F10): applies a lure when the old one has run out, casts, and
reels in. `fiska.py` presses that key and decides *when*.

## How a bite is detected

The game has no bite event. Sound turned out to be useless for it: in recorded casts the
ambience was as loud as the splash (-30..-36 dB), and other players' splashes are heard too.
So the bite is **seen** at *our* bobber:

| Signal | What it gives |
|---|---|
| **Vision** (primary) | after every cast the script finds our bobber: what newly appeared and then sits still, with newly appeared red (the feather) weighted. A bite = the median frame-to-frame change in that small box over 0.1 s reaches 2.5x the calm level (and +1.0). Other players' bobbers are elsewhere and do not count. On 6 recorded bites: all detected, 0.55-0.7 s before a human reacted; calm water peaked at 0.7 of the threshold |
| **FiskaSignal addon** (`addon/FiskaSignal`) | two squares in the top-left corner: blue = loaded, red = channeling Fishing, green = loot opened; second square red = casting (lure/bait). Confirms a cast started, waits out lure casts, knows when the bobber is gone and whether a reel-in caught something |
| **Audio** (optional) | `--confirm both` additionally requires a splash; `--confirm audio` uses sound only |

If the bobber is not found on a cast, the script reels in and casts again (`--no-bobber audio`
falls back to sound). Without the addon it uses timeouts.

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
