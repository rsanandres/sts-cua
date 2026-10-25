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
- Next: the same floors on Sonnet 5.5, for comparison.

## Clip ideas

- The 39-click loop on a visited node (`20261025T165758Z`).
- "That node is me" at floor 0 (`20261025T181043Z`).
- The first fight won (`20261025T164617Z`).
- Mawler taking it to 12 HP (`20261025T181542Z`).
- The merchant loop (`20261025T181542Z`, steps 69-75).
- Haiku vs Sonnet on the same floors, with the cost per floor.
