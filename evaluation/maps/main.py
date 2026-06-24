# coding: utf-8
"""
main.py

End-to-end pipeline to configure, load, clean, normalize, and render multi-model
street perception data on an interactive Leaflet map. Handles both real-world CSV
inputs and robust fallback simulation for testing without data files.
"""

from __future__ import annotations
import os
import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Union

# Import our customized map generation engine
from map import generate_custom_html_map
from utils import (
    _all_files_exist,
    normalize_and_align_distributions,
    generate_simulation_data
)

# ============================================================================
# CONFIGURATION — Edit freely to point to your survey and model datasets
# ============================================================================

# Absolute root of the project. All relative paths below are resolved against
# this directory at runtime. The generated map.html is also saved here.
ROOT_PATH: str = "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/ABsurveys"

# Image index CSV(s) — paths inside the CSV are relative to the CSV's own
# parent directory and will be re-expressed as ROOT_PATH-relative at load time.
# Can be a single str or a list of files (one per scenario / tile layer).
IMG_PATHS: Union[str, list[str]] = "images/Anlagenring/images.csv"

# Human survey data — maps user clicks / A-B choices to image pairs.
HUMAN_DF_PATHS: Union[str, list[str]] = "user_data/Anlagenring_user_data_merged.csv"

# TrueSkill score outcomes derived from human surveys.
# Expected columns: img_id, img_type, scenario,
#   score_<question_id>, uncertainty_<question_id>, n_answers_<question_id>
TRUESKILL_DF_PATHS: Union[str, list[str], None] = "user_data/Anlagenring_user_images_merged.csv"

# StreetScore predictions from the ML model.
# Expected columns: img_id, img_type, scenario,
#   <metric>, entropy_<metric>, uncertainty_mc_<metric>
# NOTE: any path columns in this file (path, abs_path, _base_dir …) are
#       intentionally ignored — image paths always come from images.csv.
STREETSCORE_DF_PATHS: Union[str, list[str], None] = "evaluation/streetscore/models/FrankfurtAnlagenring/scores.csv"

# Optional SQLite database (.swm2) with photo GPS coordinates and camera bearing.
# When present, lat/lon/bearing are merged into the image index by filename.
SWM2_DATABASE_PATH: Union[str, list[str], None] = "images/Anlagenring/database.swm2"

# --- Metrics mapping ---------------------------------------------------------
# Each entry links a StreetScore column, a TrueSkill question_id, an img_type
# filter, and an optional scenario filter.
METRICS_MAP = [
    {
        "streetscore_metric": "walk",
        "question_id": "walk-preference",
        "img_type": "walk",
        "scenario": "Anlagenring"
    },
    {
        "streetscore_metric": "bike",
        "question_id": "bike-preference",
        "img_type": "bike",
        "scenario": "Anlagenring"
    },
    {
        "streetscore_metric": "stay",
        "question_id": "stay-preference",
        "img_type": "stay",
        "scenario": "Anlagenring"
    }
]

# ============================================================================
# HELPERS
# ============================================================================

def _abs(relative_path: str) -> str:
    """Resolve a ROOT_PATH-relative config path to an absolute path."""
    return os.path.join(ROOT_PATH, relative_path)


def _to_abs_list(paths: Union[str, list[str], None]) -> list[str] | None:
    """Normalise a path or list of paths to a list of absolute paths."""
    if paths is None:
        return None
    if isinstance(paths, str):
        return [_abs(paths)]
    return [_abs(p) for p in paths]


# ============================================================================
# MAIN DATA PIPELINE
# ============================================================================

