# Action space

Everything the agent can press in Slay the Spire 2, grouped by screen. Draft: rows marked **confirmed** were seen on Raph's machine (game v0.107.1, build 23811903); everything else is a best guess from general UI sense, not yet seen in Spire 2, and needs checking against real screenshots. Coordinates are for a 1280x720 window.

## Two kinds of action

| Kind | What it does | Examples | Why it matters |
|---|---|---|---|
| **Commit** | Changes the game. Can't be undone. | Play a card, end turn, pick a reward, choose a map node, buy, rest | This is the actual play |
| **Inspect** | Shows information, changes nothing. Costs a step. | Open draw or discard pile, view deck, open map, hover an enemy's intent or a relic | The research question: does a reasoning model inspect more, and does that buy wins? |

Every step in the run log gets one of these labels, so we can count inspects per run, and inspects before a mistake.

## Global (most screens)

| Input | Effect | Kind | Status |
|---|---|---|---|
| `Escape` | Cancel, back, or open the pause menu | Inspect / navigation | confirmed (keybind) |
| `m` | Open or close the map | Inspect | confirmed (keybind) |
| `d` | View the whole deck | Inspect | confirmed (keybind) |
| `Return` | Select the highlighted item | Commit | confirmed (keybind) |
| Arrow keys | Move the highlight in menus | Navigation | confirmed (keybind) |
| Hover a relic, potion, power or keyword | Tooltip | Inspect | unverified |
| `wait` | Let an animation finish | None | harness action |
| Reply `DONE` with no action | Tell the harness the goal is reached; the run stops | None | harness action |

## Main menu (confirmed)

