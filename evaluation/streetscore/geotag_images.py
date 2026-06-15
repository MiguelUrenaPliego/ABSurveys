# coding: utf-8

"""
Geotag images using ordered positions + directions GeoPackages.

Pipeline:
1. Read JPG images
2. Sort by EXIF capture time (old → new)
3. Match with positions.gpkg and directions.gpkg
4. Compute heading (bearing)
5. Write clean EXIF GPS + heading

IMPORTANT:
We DO NOT reuse original EXIF to avoid piexif crashes on Android/OPPO images.
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
from typing import List, Optional
import math

import geopandas as gpd
from PIL import Image
import piexif


# =========================
# CONFIG
# =========================

IMAGE_FOLDER = "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/images/Anlagenring/FriedbergerTor"
POSITIONS_GPKG = IMAGE_FOLDER + "/positions.gpkg"
DIRECTIONS_GPKG = IMAGE_FOLDER + "/directions.gpkg"


# =========================
# EXIF TIME EXTRACTION
# =========================

def get_capture_time(image_path: str) -> Optional[datetime]:
    """
    Extract EXIF DateTimeOriginal from image.

    Args:
        image_path: Path to image.

    Returns:
        datetime or None if missing.
    """
    try:
        img = Image.open(image_path)
        exif = img._getexif()

        if not exif:
            return None

        for tag_id, value in exif.items():
            if tag_id == 36867:  # DateTimeOriginal
                return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")

        return None

    except Exception:
        return None


def sort_images_by_time(images: List[str]) -> List[str]:
    """
    Sort images by EXIF capture time (old → new).
    """

    def key(p: str):
        t = get_capture_time(p)
        return t if t is not None else datetime.max

    return sorted(images, key=key)


# =========================
# GEOMETRY
# =========================

def compute_bearing(lon1, lat1, lon2, lat2) -> float:
    """
    Compute compass bearing from point1 → point2.
    """

    lon1, lat1, lon2, lat2 = map(math.radians, [lon1, lat1, lon2, lat2])

    dlon = lon2 - lon1

    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (
        math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    )

    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


# =========================
# EXIF HELPERS
# =========================

def to_dms(value: float):
    """
    Convert decimal degrees to EXIF GPS rational format.
    """

    abs_val = abs(value)

    deg = int(abs_val)
    minutes_full = (abs_val - deg) * 60
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60

    return (
        (deg, 1),
        (minutes, 1),
        (int(seconds * 100), 100),
    )


# =========================
# WRITE EXIF (SAFE VERSION)
# =========================

def write_exif(image_path: str, lon: float, lat: float, heading: float):
    """
    Write GPS + heading EXIF safely (no reuse of corrupted metadata).
    """

    img = Image.open(image_path)

    # 🚨 DO NOT load existing EXIF (prevents piexif crashes on Android images)
    exif = {
        "0th": {},
        "Exif": {},
        "GPS": {},
        "1st": {},
        "thumbnail": None,
    }

    gps_ifd = {
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: to_dms(lat),

        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: to_dms(lon),

        # 🧭 camera heading
        piexif.GPSIFD.GPSImgDirectionRef: b"T",
        piexif.GPSIFD.GPSImgDirection: (int(heading * 100), 100),
    }

    exif["GPS"] = gps_ifd

    exif_bytes = piexif.dump(exif)

    img.save(image_path, "jpeg", exif=exif_bytes)


# =========================
# MAIN PIPELINE
# =========================

def main():
    """
    Main geotagging pipeline.
    """

    image_dir = Path(IMAGE_FOLDER)

    images = sorted(str(p) for p in image_dir.glob("*.jpg"))

    if not images:
        raise ValueError("No JPG images found.")

    print(f"Found {len(images)} images")

    # sort by time
    images = sort_images_by_time(images)
    print("Sorted by capture time (old → new)")

    pos = gpd.read_file(POSITIONS_GPKG)
    dirs = gpd.read_file(DIRECTIONS_GPKG)

    if len(pos) != len(images) or len(dirs) != len(images):
        raise ValueError(
            f"Mismatch: images={len(images)}, positions={len(pos)}, directions={len(dirs)}"
        )

    print("Loaded GeoPackages")

    for i, img_path in enumerate(images):

        p = pos.iloc[i].geometry
        d = dirs.iloc[i].geometry

        lon1, lat1 = p.x, p.y
        lon2, lat2 = d.x, d.y

        heading = compute_bearing(lon1, lat1, lon2, lat2)

        write_exif(img_path, lon1, lat1, heading)

        print(f"[{i}] {img_path} → heading {heading:.2f}°")

    print("DONE ✔ All images geotagged")


if __name__ == "__main__":
    main()