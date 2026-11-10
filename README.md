# Fiska

One-key fishing helper for WoW together with the **Single-Button Fishing (SBF)** addon.
SBF does everything on one key (F10): applies a lure when the old one has run out, casts, and
reels in. `fiska.py` presses that key and decides *when*.

## How a bite is detected

The game has no bite event. Sound turned out to be useless for it: in recorded casts the
ambience was as loud as the splash (-30..-36 dB), and other players' splashes are heard too.
So the bite is **seen** at *our* bobber. Every rule below was fitted on recorded sessions
(~150 casts, 112 real bites) and is replayed against them before it ships.

**Finding the bobber** (after every cast)

- what newly appeared between frames from just before the key press and just after landing,
  and then sits still; newly appeared red (the feather) weighs 3x;
- small and alone: centre minus 3x the ring around it, so pop-up UI and big objects lose;
- at two sizes (full and half): a zoomed-out camera makes the bobber half as big, and at full
  size its few pixels drowned in rain;
- away from the frame edge (party-frame portraits sit there) and from the character (its head,
  just above the excluded box, won on a zoomed-out camera);
- **landed**: the search is repeated (up to 2 more looks) until two in a row agree - on a long
  cast the bobber was still in the air when the first frames were taken;
- **usual area first**: with the camera behind the character the bobber lands straight ahead,
  at the same place on screen at every spot so far (492 catches at 3 spots: x 0.46-0.54,
  y 0.30-0.50 of the window). That box (`--prior`) is searched first; at a sun-lit dock an idling
  NPC, nameplates and the glitter on the water won 12 casts out of 27 before this, and 0 after;
- **learned landing zone**: after 5 catches, only where our bobbers have been landing - median
  +- 3 robust sigmas, so one lucky "catch" on an NPC can't stretch it (each step falls back to
  the next, and to the whole area, when nothing is there). Players walking onto the dock and
  stopping outscored the bobber 8 times in one session before this. The zone is saved between
  sessions; 3 failed casts in a row in it (a new fishing spot) and it is forgotten and learned
  again;
- **water check**: the box must ripple like water (calm 0.3-2.5); wood/UI is ~0.1, someone
  walking ~4. Otherwise: reel in and cast again.

**Deciding it is a bite**

- median frame-to-frame change over 0.1 s reaches 2.2x calm, and at least calm + 0.6 (0.8 missed
  a weak bite on dark, calm water by 1 %; replaying 500 earlier casts gave no new false alarms);
- not in the first 2.5 s of watching (earliest of 96 real bites: 3.2 s);
- trigger peak at most 5.5 (real bites <= 4.51) and no over-cap burst in the last second;
- it must **hold** for 0.2 s: median 0.6-2.0x the threshold (real bites 0.72-1.35x; a blip
  falls back, something walking in keeps growing).
- the **bobber must move**: a bite pulls it under and throws a splash over it. The centre of the
  box as it looked 0.4-1.0 s before is searched for in the frames of the hold; on 497 recorded
  bites it had moved >= 3.4 px in 99 % (box 42 px), while sun glitter, the little fish of a
  catch's swirl and streaks passing by left it within 1.4 px in 18 of 21 false bites. Under 5 %
  of the box (min 1.5 px): look again 0.3 s later, then ignore. Cost: 7 of 420 Darnassus bites
  with the bobber right behind the character's head (the head doesn't move).

Every cast gets a bite before the Fishing channel ends, so a cast that ends without one is a
**missed bite** (the log says `MISSED the bite`): so far always the search watching the wrong
spot.

| Signal | What it gives |
|---|---|
| **Vision** (primary) | the above |
| **FiskaSignal addon** (`addon/FiskaSignal`) | two squares in the top-left corner: blue = loaded, red = channeling Fishing, green = loot opened; second square red = casting (lure/bait). Confirms a cast started, waits out lure casts, knows when the bobber is gone and whether a reel-in caught something. The log warns when the game still runs the old 1.0 |
| **Audio** (optional) | `--confirm both` additionally requires a splash; `--confirm audio` uses sound only |

Only one `fiska.py` may run: two of them reel in each other's casts.

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Copy `addon/FiskaSignal` into `World of Warcraft/_forever_/Interface/AddOns/` and `/reload`.
In SBF's options, enable **focus audio** (mutes music/ambience while fishing).

## Use

```bash
.venv\Scripts\python fiska.py --record rec                 # fish; Pause = start/pause
.venv\Scripts\python fiska.py --dry-run --record rec       # you fish, it scores itself
.venv\Scripts\python fiska.py --calibrate                  # audio meter + addon status
```

Keys are only sent while the foreground window title contains "World of Warcraft".

**Breaks**: after a random 20-30 min of fishing it rests a random 5-10 min, always between
casts (the log says `break: 7.3 min after 24.1 min of fishing (back at 13:02)`). Pausing it
yourself for at least 5 min counts as the break; Pause during a break stops it (PAUSED).
`--work-minutes 40,60 --break-minutes 3,6` changes the ranges, `--break-minutes 0` turns
breaks off. Dry runs never break.

## What a session leaves behind (`--record DIR`)

| File | Contents |
|---|---|
| `fiska-YYYYMMDD-HHMMSS.log` | the console log |
| `casts.csv` | one row per cast: outcome (caught / empty / no_bite = missed bite / not_water / no_bobber / lure / ...), bobber position, search score and whether the landing zone was used, bobber size, calm level, trigger level, threshold, peak, hold (x threshold), bobber shift (px) and bites ignored because it stayed still, bursts, blips, duration |
| `castNNNN.npz` | pre/post frames of the bobber search, motion and audio traces, bite times, and ~1.8 s of bobber-box frames around every bite and ignored burst, and before a cast ended without our bite (to see a missed bite) |
| `castNNNN_bobber.png` | where the bobber was found (red square) |
| `castNNNN_bite.png` / `_burst.png` / `_notwater.png` / `_ended.png` | the search area at that moment |

In `--dry-run` every reel-in is also scored (DETECTED / MISSED / false alarms) with the loudest
sound and largest bobber motion just before it.

## Tests

```bash
.venv\Scripts\python -m unittest test_fiska
```
