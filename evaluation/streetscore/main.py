# main.py
# coding: utf-8

"""
End-to-end pipeline: fine-tune one or more perception models on local
AB-survey data, then run inference on every image in the dataset.

All configuration lives in the CONFIG section below.
Edit only this file for day-to-day usage.

Data loading
------------
HUMAN_DF_PATHS: List of CSV/JSON files or single file. Auto-concatenated.
               Files are loaded as pandas DataFrames before training.

IMG_TRAIN_PATHS, IMG_VAL_PATHS, IMG_TEST_PATHS:
    • IMG_TRAIN_PATHS: a file path or list of file paths (auto-concatenated).
      Can be a geopandas-compatible file (GeoJSON, Shapefile, GeoParquet, etc.)
      or a regular CSV/JSON file that becomes a plain DataFrame.
    • IMG_VAL_PATHS / IMG_TEST_PATHS: either a file path / list of file paths,
      OR an integer 0–100 representing a split percentage.

Split logic
-----------
1. Load all image files and concatenate.
2. Identify images that do NOT appear in any human_df AB pair (orphans).
3. Always move orphans to test first; they count toward IMG_TEST_PATHS %.
4. If IMG_TEST_PATHS is an int 0–100:
     - 100  → test set = ALL images; train set is NOT reduced (inference-only mode).
     - 0–99 → move that percentage of total images (including orphans) from train to
              test.  Orphans count first; only if they don't cover the quota are
              additional labeled images drawn from train.
5. If IMG_VAL_PATHS is an int 0–100:
     - After the test split, the remaining labeled (train) images define the pool.
     - Per metric, human_df is filtered first; then val_pct % of the AB pairs that
       reference train images are moved to val.  This is the *only* val split —
       there is no further split inside the training loop.

Checkpoint saving
-----------------
A checkpoint is saved (in addition to per-epoch checkpoints) if:
    • val_acc does NOT decrease by more than CHECKPOINT_SAVE_TOLERANCE (%)
    • AND val_score_std does NOT decrease by more than CHECKPOINT_SAVE_TOLERANCE (%)
    • AND val_score_mean does NOT decrease by more than CHECKPOINT_SAVE_TOLERANCE (%)
    • AND val_uncertainty does NOT decrease by more than CHECKPOINT_SAVE_TOLERANCE (%)
    • AND at least one of these metrics improved.

This prevents saving models when any metric significantly regresses, even if
overall val_acc stays flat. Set CHECKPOINT_SAVE_TOLERANCE to your desired
threshold (e.g., 5 for 5%).

Filter semantics
----------------
Each metric can filter both human_df and img_df by:

  QUESTION_IDS  – question_id column in human_df.
                  One string or a list of strings per metric.
                  Example: "walk-preference"  or  ["walk-preference", "walk-init"]

  IMG_TYPES     – img_type column in human_df (for training) and in img_df
                  (for inference).
                  One string or a list of strings per metric.
                  Example: "walk"  or  ["walk", "start"]

  SCENARIOS     – scenario column in human_df (for training) and in img_df
                  (for inference).
                  One string or a list of strings per metric.
                  Example: "Anlagenring"  or  ["Anlagenring", "Nordend"]

For training, the three filters are applied to human_df (in addition to the
existing type=="AB" and answer in {"A","B"} filters).  The img_df used for
training is then restricted to image IDs that actually appear in the filtered
human_df pairs.

For inference, IMG_TYPES and SCENARIOS are applied directly to img_df so
that all matching images are scored — regardless of whether they appeared in
any AB pair.

Any filter set to None (or an empty list) means "no filter on that column".

List-of-lists broadcasting
--------------------------
Each of QUESTION_IDS / IMG_TYPES / SCENARIOS follows the same rule:

  • A plain scalar (str or None) is broadcast to every metric.
  • A flat list whose length equals len(METRICS) is used element-by-element.
  • A list-of-lists (each inner element is a list or a scalar) gives full
    per-metric control; inner scalars are auto-wrapped to single-element lists.

RESUME_TRAINING behaviour
--------------------------
Controls what happens when a trained .pth already exists in MODEL_FOLDER:

  True  – load the existing model and continue training from where it left off.
           history.csv is appended (not overwritten), so epoch numbers carry on.

  False – skip training entirely for that metric and go straight to inference.
           No original_{metric} column is produced for already-trained metrics
           (those scores are assumed to already be in scores.csv).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Union

import geopandas as gpd
import pandas as pd
import torch
from huggingface_hub import login

import tokens

# ============================================================================
# CONFIG — edit freely, nothing else needs to change
# ============================================================================

# --- Paths: Human survey data -----------------------------------------------
# Single file or list of files. Will be concatenated if multiple.
# Supported formats: CSV, JSON
HUMAN_DF_PATHS: Union[str, list[str]] = (
    "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/"
    "ABsurveys/user_data/Anlagenring_user_data_merged.csv"
)

# --- Paths: Image data (train/val/test splits) ----------------------------
# Each can be:
#   • A file path (single file or list) — loaded and concatenated
#   • An integer between 0 and 100 — treated as a percentage
#
# IMG_VAL_PATHS (int 0–100):
#   After the test split, this percentage of AB pairs (per metric, after
#   metric-specific filtering) are moved from train → val.
#   The split is applied at the human_df / AB-pair level, not the image level,
#   so the val set always has genuine pairwise labels.
#   0 → no validation set; early stopping and checkpoint logic are disabled.
#
# IMG_TEST_PATHS (int 0–100):
#   Orphaned images (not present in any human_df AB pair) always go to test
#   first and count toward this percentage of the *total* image pool.
#   If orphans already cover the requested %, no labeled images are removed.
#   Special value 100 → run inference on ALL images without reducing train
#   (the only case where train is left completely intact).
#
# IMPORTANT: Images not present in any human_df AB pair are automatically moved
# to the test set (with a warning printed) and count toward IMG_TEST_PATHS %.

IMG_TRAIN_PATHS: Union[str, list[str]] = (
    "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/"
    "ABsurveys/images/Anlagenring/images.csv"
)

IMG_VAL_PATHS: Union[str, list[str], int] = 30   # 15 % of labeled train images → val
IMG_TEST_PATHS: Union[str, list[str], int] = 100  # 15 % of images → test (100 = all, keep train intact)

# --- Metrics to train -------------------------------------------------------
#
# All lists must have the same length — one element per metric.
# See the module docstring for the list-of-lists broadcast rules.

METRICS: list[str] = [
    "walk",
    # "start",
]

# question_id filter(s) applied to human_df — one value or list per metric.
QUESTION_IDS: list | str | None = [
    "walk-preference",          # single string for the "walk" metric
    # ["start-preference"],     # list form also works
]

# img_type filter(s) applied to human_df (train) and img_df (inference).
IMG_TYPES: list | str | None = [
    "walk",
    # ["walk", "start"],        # multiple img_types for one metric
]

# scenario filter(s) applied to human_df (train) and img_df (inference).
SCENARIOS: list | str | None = [
    "Anlagenring",
    # None,                     # None → no scenario filter for that metric
]

# --- Starting checkpoints ---------------------------------------------------
FROM_CHECKPOINTS: list[str | None] | str | None = [
    "safety",   # fine-tune "walk" starting from the safety checkpoint
    # None,     # train from scratch
]

# --- Resume / skip behaviour ------------------------------------------------
#
# What to do when MODEL_FOLDER already contains a trained .pth for a metric:
#
#   True  — resume training: load the existing model and keep training.
#            history.csv is appended so the epoch log is continuous.
#
#   False — skip training: go straight to inference with the existing model.
#            No original_{metric} column is computed (assumed already present
#            in scores.csv from a previous run).
#
RESUME_TRAINING: bool = True

# --- Checkpoint saving strategy ---------------------------------------------
#
# When any of the 4 metrics (val_acc, val_score_std, val_score_mean, val_uncertainty)
# decreases by more than CHECKPOINT_SAVE_TOLERANCE (%), that epoch's checkpoint
# is NOT saved (beyond the auto per-epoch files). Only when all metrics stay within
# the tolerance AND at least one improved, a checkpoint is saved as {metric}.pth.
#
# This prevents overwriting the best model when progress stalls or metrics regress.
#
# Suggested values:
#   5   – strict: save only when all metrics improve or stay within 5%
#   10  – moderate: allow up to 10% tolerance
#   None – disable: revert to original behavior (always save on val_acc improvement)
#
CHECKPOINT_SAVE_TOLERANCE: float = 5.0  # percent; set to None to disable

# --- Output directories -----------------------------------------------------
MODEL_FOLDER: str = "models/FrankfurtAnlagenring"
PRETRAINED_MODEL_DIR: str = "models/default_models"

# --- Model initialisation ---------------------------------------------------
VIT_WEIGHTS: bool = True
FREEZE_VIT: bool = True

# --- Training hyperparameters -----------------------------------------------
EPOCHS: int = 2
BATCH_SIZE: int = 16
LEARNING_RATE: float = 1e-4

# (VAL_SPLIT has been removed — the validation set is controlled solely by
# IMG_VAL_PATHS and is applied at the image-registry / human_df level before
# training starts.  There is no secondary split inside the training loop.)
NUM_WORKERS: int = 4

# --- Early stopping ---------------------------------------------------------
EARLY_STOPPING_PATIENCE: int = 4
EARLY_STOPPING_MIN_DELTA: float = 0.005

# --- MC-Dropout uncertainty — TRAINING monitoring ---------------------------
#
# Number of stochastic dropout passes run on the *validation set* at the end
# of each training epoch to track how confident the model is becoming.
#
#   0 or 1 → skip (no val_uncertainty column in history.csv, faster training)
#   10–20  → good balance of insight vs. extra time per epoch
#
# The mean per-image MC-Dropout std is logged as ``val_uncertainty`` in
# {metric}_history.csv and appears as the 3rd panel in {metric}_curves.jpg.
# Use it as an additional early-stopping signal: if val_uncertainty has
# plateaued for several epochs even though val_acc is still improving, the
# model is converging in confidence and further training may overfit.
TRAINING_MC_PASSES: int = 10

# --- MC-Dropout uncertainty — INFERENCE --------------------------------------
#
# Number of stochastic dropout passes per image during final inference.
#
#   0 or 1 → deterministic, no uncertainty columns written to scores.csv
#   20–50  → good estimate; inference takes ~INFERENCE_MC_PASSES× longer
#
# When enabled, adds columns to scores.csv:
#   ``uncertainty_{metric}``          – for your fine-tuned model
#   ``uncertainty_original_{metric}`` – for the pre-trained baseline model
INFERENCE_MC_PASSES: int = 20

# --- Device -----------------------------------------------------------------
DEVICE = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# --- Inference column -------------------------------------------------------
IMAGE_COLUMN: str = "abs_path"

# ============================================================================
# Authenticate with Hugging Face
# ============================================================================
login(token=tokens.token)


# ============================================================================
# Internal helpers
# ============================================================================

def _as_list_or_none(value) -> list | None:
    """Wrap a scalar in a list; pass lists and None through unchanged."""
    if value is None:
        return None
    if isinstance(value, list):
        return value
    return [value]


def _broadcast_to_metrics(value, metrics: list[str]) -> list:
    """
    Broadcast a scalar or list to match the length of metrics.

    Elements are wrapped in lists so they can be used as filter value sets.
    Use _broadcast_scalar_to_metrics for per-metric scalars (e.g. checkpoint
    names) that must NOT be wrapped in lists.

    Examples:
      _broadcast_to_metrics("walk", ["walk", "start"])
          → ["walk", "walk"]

      _broadcast_to_metrics(["w1", "s1"], ["walk", "start"])
          → ["w1", "s1"]

      _broadcast_to_metrics([["w1", "w2"], "s1"], ["walk", "start"])
          → [["w1", "w2"], ["s1"]]  (inner scalars wrapped in lists)
    """
    if value is None:
        return [None] * len(metrics)

    if not isinstance(value, list):
        # Scalar — broadcast to all metrics
        return [value] * len(metrics)

    if len(value) == len(metrics):
        # Same length as metrics — assume element-by-element mapping
        # Wrap any scalar inner elements in lists
        return [
            [v] if not isinstance(v, list) else v
            for v in value
        ]

    # Otherwise assume it's a flat list of inner values;
    # broadcast the whole thing to all metrics
    return [[v] if not isinstance(v, list) else v for v in value] * len(metrics)


def _broadcast_scalar_to_metrics(value, metrics: list[str]) -> list:
    """
    Broadcast a per-metric scalar (str | None) to a list of length len(metrics).

    Unlike _broadcast_to_metrics, elements are NEVER wrapped in an extra list.
    Use this for values where each metric expects exactly one scalar or None
    (e.g. FROM_CHECKPOINTS checkpoint names).

    Examples:
      _broadcast_scalar_to_metrics("safety", ["walk", "start"])
          → ["safety", "safety"]

      _broadcast_scalar_to_metrics(["safety", None], ["walk", "start"])
          → ["safety", None]

      _broadcast_scalar_to_metrics(None, ["walk", "start"])
          → [None, None]
    """
    if value is None:
        return [None] * len(metrics)

    if not isinstance(value, list):
        # Scalar — broadcast to all metrics unchanged
        return [value] * len(metrics)

    if len(value) == len(metrics):
        # Already one entry per metric — use as-is
        return list(value)

    raise ValueError(
        f"FROM_CHECKPOINTS has {len(value)} entries but METRICS has "
        f"{len(metrics)}. Provide either a single value (broadcast to all) "
        "or one value per metric."
    )


def _filter_df(df: pd.DataFrame, column: str, values: list | None) -> pd.DataFrame:
    """Keep only rows where df[column] is in values (flattened from list-of-lists)."""
    if values is None or not values:
        return df
    flat = []
    for v in values:
        if isinstance(v, list):
            flat.extend(v)
        else:
            flat.append(v)
    if not flat:
        return df
    return df[df[column].isin(flat)]


def _load_image_dataframes(paths: Union[str, list[str], int]) -> pd.DataFrame:
    """
    Load image files from one or more paths and concatenate them.

    Supports:
      • Single file path (str)
      • List of file paths
      • Integer 0–100 → returned as None (percentage handled by caller)

    Each loaded DataFrame gets a ``_base_dir`` column set to the parent
    directory of the source file, so relative ``path`` values in each file
    are resolved against the correct directory regardless of how many files
    are combined.

    Detects geopandas-compatible formats (GeoJSON, Shapefile, etc.) and
    loads them as GeoDataFrames, otherwise as regular DataFrames.
    """
    if isinstance(paths, (int, float)):
        # Percentage value → handled by caller; return None as a marker
        return None

    if isinstance(paths, str):
        paths = [paths]

    dfs = []
    for path in paths:
        if isinstance(path, float):
            continue  # Skip floats
        path = str(path)
        base_dir = str(Path(path).parent)
        print(f"  Loading {path} …")
        try:
            # Try geopandas first for GIS formats
            if any(path.endswith(ext) for ext in ['.geojson', '.shp', '.gpkg', '.geoparquet']):
                df = gpd.read_file(path)
            elif path.endswith('.json'):
                df = pd.read_json(path)
            else:
                df = pd.read_csv(path)
            df["_base_dir"] = base_dir
            dfs.append(df)
        except Exception as e:
            print(f"    Warning: Failed to load {path}: {e}")

    if not dfs:
        raise ValueError(f"No valid image dataframes loaded from {paths}")

    result = pd.concat(dfs, ignore_index=True)
    print(f"  Total images loaded: {len(result):,}")
    return result


def _load_human_dataframes(paths: Union[str, list[str]]) -> pd.DataFrame:
    """Load human survey data from one or more CSV/JSON files and concatenate."""
    if isinstance(paths, str):
        paths = [paths]

    dfs = []
    for path in paths:
        path = str(path)
        print(f"  Loading {path} …")
        try:
            if path.endswith('.json'):
                dfs.append(pd.read_json(path))
            else:
                dfs.append(pd.read_csv(path))
        except Exception as e:
            print(f"    Warning: Failed to load {path}: {e}")

    if not dfs:
        raise ValueError(f"No valid human dataframes loaded from {paths}")

    result = pd.concat(dfs, ignore_index=True)
    print(f"  Total AB rows loaded: {len(result):,}")
    return result


def _split_train_test(
    img_labeled: pd.DataFrame,
    img_orphaned: pd.DataFrame,
    test_pct: Union[int, None],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split images into (train, test) using integer percentages (0–100).

    Val splitting is NOT done here — it is deferred to per-metric time so it
    can be applied on the already-filtered human_df AB pairs.

    Parameters
    ----------
    img_labeled:
        Images that appear in at least one human_df AB pair (eligible for train).
    img_orphaned:
        Images that do NOT appear in any AB pair; always go to test first.
    test_pct:
        Integer 0–100.
        • 100 → test set = ALL images; train is NOT reduced (inference-only mode).
        • 0–99 → orphans go to test first and count toward this % of *total*
                 images.  Additional labeled images are drawn from train only if
                 orphans do not already cover the quota.
        • 0 or None → only orphans go to test; train is not reduced further.

    Returns
    -------
    (train_df, test_df) — both reset-indexed.
    """
    from sklearn.model_selection import train_test_split

    total_images = len(img_labeled) + len(img_orphaned)

    # ── Special case: test_pct == 100 ────────────────────────────────────────
    # Run inference on every image without touching the training pool.
    if isinstance(test_pct, int) and test_pct == 100:
        test_df = pd.concat([img_labeled, img_orphaned], ignore_index=True)
        train_df = img_labeled.copy()   # train is NOT reduced
        print(f"  test_pct=100 → test set = all {len(test_df):,} images "
              f"(train kept intact: {len(train_df):,})")
        return (
            train_df.reset_index(drop=True),
            test_df.reset_index(drop=True),
        )

    # ── Normal case: test_pct in 0–99 ────────────────────────────────────────
    # Orphans always go to test and count toward the quota.
    test_df = img_orphaned.copy()
    train_df = img_labeled.copy()

    if isinstance(test_pct, int) and test_pct > 0:
        target_test_n = round(total_images * test_pct / 100)
        already_in_test = len(test_df)
        still_needed = max(0, target_test_n - already_in_test)

        if still_needed > 0 and len(train_df) > 0:
            actual_take = min(still_needed, len(train_df))
            extra_test, train_df = train_test_split(
                train_df,
                test_size=actual_take,
                random_state=42,
            )
            test_df = pd.concat([test_df, extra_test], ignore_index=True)

        print(
            f"  test_pct={test_pct}% → target {target_test_n:,} test images  "
            f"(orphans: {already_in_test:,} + drawn from train: "
            f"{len(test_df) - already_in_test:,})  "
            f"train remaining: {len(train_df):,}"
        )
    else:
        if len(test_df) > 0:
            print(f"  test_pct=0 → only orphans in test ({len(test_df):,}), "
                  f"train unchanged ({len(train_df):,})")

    return (
        train_df.reset_index(drop=True),
        test_df.reset_index(drop=True),
    )


