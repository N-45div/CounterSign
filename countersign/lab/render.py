"""Draw what the lab's doorbell camera sees.

Plain shapes, not photorealism: a wall, a door, a mat, and whatever the scene
holds (a van at the kerb, a person at the door, a parcel with a shipping
label, a note stuck to the door). Text is real text, so OCR and the witness
read it exactly as they would on a camera frame. Rendering is deterministic:
the same scene and timestamp always give the same bytes, so evidence hashes
can be checked.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .scenarios import Scene

W, H = 1280, 720


@lru_cache(maxsize=8)
def _font(size: int):
    return ImageFont.load_default(size=size)


def _van(d: ImageDraw.ImageDraw) -> None:
    d.rounded_rectangle([20, 300, 330, 520], radius=18, fill=(236, 238, 240), outline=(120, 124, 130), width=4)
    d.rectangle([240, 320, 320, 410], fill=(150, 180, 205))  # windscreen
    for x in (80, 250):
        d.ellipse([x, 490, x + 60, 550], fill=(40, 40, 44))
        d.ellipse([x + 18, 508, x + 42, 532], fill=(150, 150, 150))
    d.text((50, 420), "PARCELS", fill=(60, 90, 140), font=_font(30))


def _person(d: ImageDraw.ImageDraw) -> None:
    d.ellipse([838, 250, 898, 310], fill=(150, 110, 85))  # head
    d.rounded_rectangle([820, 310, 918, 470], radius=22, fill=(50, 90, 150))  # torso, courier jacket
    d.rectangle([832, 470, 866, 580], fill=(40, 40, 52))  # legs
    d.rectangle([872, 470, 906, 580], fill=(40, 40, 52))


def _parcel(d: ImageDraw.ImageDraw) -> None:
    d.rectangle([540, 455, 760, 585], fill=(196, 152, 98), outline=(120, 85, 45), width=4)
    d.rectangle([540, 510, 760, 526], fill=(170, 130, 80))  # tape
    d.rectangle([560, 465, 742, 505], fill=(250, 250, 245))  # label
    d.text((566, 468), "Deliver to: A. Sharma", fill=(20, 20, 20), font=_font(16))
    d.text((566, 486), "Flat 402   FRAGILE", fill=(20, 20, 20), font=_font(16))


def _note(d: ImageDraw.ImageDraw, lines: tuple[str, ...]) -> None:
    width = max([270] + [int(_font(24).getlength(line)) + 26 for line in lines])  # the sticky fits its text
    d.rectangle([520, 170, 520 + width, 175 + 34 * len(lines)], fill=(255, 238, 120))
    for i, line in enumerate(lines):
        d.text((532, 180 + i * 32), line, fill=(10, 10, 10), font=_font(24))


def render(scene: Scene, at_ms: int, device_label: str = "Lab Doorbell") -> bytes:
    """A JPEG of the doorstep at one moment."""
    img = Image.new("RGB", (W, H), (226, 214, 190))  # wall
    d = ImageDraw.Draw(img)
    d.rectangle([0, 590, W, H], fill=(150, 150, 150))  # path
    d.rectangle([470, 90, 810, 590], fill=(110, 70, 40), outline=(60, 35, 20), width=6)  # door
    d.ellipse([760, 330, 785, 355], fill=(200, 170, 60))  # knob
    d.rectangle([430, 590, 850, 640], fill=(70, 55, 45))  # mat
    d.ellipse([1040, 470, 1180, 590], fill=(70, 120, 60))  # plant
    d.rectangle([1080, 560, 1140, 620], fill=(150, 80, 50))  # pot
    if scene.vehicle:
        _van(d)
    if scene.note:
        _note(d, scene.note)
    if scene.parcel:
        _parcel(d)
    if scene.person:
        _person(d)
    stamp = datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    d.rectangle([0, 0, W, 34], fill=(20, 20, 24))
    d.text((12, 6), f"Countersign Lab  |  {device_label}", fill=(235, 235, 235), font=_font(20))
    d.text((W - 300, 6), stamp, fill=(235, 235, 235), font=_font(20))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()
