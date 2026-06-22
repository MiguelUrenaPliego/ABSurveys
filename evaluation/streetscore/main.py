# main.py
# coding: utf-8

"""
End-to-end pipeline: fine-tune one or more perception models on local
AB-survey data, then run inference on every image in the dataset.

All configuration lives in the CONFIG section below.
Edit only this file for day-to-day usage.

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
from pathlib import Path

import geopandas as gpd
import pandas as pd
import torch
from huggingface_hub import login

import tokens

# ============================================================================
# CONFIG — edit freely, nothing else needs to change
# ============================================================================

# --- Paths ------------------------------------------------------------------
HUMAN_DF_PATH: str = (
    "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/"
    "ABsurveys/user_data/Anlagenring_user_data_merged.csv"
)

IMG_DF_PATH: str = (
    "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/"
    "ABsurveys/images/Anlagenring/images.csv"
)

# Relative paths inside images.csv are resolved against this directory.
IMG_BASE_DIR: str = str(Path(IMG_DF_PATH).parent)

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
VAL_SPLIT: float = 0.20
NUM_WORKERS: int = 4

# --- Early stopping ---------------------------------------------------------
EARLY_STOPPING_PATIENCE: int = 4
EARLY_STOPPING_MIN_DELTA: float = 0.005

# --- Uncertainty (MC-Dropout) -----------------------------------------------
#
# Number of stochastic forward passes per image for uncertainty estimation.
# 0 or 1 → deterministic inference, no uncertainty columns written.
# 20–50   → good estimate; inference takes ~mc_passes× longer.
#
# When enabled, columns named ``uncertainty_{metric}`` and
# ``uncertainty_original_{metric}`` are added to scores.csv.
MC_PASSES: int = 20

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


def _broadcast_filter(
    param,
    n: int,
    name: str,
) -> list[list | None]:
    """
    Normalise a CONFIG filter parameter to a list of length *n* where each
    element is either None or a flat list of strings.

    Accepted input shapes
    ---------------------
    • None                → [None] * n
    • "foo"               → [["foo"]] * n
    • ["foo", "bar"]      → depends on n:
        - if n == 2  → [["foo"], ["bar"]]   (one filter per metric)
        - if n == 1  → [["foo", "bar"]]     (both values for the single metric)
      NOTE: ambiguous when n == len(list).  We treat a flat list of *strings*
      whose length equals n as one-filter-per-metric.  To pass multiple values
      to every metric use a list-of-lists: [["foo","bar"], ["foo","bar"]].
    • [["foo","bar"], None, "baz"]  → [["foo","bar"], None, ["baz"]]
    """
    if param is None:
        return [None] * n

    if not isinstance(param, list):
        return [[param]] * n

    is_lol = any(isinstance(x, list) for x in param)

    if is_lol or len(param) != n:
        if len(param) != n:
            raise ValueError(
                f"{name} has {len(param)} entries but METRICS has {n}. "
                "They must have the same length."
            )
        return [_as_list_or_none(x) for x in param]
    else:
        return [[x] if x is not None else None for x in param]


def _resolve_checkpoints(
    from_checkpoints,
    n: int,
) -> list[str | None]:
    """Broadcast a scalar checkpoint to a list of length n."""
    if isinstance(from_checkpoints, (str, type(None))):
        return [from_checkpoints] * n
    if len(from_checkpoints) != n:
        raise ValueError(
            f"FROM_CHECKPOINTS has {len(from_checkpoints)} entries but "
            f"METRICS has {n}. They must be the same length."
        )
    return list(from_checkpoints)


def _filter_df(
    df: pd.DataFrame,
    column: str,
    values: list | None,
) -> pd.DataFrame:
    """Return rows where df[column] is in *values*; if None, return df unchanged."""
    if values is None:
        return df
    return df[df[column].isin(values)]


def build_image_gdf(img_df: pd.DataFrame, img_base_dir: str) -> gpd.GeoDataFrame:
    """Return a GeoDataFrame with a resolved absolute-path column."""
    df = img_df[["img_id", "path", "img_type", "scenario"]].copy()

    def resolve(p: str) -> str:
        if not isinstance(p, str):
            return p
        return p if os.path.isabs(p) else os.path.join(img_base_dir, p)

    df[IMAGE_COLUMN] = df["path"].apply(resolve)
    return gpd.GeoDataFrame(df, geometry=None)


def _load_scores_csv(path: Path) -> pd.DataFrame | None:
    """Load existing scores.csv if it exists, else return None."""
    if path.is_file():
        df = pd.read_csv(path)
        df["img_id"] = df["img_id"].astype(str)
        print(f"  Loaded existing scores.csv ({len(df):,} rows) from {path}")
        return df
    return None


def _upsert_scores(
    existing: pd.DataFrame | None,
    new_scores: pd.DataFrame,
    score_cols: list[str],
    img_df_ids: set[str] | None = None,
) -> pd.DataFrame:
    """
    Merge *new_scores* into *existing* scores.csv content.

    Strategy
    --------
    • Rows already in existing that also appear in new_scores → update only
      the non-original score columns (metric scores).  ``original_*`` columns
      are **never** overwritten — they are set once and then frozen.
    • Rows in new_scores that are not yet in existing → append them (including
      any ``original_*`` columns they carry).
    • Rows in existing that are not in new_scores → keep as-is.
    • If *img_df_ids* is provided, warn about any img_id in existing that is
      no longer present in the current image dataset.
    """
    if existing is None:
        return new_scores

    new_scores = new_scores.copy()
    new_scores["img_id"] = new_scores["img_id"].astype(str)

    merged = existing.copy()
    merged["img_id"] = merged["img_id"].astype(str)

    # Warn about images that disappeared from the dataset
    if img_df_ids is not None:
        missing_from_dataset = set(merged["img_id"]) - img_df_ids
        if missing_from_dataset:
            print(
                f"\n  ⚠  WARNING: {len(missing_from_dataset)} image(s) in scores.csv "
                f"are no longer present in the current image dataset and will be "
                f"kept as-is (no scores updated for them):\n"
                + "\n".join(f"      {i}" for i in sorted(missing_from_dataset))
            )

    # Ensure all score columns exist in merged
    for col in score_cols:
        if col not in merged.columns:
            merged[col] = float("nan")

    # Separate original_ cols (frozen) from regular metric cols (updatable)
    orig_cols    = [c for c in score_cols if c.startswith("original_")]
    metric_cols  = [c for c in score_cols if not c.startswith("original_")]

    # Update metric scores for existing rows (never touch original_ here)
    for col in metric_cols:
        if col not in new_scores.columns:
            continue
        update_src = new_scores.set_index("img_id")[col]
        common = merged["img_id"].isin(update_src.index)
        merged.loc[common, col] = merged.loc[common, "img_id"].map(update_src).values

    # Append truly new rows (they may carry original_ values — keep them)
    new_ids = new_scores[~new_scores["img_id"].isin(merged["img_id"])].copy()
    if len(new_ids) > 0:
        print(f"\n  ℹ  Adding {len(new_ids)} new image(s) to scores.csv.")
        # Ensure merged has all columns that new_ids introduces
        for col in new_ids.columns:
            if col not in merged.columns:
                merged[col] = float("nan")
        merged = pd.concat([merged, new_ids], ignore_index=True)

    return merged


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    from train import train
    from inference import run
    from model import get_model_filename

    n = len(METRICS)

    question_ids_list = _broadcast_filter(QUESTION_IDS,  n, "QUESTION_IDS")
    img_types_list    = _broadcast_filter(IMG_TYPES,     n, "IMG_TYPES")
    scenarios_list    = _broadcast_filter(SCENARIOS,     n, "SCENARIOS")
    checkpoints       = _resolve_checkpoints(FROM_CHECKPOINTS, n)
    print("=" * 60)
    print(f"Device            : {DEVICE}")
    print(f"Metrics to train  : {METRICS}")
    print(f"Question IDs      : {question_ids_list}")
    print(f"IMG types         : {img_types_list}")
    print(f"Scenarios         : {scenarios_list}")
    print(f"From checkpoints  : {checkpoints}")
    print(f"Freeze ViT        : {FREEZE_VIT}")
    print(f"Model folder      : {MODEL_FOLDER}")
    print(f"Resume training   : {RESUME_TRAINING}")
    print(f"Early stopping    : patience={EARLY_STOPPING_PATIENCE}, "
          f"min_delta={EARLY_STOPPING_MIN_DELTA}")
    print("=" * 60)

    out_dir = Path(MODEL_FOLDER)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / "scores.csv"

    # ----------------------------------------------------------------
    # 1. Load raw CSVs once
    # ----------------------------------------------------------------
    print("\n[1/3] Loading data …")
    human_df_raw = pd.read_csv(HUMAN_DF_PATH)
    img_df_raw   = pd.read_csv(IMG_DF_PATH)
    img_df_raw["img_id"] = img_df_raw["img_id"].astype(str)
    print(f"  human_df : {len(human_df_raw):,} rows")
    print(f"  img_df   : {len(img_df_raw):,} rows")

    # Load existing scores.csv once up front
    existing_scores = _load_scores_csv(out_csv)

    # ----------------------------------------------------------------
    # 2. Train — one model per metric
    # ----------------------------------------------------------------
    print("\n[2/3] Training …")

    # Track which metrics were skipped (model existed + RESUME_TRAINING=False)
    skipped_metrics: set[str] = set()

    for metric, q_ids, img_types, scenarios, from_ckpt in zip(
        METRICS, question_ids_list, img_types_list, scenarios_list, checkpoints
    ):
        print(f"\n{'─' * 60}")
        print(f"  Metric       : {metric}")
        print(f"  Question IDs : {q_ids}")
        print(f"  IMG types    : {img_types}")
        print(f"  Scenarios    : {scenarios}")
        print(f"  Checkpoint   : {from_ckpt!r}")
        print(f"{'─' * 60}")

        model_path = os.path.join(MODEL_FOLDER, get_model_filename(metric))
        model_exists = os.path.isfile(model_path)
        # ── Model already exists ────────────────────────────────────────────
        if model_exists:
            if not RESUME_TRAINING:
                print(
                    f"  ⚡ Model exists and RESUME_TRAINING=False "
                    f"— skipping training for '{metric}'."
                )
                skipped_metrics.add(metric)
                continue  # skip straight to inference

            print(
                f"  ↩  Model exists and RESUME_TRAINING=True "
                f"— loading existing model and continuing training."
            )
            # Pass the saved model path as from_checkpoint so train() loads it
            # directly (train() already handles arbitrary .pth paths when they
            # are not a "metric name" — we use the full path here).
            # History CSV will be appended by train(), not overwritten.
            from_ckpt_for_train = model_path  # .pth suffix → train.py loads it directly

        # ── Fresh training ───────────────────────────────────────────────────
        else:
            from_ckpt_for_train = from_ckpt

        # --- Filter human_df for this metric ---
        human_df = human_df_raw[
            (human_df_raw["type"].str.upper() == "AB")
            & (human_df_raw["answer"].isin(["A", "B"]))
        ].copy()

        human_df = _filter_df(human_df, "question_id", q_ids)
        human_df = _filter_df(human_df, "img_type",    img_types)
        human_df = _filter_df(human_df, "scenario",    scenarios)
        human_df = human_df.reset_index(drop=True)

        print(f"  AB rows after filtering: {len(human_df):,}")

        if len(human_df) == 0:
            raise ValueError(
                f"No AB rows found for metric '{metric}' after filtering. "
                "Check QUESTION_IDS, IMG_TYPES, and SCENARIOS."
            )

        # Restrict img_df to IDs present in the filtered human_df pairs
        valid_ids = (
            pd.concat([human_df["img_id_A"], human_df["img_id_B"]])
            .astype(str)
            .unique()
            .tolist()
        )
        img_df = img_df_raw[img_df_raw["img_id"].isin(valid_ids)].reset_index(drop=True)
        print(f"  Images used for training: {len(img_df):,}")

        saved_path = train(
            human_df=human_df,
            img_df=img_df,
            metric=metric,
            model_folder=MODEL_FOLDER,
            from_checkpoint=from_ckpt_for_train,
            vit_weights=VIT_WEIGHTS,
            freeze_vit=FREEZE_VIT,
            img_base_dir=IMG_BASE_DIR,
            epochs=EPOCHS,
            batch_size=BATCH_SIZE,
            lr=LEARNING_RATE,
            val_split=VAL_SPLIT,
            num_workers=NUM_WORKERS,
            device=DEVICE,
            pretrained_model_dir=PRETRAINED_MODEL_DIR,
            early_stopping_patience=EARLY_STOPPING_PATIENCE,
            early_stopping_min_delta=EARLY_STOPPING_MIN_DELTA,
        )
        print(f"  ✓ Saved → {saved_path}")

    # ----------------------------------------------------------------
    # 3. Inference — score images filtered by IMG_TYPES + SCENARIOS
    # ----------------------------------------------------------------
    print("\n[3/3] Running inference …")

    full_image_gdf = build_image_gdf(img_df_raw, IMG_BASE_DIR)

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
        # For the metric score column: skip images already scored in existing_scores
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
                mc_passes=MC_PASSES,
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
                    mc_passes=MC_PASSES,
                )
                # Build the columns to merge: score + uncertainty (if present)
                orig_unc_col = f"uncertainty_{orig_col}"   # uncertainty_original_{metric}
                src_unc_col  = f"uncertainty_{from_ckpt}"  # what run() named it

                merge_assign = {orig_col: tmp[from_ckpt].values}
                if MC_PASSES > 1 and src_unc_col in tmp.columns:
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

    # ----------------------------------------------------------------
    # 4. Upsert into existing scores.csv and save
    # ----------------------------------------------------------------
    all_score_cols = METRICS + [
        f"original_{m}"
        for m, ckpt in zip(METRICS, checkpoints)
        if ckpt is not None and m not in skipped_metrics
    ]
    # Add MC-Dropout uncertainty columns when they were produced
    if MC_PASSES > 1:
        all_score_cols += [f"uncertainty_{m}" for m in METRICS]
        all_score_cols += [
            f"uncertainty_original_{m}"
            for m, ckpt in zip(METRICS, checkpoints)
            if ckpt is not None and m not in skipped_metrics
        ]
    all_score_cols = [c for c in all_score_cols if c in run_result.columns]

    final_scores = _upsert_scores(
        existing_scores,
        run_result,
        all_score_cols,
        img_df_ids=set(img_df_raw["img_id"].astype(str)),
    )
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