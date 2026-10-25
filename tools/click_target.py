"""A click-accuracy target for the first live test, before Spire is involved.

Shows a green button that jumps to a new random spot after every hit, and writes
hits and misses to a JSON file after each click, so a run can be scored exactly.

    DISPLAY=:99 python tools/click_target.py --out target.json &
"""

from __future__ import annotations

import argparse
import json
import os
import random

os.environ.setdefault("SDL_VIDEO_WINDOW_POS", "0,0")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402

BUTTON = (180, 70)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="target.json")
    parser.add_argument("--size", default="1280x720")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    width, height = (int(v) for v in args.size.split("x"))
    rng = random.Random(args.seed)
    pygame.display.init()
    pygame.font.init()
    screen = pygame.display.set_mode((width, height), pygame.NOFRAME)
    font = pygame.font.Font(None, 40)

    def new_button() -> pygame.Rect:
        x = rng.randint(20, width - BUTTON[0] - 20)
        y = rng.randint(80, height - BUTTON[1] - 20)
        return pygame.Rect(x, y, *BUTTON)

    button = new_button()
    score = {"hits": 0, "misses": 0, "button": [button.x, button.y, *BUTTON], "clicks": []}

    def save() -> None:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(score, f, indent=2)

    save()
    clock = pygame.time.Clock()
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                hit = button.collidepoint(event.pos)
                score["clicks"].append({"pos": list(event.pos), "button": [button.x, button.y, *BUTTON], "hit": hit})
                if hit:
                    score["hits"] += 1
                    button = new_button()
                    score["button"] = [button.x, button.y, *BUTTON]
                else:
                    score["misses"] += 1
                save()

        screen.fill((24, 24, 32))
        status = font.render(f"Hits: {score['hits']}   Misses: {score['misses']}", True, (230, 230, 230))
        screen.blit(status, (20, 20))
        pygame.draw.rect(screen, (40, 170, 70), button, border_radius=10)
        label = font.render("CLICK ME", True, (255, 255, 255))
        screen.blit(label, label.get_rect(center=button.center))
        pygame.display.flip()
        clock.tick(30)


if __name__ == "__main__":
    main()
