from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


VISUAL_HASH_MAX_DISTANCE = 12
VISUAL_MAE_MAX = 4.0
VISUAL_RMSE_MAX = 10.0
VISUAL_SAMPLE_SIZE = (64, 64)
DIFFERENCE_HASH_SIZE = (17, 16)
LOSSY_IMAGE_FORMATS = frozenset({"JPEG", "WEBP", "AVIF"})


@dataclass(frozen=True, slots=True)
class VisualFingerprint:
    width: int
    height: int
    format_name: str
    difference_hash: str
    rgb_sample: bytes


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


def visual_fingerprint(path: Path) -> VisualFingerprint:
    """Build a strict near-duplicate fingerprint that tolerates JPEG recompression."""
    with Image.open(path) as image:
        image.seek(0)
        format_name = (image.format or "").upper()
        normalized = ImageOps.exif_transpose(image).convert("RGB")
        width, height = normalized.size
        sample = normalized.resize(VISUAL_SAMPLE_SIZE, Image.Resampling.LANCZOS)
        grayscale = normalized.resize(DIFFERENCE_HASH_SIZE, Image.Resampling.LANCZOS).convert("L")
        pixels = list(grayscale.getdata())

    bits = 0
    for y in range(DIFFERENCE_HASH_SIZE[1]):
        offset = y * DIFFERENCE_HASH_SIZE[0]
        for x in range(DIFFERENCE_HASH_SIZE[0] - 1):
            bits = (bits << 1) | int(pixels[offset + x] > pixels[offset + x + 1])
    return VisualFingerprint(
        width,
        height,
        format_name,
        f"{bits:064x}",
        sample.tobytes(),
    )


def visually_equivalent(left: VisualFingerprint, right: VisualFingerprint) -> bool:
    if (left.width, left.height) != (right.width, right.height):
        return False
    if (
        left.format_name not in LOSSY_IMAGE_FORMATS
        or right.format_name not in LOSSY_IMAGE_FORMATS
    ):
        return False
    hash_distance = (
        int(left.difference_hash, 16) ^ int(right.difference_hash, 16)
    ).bit_count()
    if hash_distance > VISUAL_HASH_MAX_DISTANCE:
        return False

    absolute_sum = 0
    squared_sum = 0
    for left_value, right_value in zip(left.rgb_sample, right.rgb_sample, strict=True):
        difference = abs(left_value - right_value)
        absolute_sum += difference
        squared_sum += difference * difference
    sample_count = len(left.rgb_sample)
    mean_absolute_error = absolute_sum / sample_count
    root_mean_square_error = math.sqrt(squared_sum / sample_count)
    return (
        mean_absolute_error <= VISUAL_MAE_MAX
        and root_mean_square_error <= VISUAL_RMSE_MAX
    )
