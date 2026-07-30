from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps


def decoded_pixel_sha256(path: Path) -> str:
    """Return an exact fingerprint of the first, EXIF-oriented RGBA frame."""
    with Image.open(path) as image:
        image.seek(0)
        normalized = ImageOps.exif_transpose(image).convert("RGBA")
        width, height = normalized.size
        digest = hashlib.sha256()
        digest.update(f"{width}x{height}:RGBA\0".encode("ascii"))
        digest.update(normalized.tobytes())
        return digest.hexdigest()
