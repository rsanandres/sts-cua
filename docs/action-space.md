# Action space

Everything the agent can press in Slay the Spire 2, grouped by screen. Draft: rows marked **confirmed** were seen on Raph's machine (game v0.107.1, build 23811903); everything else is carried over from Spire 1 or general UI sense and needs checking against real screenshots. Coordinates are for a 1280x720 window.

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

## Main menu (confirmed)

| Element | Where (1280x720) | Kind |
|---|---|---|
| Profile name, "Click to edit" | top-left | Navigation (don't use; the agent stays on Profile 3) |
| Singleplayer | ~517,505 | Commit |
| Multiplayer | | Off limits |
| Settings | | Off limits during runs |
| Quit | ~517,605, then Yes ~750,482 | Harness only |

## Character select and run start

| Element | Kind | Status |
|---|---|---|
| Character portrait | Commit | unverified |
| Ascension level up/down | Commit | unverified; fix it per experiment |
| Embark / start | Commit | unverified |
| Opening choice (Spire 1 had Neow; Spire 2's save data mentions "ancients") | Commit | unverified |

## Map

| Element | Kind | Status |
|---|---|---|
| Next reachable node (fight, elite, rest, shop, event, treasure, boss) | Commit | unverified |
| Scroll the map up and down to see further acts | Inspect | unverified |
| Legend or hover a node type | Inspect | unverified |

## Combat (most steps happen here)

| Input | Effect | Kind | Status |
|---|---|---|---|
| `1`-`9`, `0` | Select the card in that hand position (number shown above the card when "Show Hand Card Count" is on) | Commit, part 1 | confirmed (keybind and setting) |
| Click an enemy after selecting a targeted card | Play it on that enemy | Commit | unverified |
| Click empty play area, or select again | Play an untargeted card | Commit | unverified |
| `Down` | Release the selected card back to hand | Undo selection | confirmed (keybind) |
| `e` | End turn | Commit | confirmed (keybind "confirm"; verify it ends the turn in combat) |
| Hover an enemy | Intent (attack value, buff, debuff) and powers | Inspect | unverified; the intent icon may already show on screen |
| `a` | Draw pile: what's left to draw (Spire 1 showed it unordered) | Inspect | confirmed (keybind); order unverified |
| `s` | Discard pile | Inspect | confirmed (keybind) |
| `x` | Exhaust pile | Inspect | confirmed (keybind) |
| `Space` | Peek (keybind name; likely shows the board without the hand) | Inspect | confirmed (keybind); effect unverified |
| Click a potion slot, then Use / Discard, then a target | Use or drop a potion | Commit | unverified |

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
