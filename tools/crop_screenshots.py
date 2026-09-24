"""
crop_screenshots.py
====================================================================
One-off image-prep script (NOT part of the SIH-26027 DSS application).
Crops the raw VDU console screenshots supplied for the PPT so only the
meaningful console content is embedded (top browser chrome / login
fields / tab bar removed). Saves cropped PNGs into tools/ppt_assets/.
====================================================================
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

SRC_DIR = Path(
    r"C:\Users\JITESH\.cursor\projects\c-Users-JITESH-Downloads-PythonProject5\assets"
)
OUT_DIR = Path(r"C:\Users\JITESH\Downloads\PythonProject5\tools\ppt_assets")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# (source filename fragment, output name, top_crop_px, bottom_crop_px)
JOBS: tuple[tuple[str, str, int, int], ...] = (
    ("image-926c0819", "officer_console.png", 128, 0),
    ("image-22868a3a", "controller_console.png", 128, 0),
    ("image-1a2a243d", "weekly_plan.png", 128, 0),
    ("image-4b047e1c", "situation_map.png", 128, 0),
)


def find_source(fragment: str) -> Path:
    matches = list(SRC_DIR.glob(f"*{fragment}*"))
    if not matches:
        raise FileNotFoundError(f"No source image found for fragment {fragment!r}")
    return matches[0]


def main() -> None:
    for fragment, out_name, top_px, bottom_px in JOBS:
        src_path = find_source(fragment)
        with Image.open(src_path) as img:
            width, height = img.size
            box = (0, top_px, width, height - bottom_px)
            cropped = img.crop(box)
            out_path = OUT_DIR / out_name
            cropped.save(out_path)
            print(f"{src_path.name}: {img.size} -> crop {box} -> {cropped.size}  saved {out_path}")


if __name__ == "__main__":
    main()
