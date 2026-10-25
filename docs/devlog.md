# Devlog

The project's history, for a video about an AI learning to play Slay the Spire 2 from the screen alone. Newest entries at the bottom. Each entry keeps the numbers, the run IDs (folders under `runs/` on Raph's PC) and what broke, since the failures make the best footage.

Make footage from any run with `python -m sts_cua.replay runs/<run>` (see the README's "Video footage").

## 2026-10-25

### The idea

- Build a computer-use agent (CUA) that plays Slay the Spire 2 the way a person does: it sees screenshots and sends mouse clicks and key presses. It gets no game API.
- It runs on Raph's PC: Windows + WSL2 Ubuntu, RTX 4090.
- Models: Claude through the API first (Haiku 5.5 is the cheapest), then a local open model on the 4090.
- The research question: how much does thinking help? We run the same harness with reasoning on and off.

### Harness

- The loop: screenshot → the model picks actions → xdotool performs them → everything is logged (thought, actions, cost, screenshot per step).
- Test 1 was a click-target page, not the game. Haiku hit 5 of 5 targets for about $0.0002 a step.

### Getting Spire 2 to run in WSL

- The game runs on a virtual screen (Xvfb :99, 1280x720). The GPU goes through `GALLIUM_DRIVER=d3d12`, so it renders on the 4090.
- Launching through Steam failed: Steam's Linux runtime container can't see the WSL GPU. The fix is to launch the game binary directly with the Steam app ID set and Steam running.
- Quirks found on the way:
  - The first frame needs a 1-pixel window resize to draw properly.
  - The game ignores SIGTERM and hangs on exit, so it gets force-killed.
  - The first click after the window gets focus is swallowed.
  - Clicks only register when the mouse hovers 0.25 s and then holds the button 0.2 s.
  - Pressing a card's number twice deselects it.
  - Skills like Defend play by clicking empty space, not an enemy.
  - Escape in combat opens the pause menu, where "Give Up" ends the run.
- First real run: Haiku went from the main menu through character select and the tutorial prompt ("No") to the Act 1 map in 3 clicks, for $0.0037.

### Ground truth from a mod

- STS2MCP (an open-source Spire 2 mod) exposes the game state on localhost. The harness logs it every step: floor, HP, hand, enemies. The AI never sees it; it only lets us score runs and find mistakes.
- Release 0.4.0 crashed on game v0.107, so we built it from source (main @ 55e0648).

### First fight won

- Run `20261025T164617Z`: Haiku beat a Nibbit in 14 steps for $0.0056.

### First loop of doom

- Run `20261025T165758Z`, goal "reach floor 5": the agent reached floor 3, then clicked an already-visited map node 39 times in a row. It thought the node it had just cleared was its next choice.
- Fix (PR #3): the prompt explains that map paths go upward from your marker, each step tells the model when its last click didn't change the screen, and a run stops after 8 unchanged steps.

### The agent gets its own Steam account

- The agent plays on its own account ("budthud") with its own copy of the game, so Raph's saves are never touched. The second account also makes co-op with the AI possible later.
- The game is set to low settings, a 1280x720 window and fast mode.

### The disk filled up

- C: ran out of space. WSL hit I/O errors and Steam crashed.
- Cleanup: removed unused Ollama models and compacted the WSL disk from 126 GB to 38 GB. Added desktop on/off buttons that shut WSL down to free memory for gaming.

### "Steam failed to initialize"

- After a restart, the on button launched the game 18 s before Steam had logged in. The game sat on a Steam error and the mod never started.
- Cause: the startup script found an old "Logged On" line from the last session in Steam's log. Fix (PR #14): only count log lines written after Steam starts.

### "That node is me"

- Run `20261025T181043Z`: on the floor 0 map, Haiku took the single starting node at the bottom for its own position marker. It clicked the row above, which can't be chosen yet, until the stuck stop ended the run: 13 steps, $0.0055.
- Fix (PR #15): the prompt says that at floor 0 the bottom node is the first choice, and that a click that does nothing means the node can't be reached yet.

### Six floors on Haiku

- Run `20261025T181542Z`: continued from the floor 0 map. It reached floor 6 and said DONE: 96 steps, $0.041, about $0.0004 a step, 21 minutes.
- Its first move was the bottom node, so the fix worked. It won 5 fights:
  - Floor 1: Nibbit, 80 → 68 HP. Took Setup Strike.
  - Floor 2: three slimes, 68 → 44 HP. Slimed status cards clogged the hand. Took Inflame.
  - Floor 3: beetle, 50 → 43 HP. Took a Fire Potion and Dismantle.
  - Floor 4: three Inklets, 49 → 24 HP. Took Shrug It Off.
  - Floor 6: Mawler, down to 12 HP (18 after healing). Took Thunderclap.
- Floor 5 was the merchant. It looped for 7 steps between the tent's Proceed and the shop's back arrow, bought nothing, and didn't notice it had hit its floor 5 goal.
- Weak play: it never used the Fire Potion, and it often ended turns with Defends still in hand, which is why HP dropped so far.

### Sonnet takes over at 18 HP

- Run `20261025T183740Z`: Sonnet 5.5 (thinking on, effort low) continued Haiku's run from the floor 6 rewards screen at 18/80 HP, aiming for floor 11. It died on floor 8: 46 steps, $0.29, 6.5 minutes.
- At the floor 7 merchant it had 163 gold and 18 HP. It bought Whirlwind and a potion and paid to remove a Strike, but didn't heal.
- It looped between the merchant tent and the shop for about 12 steps, just like Haiku.
- Floor 8 was three Raiders. It dropped to 1 HP on turn 1, died on step 33, and never drank a potion.
- After the death it clicked through screens nobody had seen yet (game over, Timeline, Epoch unlock, a story screen) and said DONE on the Timeline.
- Per step: Sonnet $0.0063 with 147 output tokens, Haiku $0.00043 with 337. Sonnet writes less, but each token costs about 20 times as much.
- Not a fair comparison: the two models played different floors from different HP. Next is a fresh run from floor 0 for each model.
- Problems both models share: leaving the shop, never using potions, and the screens after a run. The first two look like missing tutorial knowledge, not strategy.
- Replays exist for both runs (`replay.mp4` and `highlights.md` in each run folder): 2:22 for Haiku, 1:07 for Sonnet.

### A clean start on Sonnet, recorded

- Run `20261025T191243Z`: a fresh run with Sonnet 5.5 (thinking on, effort low) and screen recording on. It reached floor 5 at 63/70 HP and said DONE: 72 steps, $0.46, 13 minutes. It's the first run with both `screen.mp4` (12:59) and `replay.mp4` (1:48).
- It started on the Timeline screen left by the last death. It revealed the waiting Epoch, which unlocked the Silent, then went back to the menu.
- The game now preselects the Silent, so "default character" meant the Silent. Goals should name the character from now on.
- At the opening Ancient event it took Lead Paperweight, avoiding a curse, then Thrumming Hatchet.
- On the map it clicked the bottom node twice, but after an opening event that node is a cave and already done. Then it picked the node above it. The prompt is fixed to cover this (PR #21).
- Three fights, 7 HP lost in total:
  - Slimes: 0 HP lost. Took Noxious Fumes.
  - Nibbit: 2 HP lost. Took Deflect and a Flex Potion.
  - Shrinker Beetle: 5 HP lost. Took Bouncing Flask.
- Compared with Haiku on similar floors, which lost 12-19 HP per fight, Sonnet blocks and sequences far better. It costs $0.0064 a step against Haiku's $0.0004.
- At about 14 steps a floor, a full run (about 50 floors) on Sonnet would cost several dollars, more than the $2/day budget. Haiku could do one for well under $1.

## Clip ideas

- The 39-click loop on a visited node (`20261025T165758Z`).
- "That node is me" at floor 0 (`20261025T181043Z`).
- The first fight won (`20261025T164617Z`).
- Mawler taking it to 12 HP (`20261025T181542Z`).
- The merchant loop (`20261025T181542Z`, steps 69-75).
- Sonnet starting at 18 HP, spending its gold on a card instead of healing, then dying on floor 8 with two potions unused (`20261025T183740Z`).
- Both models getting lost leaving the merchant.
- Sonnet's clean floors 1-5 with 7 HP lost, against Haiku's 12-19 HP per fight (`20261025T191243Z`, with real-time footage).
- Haiku vs Sonnet on the same floors, with the cost per floor.
