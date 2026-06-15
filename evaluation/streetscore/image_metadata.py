# coding: utf-8

"""
Utilities for converting image folders into GeoDataFrames.

Features:
- Recursive image discovery
- Robust EXIF GPS extraction
- Robust datetime extraction (returns pandas datetime)
- Robust heading extraction (multi-source fallback)
- Compatible with real-world Android/OPPO/realme images
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple, List, Any, Dict

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from PIL import Image, ExifTags
import piexif
import os


# =========================
# CONFIG
# =========================

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"
}


# =========================
# EXIF LOADERS
# =========================

def _load_exif_pillow(image_path: str) -> Dict[str, Any]:
    """
    Load EXIF using Pillow (partial but fast).
    """
    try:
        img = Image.open(image_path)
        exif = img._getexif()

        if not exif:
            return {}

        return {
            ExifTags.TAGS.get(k, k): v
            for k, v in exif.items()
        }
    except Exception:
        return {}


def _load_exif_piexif(image_path: str) -> Dict[str, Any]:
    """
    Load full EXIF using piexif.
    """
    try:
        return piexif.load(image_path)
    except Exception:
        return {}


# =========================
# GPS EXTRACTION
# =========================

def _extract_gps(image_path: str) -> Optional[Tuple[float, float]]:
    """
    Extract (lon, lat) from EXIF GPS metadata.
    """
    exif = _load_exif_pillow(image_path)

    gps_info = exif.get("GPSInfo")
    if not gps_info:
        return None

    gps_data = {
        ExifTags.GPSTAGS.get(k, k): v
        for k, v in gps_info.items()
    }

    def _to_deg(value):
        d, m, s = value
        return float(d) + float(m) / 60.0 + float(s) / 3600.0

    lat = gps_data.get("GPSLatitude")
    lon = gps_data.get("GPSLongitude")
    lat_ref = gps_data.get("GPSLatitudeRef")
    lon_ref = gps_data.get("GPSLongitudeRef")

    if not lat or not lon:
        return None

    lat = _to_deg(lat)
    lon = _to_deg(lon)

    if lat_ref != "N":
        lat = -lat
    if lon_ref != "E":
        lon = -lon

    return lon, lat


# =========================
# DATETIME EXTRACTION (ROBUST)
# =========================

def _extract_datetime(image_path: str) -> Optional[pd.Timestamp]:
    """
    Extract capture datetime and return pandas Timestamp.

    Order:
    1. piexif EXIF
    2. Pillow EXIF
    3. filesystem timestamp fallback
    """

    # 1. piexif
    try:
        exif = _load_exif_piexif(image_path)

        exif_ifd = exif.get("Exif", {})

        for tag in [
            piexif.ExifIFD.DateTimeOriginal,
            piexif.ExifIFD.DateTimeDigitized,
        ]:
            if tag in exif_ifd:
                val = exif_ifd[tag]
                if isinstance(val, bytes):
                    val = val.decode(errors="ignore")
                return pd.to_datetime(val, errors="coerce")

        zeroth = exif.get("0th", {})
        if piexif.ImageIFD.DateTime in zeroth:
            val = zeroth[piexif.ImageIFD.DateTime]
            if isinstance(val, bytes):
                val = val.decode(errors="ignore")
            return pd.to_datetime(val, errors="coerce")

    except Exception:
        pass

    # 2. Pillow
    try:
        img = Image.open(image_path)
        exif = img._getexif()

        if exif:
            exif_data = {
                ExifTags.TAGS.get(k, k): v
                for k, v in exif.items()
            }

            for key in ["DateTimeOriginal", "DateTime", "CreateDate"]:
                if key in exif_data:
                    return pd.to_datetime(exif_data[key], errors="coerce")

    except Exception:
        pass

    # 3. filesystem fallback
    try:
        ts = os.path.getmtime(image_path)
        return pd.to_datetime(ts, unit="s")
    except Exception:
        return pd.NaT


# =========================
# HEADING EXTRACTION
# =========================

def _rational(value):
    try:
        if isinstance(value, tuple) and len(value) == 2:
            n, d = value
            return float(n) / float(d) if d != 0 else None
        return float(value)
    except Exception:
        return None


def _extract_heading(image_path: str) -> Optional[float]:
    """
    Extract heading from EXIF using all known sources.
    """

    exif = _load_exif_piexif(image_path)
    gps = exif.get("GPS", {})

    for key in [
        piexif.GPSIFD.GPSImgDirection,
        getattr(piexif.GPSIFD, "GPSBearing", None),
        getattr(piexif.GPSIFD, "GPSDestBearing", None),
    ]:
        if key is not None and key in gps:
            return _rational(gps[key])

    return None


# =========================
# MAIN FUNCTION
# =========================

def images_gdf(
    folder_path: str,
    recursive: bool = True,
) -> gpd.GeoDataFrame:
    """
    Convert folder of images into GeoDataFrame.

    Returns columns:
        - path (absolute)
        - filename
        - datetime (pd.Timestamp)
        - heading
        - x (lon)
        - y (lat)
        - geometry (Point)
    """

    folder = Path(folder_path).resolve()

    if recursive:
        image_paths = [
            p for p in folder.rglob("*")
            if p.suffix.lower() in IMAGE_EXTENSIONS
        ]
    else:
        image_paths = [
            p for p in folder.iterdir()
            if p.suffix.lower() in IMAGE_EXTENSIONS
        ]

    rows: List[dict] = []

    for p in image_paths:

        path = str(p.resolve())
        filename = p.name

        gps = _extract_gps(path)
        dt = _extract_datetime(path)
        heading = _extract_heading(path)

        if gps is None:
            x, y = None, None
            geom = None
        else:
            x, y = gps
            geom = Point(x, y)

        rows.append({
            "path": path,
            "filename": filename,
            "datetime": dt,
            "heading": heading,
            "x": x,
            "y": y,
            "geometry": geom,
        })

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")

    return gdf