def _split_human_df_val(
    human_df: pd.DataFrame,
    train_img_ids: set[str],
    val_pct: Union[int, None],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split a (already metric-filtered) human_df into train and val AB pairs.

    Only pairs where *both* images are in *train_img_ids* are eligible; any
    pair where one image ended up in the test set is silently excluded from
    both train and val (it will never reach a DataLoader).

    Parameters
    ----------
    human_df:
        Metric-filtered AB pairs (type=="AB", answer in {"A","B"}).
    train_img_ids:
        Set of image IDs that remain in the training image pool after the
        test split.  Pairs referencing test images are excluded.
    val_pct:
        Integer 0–100.  Move this % of eligible AB pairs to val.
        0 or None → all eligible pairs go to train, val is empty.

    Returns
    -------
    (train_human_df, val_human_df) — both reset-indexed.
    """
    from sklearn.model_selection import train_test_split

    # Keep only pairs where both images are in the train pool
    eligible = human_df[
        human_df["img_id_A"].astype(str).isin(train_img_ids)
        & human_df["img_id_B"].astype(str).isin(train_img_ids)
    ].copy()

    excluded = len(human_df) - len(eligible)
    if excluded > 0:
        print(f"    ⚠️  {excluded:,} AB pair(s) reference test-set images and are excluded "
              f"from train/val.")

    if not (isinstance(val_pct, int) and 0 < val_pct < 100) or len(eligible) == 0:
        return eligible.reset_index(drop=True), pd.DataFrame(columns=human_df.columns)

    train_hdf, val_hdf = train_test_split(
        eligible,
        test_size=val_pct / 100.0,
        random_state=42,
    )
    print(f"    val_pct={val_pct}% → train {len(train_hdf):,} pairs, "
          f"val {len(val_hdf):,} pairs")
    return train_hdf.reset_index(drop=True), val_hdf.reset_index(drop=True)


def build_image_gdf(img_df: pd.DataFrame) -> gpd.GeoDataFrame:
    """
    Convert image DataFrame to GeoDataFrame with absolute paths.

    Each row's ``path`` value is resolved against the ``_base_dir`` column
    that was stamped onto it at load time by ``_load_image_dataframes``.
    This means images from different source files are each resolved relative
    to their own source file's parent directory.

    Assumes img_df has columns:
      - img_id
      - path          (relative or absolute image path)
      - _base_dir     (parent dir of the CSV/JSON file this row came from)
      - optionally: geometry (for geodataframes)
    """
    if "path" not in img_df.columns:
        raise ValueError("img_df must have a 'path' column")

    result = img_df.copy()

    # Ensure img_id is string
    result["img_id"] = result["img_id"].astype(str)

    # Resolve each path against its own source file's parent directory
    def _resolve(row) -> str:
        p = str(row["path"])
        if os.path.isabs(p):
            return p
        base = str(row["_base_dir"]) if "_base_dir" in row.index else ""
        return os.path.join(base, p) if base else p

    result[IMAGE_COLUMN] = result.apply(_resolve, axis=1)

    # Convert to GeoDataFrame if geometry column exists
    if "geometry" in result.columns and not isinstance(result, gpd.GeoDataFrame):
        result = gpd.GeoDataFrame(result, geometry="geometry")
    elif not isinstance(result, gpd.GeoDataFrame):
        result = gpd.GeoDataFrame(result)

    return result


# ============================================================================
# Main pipeline
# ============================================================================

def main() -> None:
    """
    Fine-tune metrics, then run inference on all images.

    Pipeline:
      1. Load human_df and img_train/img_val/img_test
      2. Move images not in human_df to img_test (with warning)
      3. Train models
      4. Score images
    """
    from inference import run
    from train import train

    print(f"Device: {DEVICE}  |  Precision: float32")
    print(f"{'='*80}")

    # ====================================================================
    # 0. Load and validate data
    # ====================================================================
    print("[0/3] Loading data …")

    print("\n  Loading human survey responses …")
    human_df_raw = _load_human_dataframes(HUMAN_DF_PATHS)

    print("\n  Loading image registry …")
    img_train_paths = IMG_TRAIN_PATHS if IMG_TRAIN_PATHS is not None else []
    img_val_pct  = IMG_VAL_PATHS  if isinstance(IMG_VAL_PATHS,  (int, float)) else None
    img_test_pct = IMG_TEST_PATHS if isinstance(IMG_TEST_PATHS, (int, float)) else None

    # Validate percentage values
    if img_val_pct is not None:
        img_val_pct = int(img_val_pct)
        if not (0 <= img_val_pct <= 100):
            raise ValueError(f"IMG_VAL_PATHS must be an integer 0–100, got {img_val_pct}")
    if img_test_pct is not None:
        img_test_pct = int(img_test_pct)
        if not (0 <= img_test_pct <= 100):
            raise ValueError(f"IMG_TEST_PATHS must be an integer 0–100, got {img_test_pct}")

    # Load image files (file-path variants; returns None for percentage configs)
    img_train_df = _load_image_dataframes(img_train_paths)
    img_val_df   = _load_image_dataframes(IMG_VAL_PATHS)   if not isinstance(IMG_VAL_PATHS,  (int, float)) else None
    img_test_df  = _load_image_dataframes(IMG_TEST_PATHS)  if not isinstance(IMG_TEST_PATHS, (int, float)) else None

    # The full image pool is the train file plus any explicitly provided val/test files
    img_dfs_to_concat = [df for df in [img_train_df, img_val_df, img_test_df] if df is not None]
    if not img_dfs_to_concat:
        raise ValueError("No image dataframes loaded")

    img_df_combined = pd.concat(img_dfs_to_concat, ignore_index=True)

    if "img_id" not in img_df_combined.columns:
        raise ValueError("Image dataframe must have an 'img_id' column")

    img_df_combined["img_id"] = img_df_combined["img_id"].astype(str)
    print(f"  Total images across all sources: {len(img_df_combined):,}")

    # ====================================================================
    # Extract valid image IDs from human_df
    # ====================================================================
    valid_img_ids = (
        pd.concat([human_df_raw["img_id_A"], human_df_raw["img_id_B"]])
        .astype(str)
        .unique()
        .tolist()
    )
    print(f"  Image IDs appearing in AB pairs: {len(valid_img_ids):,}")

    # Separate labeled images (in AB pairs) from orphans (not in any pair)
    mask_in_pairs = img_df_combined["img_id"].isin(valid_img_ids)
    img_orphaned  = img_df_combined[~mask_in_pairs].reset_index(drop=True)
    img_labeled   = img_df_combined[mask_in_pairs].reset_index(drop=True)

    if len(img_orphaned) > 0:
        print(f"\n  ⚠️  WARNING: {len(img_orphaned):,} images do NOT appear in any AB pair.")
        print(f"      They will be placed in the test set and count toward IMG_TEST_PATHS %.")

    print(f"  Labeled images (in AB pairs): {len(img_labeled):,}")

    # ====================================================================
    # Split train / test at the image-registry level
    # Val split is deferred to per-metric time (human_df level)
    # ====================================================================
    print("\n  Splitting train/test …")
    img_train_final, img_test_final_split = _split_train_test(
        img_labeled=img_labeled,
        img_orphaned=img_orphaned,
        test_pct=img_test_pct,
    )

    # Merge with any explicitly loaded test files
    if img_test_df is not None and len(img_test_df) > 0:
        img_test_final = pd.concat([img_test_df, img_test_final_split], ignore_index=True)
    else:
        img_test_final = img_test_final_split

    print(f"  Image split: train {len(img_train_final):,}, "
          f"test {len(img_test_final):,}")

    # The set of image IDs that remain in the training pool (used for val split)
    train_img_id_set = set(img_train_final["img_id"].astype(str).tolist())

    # Inference runs over the test set; training uses img_train_final
    img_df_raw = img_test_final if img_test_pct == 100 else img_train_final

    # ====================================================================
    # 1. Training
    # ====================================================================
    print(f"\n[1/3] Training models …")

    # Broadcast filters to per-metric lists
    question_ids_list = _broadcast_to_metrics(QUESTION_IDS, METRICS)
    img_types_list = _broadcast_to_metrics(IMG_TYPES, METRICS)
    scenarios_list = _broadcast_to_metrics(SCENARIOS, METRICS)
    checkpoints = _broadcast_scalar_to_metrics(FROM_CHECKPOINTS, METRICS)

    skipped_metrics = set()

    for metric, q_ids, img_types, from_ckpt_for_train in zip(
        METRICS, question_ids_list, img_types_list, checkpoints
    ):
        print(f"\n  Metric '{metric}':")

        # Check if model already exists and if we should skip
        model_path = os.path.join(MODEL_FOLDER, f"{metric}.pth")
        if os.path.isfile(model_path) and not RESUME_TRAINING:
            print(f"    Model exists and RESUME_TRAINING=False → skipping training")
            skipped_metrics.add(metric)
            continue

        # Filter human_df for this metric
        human_df_all = human_df_raw[
            (human_df_raw["type"] == "AB")
            & (human_df_raw["answer"].isin(["A", "B"]))
        ].copy()

        human_df_all = _filter_df(human_df_all, "question_id", q_ids)
        human_df_all = _filter_df(human_df_all, "img_type", img_types)
        human_df_all = _filter_df(human_df_all, "scenario", scenarios_list[METRICS.index(metric)])
        human_df_all = human_df_all.reset_index(drop=True)

        print(f"    AB rows after filtering: {len(human_df_all):,}")

        if len(human_df_all) == 0:
            raise ValueError(
                f"No AB rows found for metric '{metric}' after filtering. "
                "Check QUESTION_IDS, IMG_TYPES, and SCENARIOS."
            )

        # Split human_df into train / val AB pairs at this metric's level.
        # Only pairs whose both images are in the train image pool are eligible.
        human_df_train, human_df_val = _split_human_df_val(
            human_df=human_df_all,
            train_img_ids=train_img_id_set,
            val_pct=img_val_pct,
        )

        print(f"    Train AB pairs: {len(human_df_train):,}  |  "
              f"Val AB pairs: {len(human_df_val):,}")

        if len(human_df_train) == 0:
            raise ValueError(
                f"No AB training pairs remain for metric '{metric}' after val split. "
                "Lower IMG_VAL_PATHS or check your data."
            )

        # Restrict img_df to IDs present in the training AB pairs
        train_valid_ids = (
            pd.concat([human_df_train["img_id_A"], human_df_train["img_id_B"]])
            .astype(str)
            .unique()
            .tolist()
        )
        img_df_train = img_train_final[
            img_train_final["img_id"].isin(train_valid_ids)
        ].reset_index(drop=True)
        print(f"    Images used for training: {len(img_df_train):,}")

        # Val img_df: restrict to images referenced by val AB pairs
        val_img_df_metric = None
        if len(human_df_val) > 0:
            val_valid_ids = (
                pd.concat([human_df_val["img_id_A"], human_df_val["img_id_B"]])
                .astype(str)
                .unique()
                .tolist()
            )
            val_img_df_metric = img_train_final[
                img_train_final["img_id"].isin(val_valid_ids)
            ].reset_index(drop=True)

        saved_path = train(
            human_df=human_df_train,
            img_df=img_df_train,
            metric=metric,
            model_folder=MODEL_FOLDER,
            val_human_df=human_df_val if len(human_df_val) > 0 else None,
            val_img_df=val_img_df_metric,
            from_checkpoint=from_ckpt_for_train,
            vit_weights=VIT_WEIGHTS,
            freeze_vit=FREEZE_VIT,
            epochs=EPOCHS,
            batch_size=BATCH_SIZE,
            lr=LEARNING_RATE,
            num_workers=NUM_WORKERS,
            device=DEVICE,
            pretrained_model_dir=PRETRAINED_MODEL_DIR,
            early_stopping_patience=EARLY_STOPPING_PATIENCE,
            early_stopping_min_delta=EARLY_STOPPING_MIN_DELTA,
            mc_passes=TRAINING_MC_PASSES,
            checkpoint_save_tolerance=CHECKPOINT_SAVE_TOLERANCE,
        )
        print(f"    ✓ Saved → {saved_path}")

    # ====================================================================
    # 2. Inference
    # ====================================================================
    print(f"\n[2/3] Running inference …")

    full_image_gdf = build_image_gdf(img_df_raw)

    out_csv = os.path.join(MODEL_FOLDER, "scores.csv")
    existing_scores = None
    if os.path.isfile(out_csv):
        existing_scores = pd.read_csv(out_csv)
        print(f"  Found existing scores ({len(existing_scores):,} rows) — upserting …")

    # Accumulates the final merged result for this run
    run_result: gpd.GeoDataFrame | None = None

    for metric, img_types, scenarios, from_ckpt in zip(
        METRICS, img_types_list, scenarios_list, checkpoints
    ):
        was_skipped = metric in skipped_metrics

        print(f"\n  Metric '{metric}' (skipped_training={was_skipped}): "
              f"filtering inference images …")

        infer_gdf = full_image_gdf.copy()
        infer_gdf = _filter_df(infer_gdf, "img_type", img_types)
        infer_gdf = _filter_df(infer_gdf, "scenario", scenarios)
        infer_gdf["img_id"] = infer_gdf["img_id"].astype(str)
        print(f"    Total images in scope: {len(infer_gdf):,}")

        # ── Determine which images still need new inference scores ───────────
        orig_col = f"original_{metric}"

        if existing_scores is not None and metric in existing_scores.columns:
            already_scored_ids = set(
                existing_scores.loc[
                    existing_scores[metric].notna(), "img_id"
                ].astype(str)
            )
            new_infer_gdf = infer_gdf[~infer_gdf["img_id"].isin(already_scored_ids)]
            print(f"    Already scored (metric): {len(already_scored_ids):,}  "
                  f"→  need to score: {len(new_infer_gdf):,}")
        else:
            new_infer_gdf = infer_gdf
            already_scored_ids = set()

        # ── Score new images with the trained model ──────────────────────────
        if len(new_infer_gdf) > 0:
            scored = run(
                gdf=new_infer_gdf,
                metrics=[metric],
                model_dir=MODEL_FOLDER,
                image_column=IMAGE_COLUMN,
                device=DEVICE,
                download_missing_models=False,
                mc_passes=INFERENCE_MC_PASSES,
            )
        else:
            print(f"    All images already scored for '{metric}' — skipping model inference.")
            scored = new_infer_gdf.copy()
            scored[metric] = pd.Series(dtype=float)

        # ── original_{metric}: only for non-skipped runs, only new images ───
        if not was_skipped and from_ckpt is not None:
            # Which images are missing the orig_ score?
            if existing_scores is not None and orig_col in existing_scores.columns:
                already_orig_ids = set(
                    existing_scores.loc[
                        pd.to_numeric(
                            existing_scores[orig_col], errors="coerce"
                        ).notna(),
                        "img_id",
                    ].astype(str)
                )
            else:
                already_orig_ids = set()

            need_orig_gdf = infer_gdf[~infer_gdf["img_id"].isin(already_orig_ids)]
            print(
                f"    Already have {orig_col}: {len(already_orig_ids):,}  "
                f"→  need to score: {len(need_orig_gdf):,}"
            )

            if len(need_orig_gdf) > 0:
                tmp = run(
                    gdf=need_orig_gdf,
                    metrics=[from_ckpt],
                    model_dir=PRETRAINED_MODEL_DIR,
                    image_column=IMAGE_COLUMN,
                    device=DEVICE,
                    download_missing_models=True,
                    mc_passes=INFERENCE_MC_PASSES,
                )
                # Build the columns to merge: score + uncertainty (if present)
                orig_unc_col = f"uncertainty_{orig_col}"
                src_unc_col = f"uncertainty_{from_ckpt}"

                merge_assign = {orig_col: tmp[from_ckpt].values}
                if INFERENCE_MC_PASSES > 1 and src_unc_col in tmp.columns:
                    merge_assign[orig_unc_col] = tmp[src_unc_col].values

                scored = scored.merge(
                    tmp[["img_id"]].assign(**merge_assign),
                    on="img_id",
                    how="outer",
                )
            else:
                print(f"    All images already have {orig_col} — skipping.")

        # ── Accumulate into run_result ────────────────────────────────────────
        if run_result is None:
            run_result = scored
        else:
            new_cols = [c for c in scored.columns if c not in run_result.columns]
            run_result = run_result.merge(
                scored[["img_id"] + new_cols],
                on="img_id",
                how="outer",
            )

    if run_result is None:
        print("No inference was run — nothing to save.")
        return

    # ====================================================================
    # 3. Save scores
    # ====================================================================
    all_score_cols = METRICS + [
        f"original_{m}"
        for m, ckpt in zip(METRICS, checkpoints)
        if ckpt is not None and m not in skipped_metrics
    ]
    if INFERENCE_MC_PASSES > 1:
        all_score_cols += [f"uncertainty_{m}" for m in METRICS]
        all_score_cols += [
            f"uncertainty_original_{m}"
            for m, ckpt in zip(METRICS, checkpoints)
            if ckpt is not None and m not in skipped_metrics
        ]
    all_score_cols = [c for c in all_score_cols if c in run_result.columns]

    final_scores = run_result.copy()
    final_scores = final_scores[["img_id"] + [c for c in final_scores.columns if c != "img_id"]]

    # Upsert into existing scores if needed
    if existing_scores is not None:
        # Keep non-overlapping rows from existing
        existing_ids = set(existing_scores["img_id"].astype(str))
        new_ids = set(final_scores["img_id"].astype(str))
        to_keep = existing_scores[~existing_scores["img_id"].astype(str).isin(new_ids)]
        final_scores = pd.concat([to_keep, final_scores], ignore_index=True)

    Path(MODEL_FOLDER).mkdir(parents=True, exist_ok=True)
    final_scores.to_csv(out_csv, index=False)

    display_cols = ["img_id", IMAGE_COLUMN] + [
        c for c in all_score_cols if c in final_scores.columns
    ]
    display_cols = [c for c in display_cols if c in final_scores.columns]

    print(f"\nScores saved → {out_csv}  ({len(final_scores):,} total rows)")
    print(final_scores[display_cols].head(10).to_string(index=False))
    print("\nDone.")


if __name__ == "__main__":
    main()