| Element | Where (1280x720) | Kind |
|---|---|---|
| Profile name, "Click to edit" | top-left | Navigation (don't use; the agent stays on Profile 3) |
| Singleplayer | ~517,505 | Commit |
| Multiplayer | | Off limits in solo runs; co-op is below |
| Settings | | Off limits during runs |
| Quit | ~517,605, then Yes ~750,482 | Harness only |

## Character select and run start

Seen in run 20261025T085110Z (Haiku, Profile 3). On a fresh profile, Singleplayer goes straight to character select with no mode submenu.

| Element | Where (1280x720) | Kind | Status |
|---|---|---|---|
| The Ironclad (default, highlighted): 80 HP, 99 gold, Burning Blood | | Inspect | confirmed |
| Other four characters | | Locked on a fresh profile | confirmed |
| Confirm checkmark | ~1225,518 | Commit | confirmed |
| "Enable Tutorials?" dialog (first run on a profile): No / Yes | ~528,482 / ~752,482 | Commit | confirmed; the agent picks No |
| Ascension level | | Commit | not yet seen (locked on a fresh profile?) |
| Opening choice, if the run starts with one (Spire 2's save data mentions "ancients") | | Commit | not yet seen |

## Map

Act 1 is "Overgrowth". The top bar shows HP, gold, 3 potion slots, floor number, a run timer and deck size (10 cards at the start). A Legend panel sits on the right.

| Element | Kind | Status |
|---|---|---|
| First node: a single large, pulsing fight node at bottom centre (~605,512); the run is on floor 0 until it's clicked | Commit | confirmed. Haiku misread it as "where I am standing" and clicked unreachable row-2 nodes until the stuck stop (run 20261025T181043Z); the prompt now names it |
| Next reachable node (fight, elite, rest, shop, event, treasure, boss): directly **above** the player marker; paths go upward | Commit | confirmed (run 20261025T165758Z) |
| Hover a node below the marker (already visited) | Tooltip with that floor's history, e.g. "Floor 2… Rewards: 17 Gold, Inflame; Skipped: Breakthrough, Anger" | Inspect | confirmed. Haiku clicked a visited node 39 times thinking it was the next choice |
| Scroll the map up and down to see further acts | Inspect | unverified |
| Legend or hover a node type | Inspect | unverified |

## Combat (most steps happen here)

Seen in run 20261025T090109Z (Haiku vs Nibbit, first fight of Act 1).

### Layout (1280x720)

| Element | Where | Notes |
|---|---|---|
| Hand | fan along the bottom, card centres y≈640–660; 5 cards at x≈390, 515, 640, 760, 880 | |
| Card numbers | above each card, y≈555–595 | renumber left to right after every play |
| Energy | red orb bottom-left ≈(108,593), "3/3" | |
| Player | body ≈(320,400), HP bar ≈(320,503) | |
| Enemy (one) | body ≈(950,450), HP ≈(960,503), name below | |
| Enemy intent | above the enemy ≈(950,378): sword + damage, shield when blocking | always on screen, no hover needed |
| End Turn button | ≈(1142,595), "End Turn N" | |
| Draw pile / discard pile | ≈(35,685) / ≈(1245,685), with count badges | |
| Potion slots | top bar ≈(355,25), (395,25), (437,25) | |
| Top bar | HP ≈(150,25), gold ≈(250,25), floor ≈(550,25), map ≈(1135,25), deck ≈(1190,25), settings ≈(1242,25) | "Game Saved" toast ≈(1030,25) |

### Inputs

| Input | Effect | Kind | Status |
|---|---|---|---|
| `1`-`9`, `0` | Select the card in that slot. **Pressing the same number again deselects it.** | Commit, part 1 | confirmed |
| Click the enemy body with a targeted card selected | Play it on that enemy | Commit | confirmed, **only if the pointer was already hovering the enemy**. The harness now moves, waits 0.25 s, then clicks |
| Click empty space above the hand ≈(640,300) with a non-targeted card selected (Defend) | Play it | Commit | confirmed, with a **slow click** (hold 0.2 s). Instant clicks, `Return`, `e`, clicking the player, and dragging the card do not play it. The harness now holds every click 0.2 s |
| `e` with no card selected | End turn | Commit | confirmed |
| `e` with a card selected | Nothing (doesn't confirm, doesn't end the turn) | None | confirmed |
| `Escape` in combat | Cancels the selection and opens the Pause menu (Resume, Settings, Give Up, Save and Quit) | Navigation | confirmed; avoid. "Give Up" ends the run |
| Hover the enemy | Tooltip, e.g. "Aggressive: This enemy intends to Attack for 12 damage" ≈(745,433) | Inspect | confirmed |
| `Down` | Release the selected card back to hand | Undo selection | keybind confirmed, effect untested |
| `a` / `s` / `x` | Draw / discard / exhaust pile | Inspect | keybinds confirmed, views not yet seen |
| `Space` | Peek | Inspect | keybind confirmed, effect not yet seen |
| Potion slot, then Use / Discard, then target | Use or drop a potion | Commit | not yet seen |

### Known failure modes (first fight)

- Pressing a card number three times to "play three cards": the presses toggle one card on and off.
- Missing the selected-card state: a selected card is drawn large mid-screen and can cover the enemy, which the model read as "the enemy is gone".

## Mod state types seen so far

`GET /api/v1/singleplayer` `state_type` values met in runs: `menu`, `map`, `monster` (combat), `rewards`, `card_reward`, `event`. The run log keeps a summary per step.

## Co-op

The agent joins Raph's lobby as a client (`python -m sts_cua --coop`, see the README). Nothing in this section has been seen on Raph's machine yet, so every row is **unverified** unless it says otherwise. Mod facts are read in STS2MCP main @ 55e0648 (`McpMod.StateBuilder.cs`, `McpMod.MultiplayerState.cs`, `docs/raw-full.md`); rules facts are from third-party guides ([allthings.how](https://allthings.how/slay-the-spire-2-co-op-multiplayer-every-mechanic-and-rule-explained/), [brokenbuilds.gg](https://brokenbuilds.gg/slay-the-spire-2/guides/slay-the-spire-2-co-op-guide)), which disagree in places.

### Mod endpoints

- Lobby and menus come from `GET /api/v1/singleplayer`: no run is in progress yet. During a co-op run that endpoint answers 409 and `GET /api/v1/multiplayer` answers instead (and 409 outside a run). `coop.fetch_state` tries multiplayer, then singleplayer.
- The harness only reads. The mod's multiplayer POST actions (`end_turn`, `undo_end_turn`, `menu_select join_<id>`, ...) are never called.

### Lobby

| Element | Kind | Mod fields | Status |
|---|---|---|---|
| Main menu > Multiplayer: Host / Join / Load / Abandon, Back | Navigation | `menu_screen` "multiplayer"; "multiplayer_host" for Standard / Daily / Custom | mod source |
| Join: a list of Steam friends with open lobbies, Refresh, Back | Commit (joins) | `menu_screen` "multiplayer_join", `friends` [{name, player_id}] | mod source; the agent doesn't use it yet (Steam invite instead) |
| Character select with the lobby: pick a character, confirm (embark) to ready up | Commit | `lobby` {type client, is_local_ready, all_ready, players [{is_local, character_id, is_ready}]} | mod source. Confirm is ~1225,518 in solo (confirmed); same screen class in co-op, position unverified |
| Unready: takes back ready | Commit | option "unready" | mod source |
| Back: **leaves the lobby** | Off limits | option "back" | mod source |
| Invite button (host side) | Raph only | | patch v0.100.0 mentions it |
| Load lobby (resume a saved co-op run): confirm to ready, players show connected and ready | Commit | `menu_screen` "multiplayer_load_lobby" | mod source |

### Combat

| Element | Kind | Mod fields | Status |
|---|---|---|---|
| Everyone plays at the same time with their own energy and hand; the turn passes once every player pressed End Turn | | `battle.all_players_ready`, `players[].is_ready_to_end_turn` | guides + mod source |
| `e` / End Turn after your turn: you are ready and wait | Commit | local `is_ready_to_end_turn` true | `e` confirmed in solo; the waiting look on screen is unverified |
| Pressing End Turn again while waiting | Avoid | the mod has `undo_end_turn`, so the game can take an end turn back | the in-game input for it is unverified |
| Enemy turn | Wait | `battle.turn` != "player" or `is_play_phase` false | mod source |
| A player at 0 HP is knocked out for the rest of the fight and comes back at 1 HP after it | Wait | `players[].is_alive` | guide |

### Map vote

| Element | Kind | Mod fields | Status |
|---|---|---|---|
| Every player clicks a reachable node to vote; the game then picks the path (a roulette weighted by votes per one guide, random when split per another) | Commit | `map.votes` [{is_local, voted, vote_col, vote_row}], `all_voted`, `next_options` (sorted by column) | mod source; tie behaviour unverified |
| What a teammate's vote looks like on screen (a marker or portrait on the node) | Inspect | | unverified: confirm on the PC so the model can see it without the mod |
| Map drawings by any player (arrows, scribbles) stay for the act | Inspect | | guide; patch v0.100.0 can turn them off |

The agent votes last and follows the majority of the other players' votes (`sts_cua.rules`); Raph votes first.

### Per-player and shared screens

| Screen | Shared? | Mod fields | Status |
|---|---|---|---|
| Card rewards, gold, potions | Per player | `rewards.items` | guides |
| Shop | Per player (own stock and gold) | | guides |
| Rest site | Per player; Mend heals a teammate | | guides |
| Event | Usually per player; some are shared votes | `event.is_shared`, `event.votes`, `all_voted` | mod source |
| Treasure | One relic each; a contested relic is settled by an automatic duel | `treasure.is_bidding_phase`, `bids`, `all_bid` | guide + mod source |
| Game over | | no explicit check in the multiplayer state; read as `overlay` "NGameOverScreen" (inferred) | unverified |

### Waiting states

`coop.waiting(state)` names why waiting is normal; the loop then doesn't count wait-only steps as idle or unchanged screens as stuck, and the `wait_turn` rule waits without a model call, re-reading the mod every 0.5 s.

| Reason | When |
|---|---|
| `lobby_ready`, `lobby_starting` | ready in the lobby; everyone ready |
| `teammate_end_turn`, `turn_ending` | the agent ended its turn; every player has |
| `enemy_turn` | not the players' play phase |
| `knocked_out` | the agent's character is down this fight |
| `teammates_voting` | a vote (map, shared event, treasure) that not every other player has cast yet |
| `map_vote`, `event_vote`, `treasure_bid` | the agent has voted |
| `teammate_maybe` | rewards, shop, rest site, private event, popups: the mod can't tell, so the run never ends there for idling (the model still acts) |
| `no_state` | the mod is unreachable |

## Rewards (after combat)

| Element | Kind | Status |
|---|---|---|
| Gold, potion, relic rows | Commit | unverified |
| Card reward: pick one of N, or Skip | Commit | unverified |
| Proceed | Commit | unverified |

## Rest site, shop, event, treasure

| Screen | Choices | Status |
|---|---|---|
| Rest site | Rest (heal), Smith (upgrade a card, then pick the card), other options from relics | unverified |
| Shop | Buy a card, relic or potion; card removal; Leave | unverified |
| Event | Pick one of the listed options; Proceed | unverified |
| Treasure | Open the chest, take the relic, Proceed | unverified |

## End of run

| Element | Kind | Status |
|---|---|---|
| Victory or death screen, score breakdown | Inspect | unverified |
| Continue to main menu | Commit (harness) | unverified |

## How we fill in the unverified rows

1. During the first Haiku runs, save a screenshot of every new screen type (the run log already keeps one per step).
2. For each screen, record the elements, their positions at 1280x720, and the shortcut if there is one.
3. Once the state-export mod exists, it gives the screen type and the legal actions directly. This table then becomes the prompt text the agent sees per screen, and the labels in the run log.
