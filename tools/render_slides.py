"""
render_slides.py
====================================================================
Renders every slide of a .pptx to a PNG using PowerPoint COM
automation, for visual QA. Not part of the SIH-26027 DSS application.

Run: .venv\\Scripts\\python.exe tools\\render_slides.py
====================================================================
"""
import sys
import time
from pathlib import Path

import win32com.client

SRC = Path(r"c:\Users\JITESH\Downloads\SIH26027_Bug_Hunters_2.pptx")
OUT_DIR = Path(r"C:\Users\JITESH\Downloads\PythonProject5\tools\render_out")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for f in OUT_DIR.glob("*.png"):
        f.unlink()

    powerpoint = win32com.client.Dispatch("PowerPoint.Application")
    powerpoint.Visible = True
    time.sleep(1)
    presentation = powerpoint.Presentations.Open(str(SRC), WithWindow=False)
    try:
        for i, slide in enumerate(presentation.Slides, start=1):
            out_path = OUT_DIR / f"slide_{i}.png"
            slide.Export(str(out_path), "PNG", 1920, 1080)
            print(f"Exported {out_path}")
    finally:
        presentation.Close()
        powerpoint.Quit()


if __name__ == "__main__":
    main()
