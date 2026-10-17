#!/usr/bin/env python3
"""Combine the README screenshots into animated PNG slideshows (light and dark).

GitHub READMEs cannot run JavaScript, so a click-through gallery is not
possible; an animated PNG shows one screenshot at a time instead and keeps
full colour quality (unlike GIF). Requires Pillow.

Usage: build-aux/make-slideshow.py
"""

from pathlib import Path

from PIL import Image

SHOTS = ["app_list", "install_compare_packages", "risk"]  # slideshow order
SECONDS_PER_SLIDE = 4

root = Path(__file__).resolve().parent.parent / "screenshots"
for theme in ("light", "dark"):
    frames = [Image.open(root / f"{name}_{theme}.png").convert("RGBA") for name in SHOTS]
    out = root / f"slideshow_{theme}.png"
    frames[0].save(out, save_all=True, append_images=frames[1:], loop=0,
                   duration=SECONDS_PER_SLIDE * 1000,
                   disposal=1,  # clear before the next frame (screenshots have transparent shadows)
                   blend=0)     # replace instead of blending onto the previous frame
    print(f"wrote {out} ({out.stat().st_size // 1024} kB)")