def load_and_compile_perceptions() -> tuple[list[dict], int, int]:
    """
    Loads raw tables, merges coordinates, matches image assets, resolves and
    scales uncertainty parameters, executes dual-distribution normalisation,
    and returns compiled data ready for Leaflet dashboard rendering.

    Image paths in the output are always relative to ROOT_PATH so that the
    generated map.html can serve images with simple relative URLs.
    """
    print("[Pipeline] Starting data ingestion and cleaning...")

    # Resolve all config paths to absolute so the rest of the code is
    # independent of the working directory.
    abs_img_paths      = _to_abs_list(IMG_PATHS)
    abs_human_paths    = _to_abs_list(HUMAN_DF_PATHS)
    abs_trueskill_paths = _to_abs_list(TRUESKILL_DF_PATHS)
    abs_streetscore_paths = _to_abs_list(STREETSCORE_DF_PATHS)

    files_exist = (
        _all_files_exist(abs_img_paths) and
        _all_files_exist(abs_human_paths) and
        _all_files_exist(abs_trueskill_paths) and
        _all_files_exist(abs_streetscore_paths)
    )

    if not files_exist:
        print("[Pipeline] Data files not fully found. Activating Realistic Simulation Mode!")
        return generate_simulation_data()

    # -------------------------------------------------------------------------
    # 1. IMAGE INDEX — coordinates & ROOT_PATH-relative image paths
    # -------------------------------------------------------------------------
    # Normalise SWM2 paths to match img_paths length.
    if SWM2_DATABASE_PATH is None:
        swm2_abs_list = [None] * len(abs_img_paths)
    elif isinstance(SWM2_DATABASE_PATH, str):
        swm2_abs_list = [_abs(SWM2_DATABASE_PATH)] * len(abs_img_paths)
    else:
        swm2_abs_list = [_abs(p) for p in SWM2_DATABASE_PATH]

    # Pad with None if fewer SWM2 entries than image CSVs.
    if len(swm2_abs_list) < len(abs_img_paths):
        swm2_abs_list += [None] * (len(abs_img_paths) - len(swm2_abs_list))

    img_dfs = []
    for img_csv_abs, swm2_db in zip(abs_img_paths, swm2_abs_list):
        print(f"[Pipeline] Loading image index from: {img_csv_abs}")
        df = pd.read_csv(img_csv_abs)

        # ------------------------------------------------------------------
        # Re-express image paths:
        #   CSV paths  →  relative to the CSV's parent dir
        #              →  absolute
        #              →  relative to ROOT_PATH   (used in map.html URLs)
        # ------------------------------------------------------------------
        csv_dir = os.path.dirname(os.path.abspath(img_csv_abs))
        root_abs = os.path.abspath(ROOT_PATH)

        def to_root_relative(row_path: str) -> str:
            if pd.isna(row_path):
                return row_path
            # path column is relative to the CSV's directory
            full = os.path.normpath(os.path.join(csv_dir, str(row_path)))
            try:
                return os.path.relpath(full, root_abs)
            except ValueError:
                # On Windows, relpath can fail across drives; fall back to absolute.
                return full

        if "path" in df.columns:
            df["path"] = df["path"].apply(to_root_relative)

        # ------------------------------------------------------------------
        # Optional: merge GPS coordinates from SWM2 SQLite database.
        # ------------------------------------------------------------------
        if swm2_db is not None and os.path.exists(swm2_db):
            print(f"[Pipeline] Merging coordinates from SWM2: {swm2_db}")
            try:
                conn = sqlite3.connect(swm2_db)
                db_meta = pd.read_sql_query(
                    """
                    SELECT photo_path, lon AS x, lat AS y, bearing
                    FROM photos p JOIN points pt ON p.uuid = pt.fid
                    """,
                    conn,
                )
                conn.close()

                db_meta["filename"] = db_meta["photo_path"].apply(os.path.basename)
                df["filename"] = df["path"].apply(os.path.basename)

                df = df.merge(
                    db_meta[["filename", "x", "y", "bearing"]],
                    on="filename", how="left", suffixes=("", "_db")
                )
                for col in ["x", "y", "bearing"]:
                    db_col = f"{col}_db"
                    if db_col in df.columns:
                        df[col] = df[db_col].combine_first(df.get(col, pd.Series(dtype=float)))
                        df.drop(columns=[db_col], inplace=True)
            except Exception as e:
                print(f"[Pipeline] SWM2 join failed for {swm2_db}: {e}")

        img_dfs.append(df)

    img_df = pd.concat(img_dfs, ignore_index=True)

    # Normalise coordinate column names.
    if "lon" in img_df.columns:
        img_df.rename(columns={"lon": "x"}, inplace=True)
    if "lat" in img_df.columns:
        img_df.rename(columns={"lat": "y"}, inplace=True)

    # Fill in fallback coordinates / bearing if the SWM2 had no data.
    if "x" not in img_df.columns:
        img_df["x"] = 8.6821 + np.random.normal(0, 0.002, len(img_df))
    if "y" not in img_df.columns:
        img_df["y"] = 50.1109 + np.random.normal(0, 0.002, len(img_df))
    if "bearing" not in img_df.columns:
        img_df["bearing"] = None

    # -------------------------------------------------------------------------
    # 2. HUMAN SURVEY DATA — click counts and unique respondents
    # -------------------------------------------------------------------------
    print(f"[Pipeline] Loading human clicks from: {abs_human_paths}")
    human_df = pd.concat(
        [pd.read_csv(p) for p in abs_human_paths], ignore_index=True
    )

    unique_users = int(human_df["user_id"].nunique()) if "user_id" in human_df.columns else 0
    if "type" in human_df.columns:
        total_clicks = int((human_df["type"] == "AB").sum())
    else:
        total_clicks = len(human_df)

    # -------------------------------------------------------------------------
    # 3. TRUESKILL SCORES — from human A/B survey outcomes
    # -------------------------------------------------------------------------
    if abs_trueskill_paths is not None:
        print(f"[Pipeline] Loading TrueSkill outcomes from: {abs_trueskill_paths}")
        trueskill_df = pd.concat(
            [pd.read_csv(p) for p in abs_trueskill_paths], ignore_index=True
        )
    else:
        print("[Pipeline] TrueSkill outcomes omitted (None).")
        trueskill_df = pd.DataFrame()

    # -------------------------------------------------------------------------
    # 4. STREETSCORE PREDICTIONS — from ML model
    #    Path columns (_base_dir, abs_path, path) are intentionally ignored;
    #    image paths always come from images.csv (img_df).
    # -------------------------------------------------------------------------
    if abs_streetscore_paths is not None:
        print(f"[Pipeline] Loading StreetScore predictions from: {abs_streetscore_paths}")
        streetscore_df = pd.concat(
            [pd.read_csv(p) for p in abs_streetscore_paths], ignore_index=True
        )
        # Drop any machine-specific path columns so they can never bleed through.
        streetscore_df.drop(
            columns=[c for c in ("path", "_base_dir", "abs_path") if c in streetscore_df.columns],
            inplace=True,
        )
    else:
        print("[Pipeline] StreetScore predictions omitted (None).")
        streetscore_df = pd.DataFrame()

    # -------------------------------------------------------------------------
    # 5. BUILD MASTER POINTS INDEX keyed by img_id
    #    img_path is always taken from img_df (images.csv), already ROOT_PATH-relative.
    # -------------------------------------------------------------------------
    points_dict: dict[str, dict] = {}
    for _, row in img_df.iterrows():
        img_id = str(row["img_id"])
        points_dict[img_id] = {
            "id":       img_id,
            "x":        float(row["x"])       if pd.notna(row.get("x"))       else None,
            "y":        float(row["y"])        if pd.notna(row.get("y"))       else None,
            "bearing":  float(row["bearing"])  if pd.notna(row.get("bearing")) else None,
            "img_path": str(row["path"]),   # ROOT_PATH-relative
            "metrics":  {}
        }

    # -------------------------------------------------------------------------
    # 6. PER-METRIC SCORE NORMALISATION & UNCERTAINTY SCALING
    # -------------------------------------------------------------------------
    for mconfig in METRICS_MAP:
        metric_val    = mconfig["streetscore_metric"]
        qid_val       = mconfig["question_id"]
        img_type_val  = mconfig["img_type"]
        scenario_val  = mconfig.get("scenario")

        metric_key = "-".join(metric_val) if isinstance(metric_val, list) else metric_val
        print(f"[Pipeline] Processing metric: '{metric_key}'...")

        # Column name lists
        qids              = qid_val if isinstance(qid_val, list) else [qid_val]
        ts_cols           = [f"score_{q}"     for q in qids]
        ts_unc_cols       = [f"uncertainty_{q}" for q in qids]
        ts_n_answers_cols = [f"n_answers_{q}" for q in qids]

        metrics        = metric_val if isinstance(metric_val, list) else [metric_val]
        ss_cols        = list(metrics)
        ss_unc_cols    = [f"uncertainty_mc_{m}" for m in metrics]
        ss_entropy_cols = [f"entropy_{m}"       for m in metrics]

        # Filter by img_type / scenario
        def _filter(df: pd.DataFrame, type_val, scen_val) -> pd.DataFrame:
            if df.empty:
                return df
            if "img_type" in df.columns:
                mask = df["img_type"].isin(type_val) if isinstance(type_val, list) else df["img_type"] == type_val
                df = df[mask]
            if scen_val is not None and "scenario" in df.columns:
                mask = df["scenario"].isin(scen_val) if isinstance(scen_val, list) else df["scenario"] == scen_val
                df = df[mask]
            return df

        ts_sub = _filter(trueskill_df.copy(),   img_type_val, scenario_val)
        ss_sub = _filter(streetscore_df.copy(), img_type_val, scenario_val)

        # Extract score series indexed by img_id
        existing_ts_cols = [c for c in ts_cols if c in ts_sub.columns] if not ts_sub.empty else []
        ts_scores = (
            ts_sub.set_index("img_id")[existing_ts_cols].mean(axis=1)
            if existing_ts_cols else pd.Series(dtype=float)
        )

        existing_ss_cols = [c for c in ss_cols if c in ss_sub.columns] if not ss_sub.empty else []
        ss_scores = (
            ss_sub.set_index("img_id")[existing_ss_cols].mean(axis=1)
            if existing_ss_cols else pd.Series(dtype=float)
        )

        ts_aligned, ss_aligned, ts_unc_mult, ss_unc_mult = normalize_and_align_distributions(
            ts_scores, ss_scores
        )

        # Write normalised values into the master points index
        for img_id in points_dict:
            ts_row = ts_sub[ts_sub["img_id"] == img_id] if not ts_sub.empty and "img_id" in ts_sub.columns else pd.DataFrame()
            ss_row = ss_sub[ss_sub["img_id"] == img_id] if not ss_sub.empty and "img_id" in ss_sub.columns else pd.DataFrame()

            ts_score = float(ts_aligned.get(img_id, np.nan)) if not ts_aligned.empty else np.nan
            ss_score = float(ss_aligned.get(img_id, np.nan)) if not ss_aligned.empty else np.nan

            # TrueSkill uncertainty (scaled to match aligned score distribution)
            ts_unc = np.nan
            existing_ts_unc = [c for c in ts_unc_cols if c in ts_row.columns] if not ts_row.empty else []
            if existing_ts_unc:
                ts_unc = float(ts_row.iloc[0][existing_ts_unc].mean()) * ts_unc_mult
                if pd.isna(ts_unc):
                    ts_unc = 0.5 * ts_unc_mult

            # StreetScore uncertainty: prefer MC dropout, fall back to entropy
            ss_unc = np.nan
            existing_ss_unc     = [c for c in ss_unc_cols     if c in ss_row.columns] if not ss_row.empty else []
            existing_ss_entropy = [c for c in ss_entropy_cols if c in ss_row.columns] if not ss_row.empty else []
            if existing_ss_unc:
                ss_unc = float(ss_row.iloc[0][existing_ss_unc].mean()) * ss_unc_mult
            elif existing_ss_entropy:
                ss_unc = float(ss_row.iloc[0][existing_ss_entropy].mean()) * 1.5 * ss_unc_mult

            # Answer count from TrueSkill rows
            n_answers = None
            existing_ts_n_ans = [c for c in ts_n_answers_cols if c in ts_row.columns] if not ts_row.empty else []
            if existing_ts_n_ans:
                n_answers = int(ts_row.iloc[0][existing_ts_n_ans].sum())

            points_dict[img_id]["metrics"][metric_key] = {
                "trueskill": {
                    "score":      None if pd.isna(ts_score) else round(ts_score, 4),
                    "uncertainty": None if pd.isna(ts_unc)   else round(ts_unc,   4),
                    "n_answers":  n_answers,
                },
                "streetscore": {
                    "score":      None if pd.isna(ss_score) else round(ss_score, 4),
                    "uncertainty": None if pd.isna(ss_unc)   else round(ss_unc,   4),
                    "n_answers":  None,
                },
            }

    compiled_points = list(points_dict.values())
    return compiled_points, unique_users, total_clicks


# ============================================================================
# ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    print("=" * 65)
    print("   FRANKFURT ANLAGENRING — PERCEPTION DATA MAPPING SYSTEM   ")
    print("=" * 65)

    points, users, clicks = load_and_compile_perceptions()

    metrics_list   = list(points[0]["metrics"].keys()) if points else ["walk", "bike", "stay"]
    default_metric = "walk" if "walk" in metrics_list else metrics_list[0]

    output_html = os.path.join(ROOT_PATH, "map.html")

    generate_custom_html_map(
        points_data=points,
        unique_users=users,
        total_clicks=clicks,
        metrics_list=metrics_list,
        default_metric=default_metric,
        output_path=output_html,
    )

    print("-" * 65)
    print("Success! Perceptual map compiled.")
    print(f"Interactive file: {output_html}")
    print("=" * 65)