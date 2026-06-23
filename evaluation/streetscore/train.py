# train.py
# coding: utf-8

"""
Fine-tuning street-perception ViT models from pairwise AB-comparison data.

Training data format
--------------------
Two DataFrames are required:

``human_df``  – pairwise human judgements ::

    user_id, scenario, language, question_id, type, answer,
    img_id_A, img_id_B, img_type, info

    ``answer`` must be ``"A"`` or ``"B"``.

``img_df``  – image registry ::

    img_id, path, _base_dir, img_type, scenario, incompatible_ids, …

    Only ``img_id``, ``path``, and ``_base_dir`` columns are consumed.
    ``_base_dir`` is stamped automatically by ``_load_image_dataframes``
    in main.py and holds the parent directory of the source CSV/JSON file,
    so relative paths in each file are resolved against the right directory
    even when images come from multiple source files.

Training objective
------------------
Each AB pair is treated as two independent (image, label) samples:
  - image_A with label 1  (preferred)  and image_B with label 0  (not preferred)
  … when the human chose A, and vice-versa when the human chose B.

This turns pairwise preference data into standard 2-class classification.

Outputs (all written to model_folder)
--------------------------------------
  {metric}.pth          – best checkpoint (saved whenever the smart logic fires)
  {metric}_history.csv  – per-epoch metrics: loss, accuracy, score_mean, score_std,
                          val_uncertainty (MC-Dropout mean std, when mc_passes > 1)
  {metric}_curves.jpg   – loss / accuracy / uncertainty curves (updated each epoch)

Checkpoint saving logic
-----------------------
A checkpoint {metric}.pth is saved when:
  1. val_acc does NOT decrease by more than CHECKPOINT_SAVE_TOLERANCE (%)
  2. AND val_score_std does NOT decrease by more than CHECKPOINT_SAVE_TOLERANCE (%)
  3. AND val_score_mean does NOT decrease by more than CHECKPOINT_SAVE_TOLERANCE (%)
  4. AND val_uncertainty does NOT decrease by more than CHECKPOINT_SAVE_TOLERANCE (%)
  5. AND at least one of these four metrics improved

When CHECKPOINT_SAVE_TOLERANCE is None, the original behavior is used: save whenever
val_acc improves.

Public API
----------
``train(human_df, img_df, metric, model_folder, …, checkpoint_save_tolerance=5.0)``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — safe in all environments
import matplotlib.pyplot as plt
import pandas as pd
import torch
import torch.nn as nn
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from tqdm import tqdm

from model import (
    DEFAULT_METRICS,
    HF_REPO_ID,
    IMAGE_TRANSFORM,
    Net,
    get_model_filename,
)


# ---------------------------------------------------------------------------
# Early-stopping helper
# ---------------------------------------------------------------------------

class EarlyStopping:
    """
    Stop training when validation accuracy has not improved by more than
    *min_delta* for *patience* consecutive epochs.

    The criterion is deliberately conservative: we only stop when the model
    has clearly plateaued, not just had one bad epoch.

    Args:
        patience:  Number of epochs without meaningful improvement before
                   stopping.  Default 4 — generous enough for fine-tuning
                   small datasets where progress can be slow.
        min_delta: Minimum absolute improvement in val_acc that counts as
                   "progress".  Default 0.005 (0.5 pp) — below this the
                   gain is noise, not signal.
    """

    def __init__(self, patience: int = 4, min_delta: float = 0.005) -> None:
        self.patience = patience
        self.min_delta = min_delta
        self._best: float = -1.0
        self._wait: int = 0

    def step(self, val_acc: float) -> bool:
        """
        Call once per epoch with the current validation accuracy.

        Returns *True* when training should stop.
        """
        if val_acc >= self._best + self.min_delta:
            self._best = val_acc
            self._wait = 0
        else:
            self._wait += 1

        if self._wait >= self.patience:
            print(
                f"\n  Early stopping: val_acc has not improved by >{self.min_delta:.3f} "
                f"for {self.patience} consecutive epochs.  Best val_acc={self._best:.4f}"
            )
            return True
        return False


# ---------------------------------------------------------------------------
# Smart checkpoint saving helper
# ---------------------------------------------------------------------------

def _should_save_checkpoint(
    current_metrics: dict,
    best_metrics: dict,
    tolerance: float | None,
) -> tuple[bool, str]:
    """
    Determine if a checkpoint should be saved based on multi-metric tolerance.

    Args:
        current_metrics:  Current epoch's metrics (val_acc, val_score_std,
                         val_score_mean, val_uncertainty)
        best_metrics:     Best values seen so far for each metric
        tolerance:        Tolerance in percent (e.g., 5 for 5%). If None,
                         only save when val_acc improves (original behavior).

    Returns:
        (should_save: bool, reason: str)
    """
    if tolerance is None:
        # Original behavior: save if val_acc improves
        if current_metrics["val_acc"] > best_metrics.get("val_acc", -1):
            return True, "val_acc improved"
        return False, "val_acc did not improve"

    # Check all four metrics against tolerance
    metrics_to_check = ["val_acc", "val_score_std", "val_score_mean", "val_uncertainty"]
    any_improved = False
    any_regressed = False
    regression_reasons = []

    for metric_name in metrics_to_check:
        current = current_metrics.get(metric_name)
        best = best_metrics.get(metric_name)

        # Skip missing metrics (e.g., val_uncertainty when mc_passes <= 1)
        if current is None or best is None or (isinstance(current, float) and pd.isna(current)):
            continue

        # For most metrics, higher is better; for uncertainty, lower is better
        if metric_name == "val_uncertainty":
            # Lower uncertainty is better
            if current < best:
                any_improved = True
            else:
                pct_change = ((current - best) / abs(best)) * 100 if best != 0 else 0
                if pct_change > tolerance:
                    any_regressed = True
                    regression_reasons.append(f"{metric_name} +{pct_change:.1f}%")
        else:
            # Higher is better (acc, score_std, score_mean)
            if current > best:
                any_improved = True
            else:
                pct_change = ((best - current) / abs(best)) * 100 if best != 0 else 0
                if pct_change > tolerance:
                    any_regressed = True
                    regression_reasons.append(f"{metric_name} -{pct_change:.1f}%")

    if any_regressed:
        reason = f"metrics regressed: {', '.join(regression_reasons)}"
        return False, reason

    if any_improved:
        return True, "at least one metric improved"

    return False, "no metrics improved"


# ---------------------------------------------------------------------------
# Plotting / history helpers
# ---------------------------------------------------------------------------

def _save_history(history: list[dict], csv_path: str) -> None:
    """Append epoch rows to the history CSV (creates it on first call)."""
    df = pd.DataFrame(history)
    write_header = not os.path.isfile(csv_path)
    df.to_csv(csv_path, mode="a", header=write_header, index=False)



def _plot_curves(csv_path: str, jpg_path: str, metric: str) -> None:
    """
    Read the full history CSV and save a figure with up to 3 panels:
      panel 1 – train vs val loss
      panel 2 – train vs val accuracy
      panel 3 – val MC-Dropout uncertainty (mean per-image std, 0–10 scale)
                only rendered when the ``val_uncertainty`` column is present.

    Train lines: solid  (–)   Val lines: dashed (--)
    Each resumed run gets its own colour; the legend makes the train/val
    distinction clear via linestyle and label prefix.

    The figure is overwritten after every epoch so you can monitor progress
    mid-training.
    """
    df = pd.read_csv(csv_path)
    df = df.reset_index(drop=True)
    df["_step"] = range(len(df))

    # Detect run boundaries: wherever epoch does NOT increment by 1.
    boundaries = [0] + (
        df.index[df["epoch"].diff().fillna(0) <= 0].tolist()
    ) + [len(df)]
    boundaries = sorted(set(boundaries))

    has_uncertainty = (
        "val_uncertainty" in df.columns
        and df["val_uncertainty"].notna().any()
    )

    ncols = 3 if has_uncertainty else 2
    fig, axes = plt.subplots(1, ncols, figsize=(6 * ncols, 4))
    ax_loss, ax_acc = axes[0], axes[1]
    ax_unc = axes[2] if has_uncertainty else None

    fig.suptitle(f"Training curves — {metric}", fontsize=13)

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    for run_idx, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:])):
        seg = df.iloc[start:end]
        color = colors[run_idx % len(colors)]
        run_label = f" run {run_idx + 1}" if len(boundaries) > 2 else ""

        # ── Loss ──────────────────────────────────────────────────────────
        ax_loss.plot(seg["_step"], seg["train_loss"],
                     label=f"train{run_label}", color=color,
                     linestyle="-", marker="o", markersize=3)
        ax_loss.plot(seg["_step"], seg["val_loss"],
                     label=f"val{run_label}", color=color,
                     linestyle="--", marker="s", markersize=3)

        # ── Accuracy ──────────────────────────────────────────────────────
        ax_acc.plot(seg["_step"], seg["train_acc"],
                    label=f"train{run_label}", color=color,
                    linestyle="-", marker="o", markersize=3)
        ax_acc.plot(seg["_step"], seg["val_acc"],
                    label=f"val{run_label}", color=color,
                    linestyle="--", marker="s", markersize=3)

        # ── Uncertainty (val only — no train-time uncertainty) ────────────
        if has_uncertainty:
            unc_seg = seg.dropna(subset=["val_uncertainty"])
            if not unc_seg.empty:
                ax_unc.plot(unc_seg["_step"], unc_seg["val_uncertainty"],
                            label=f"val{run_label}", color=color,
                            linestyle="--", marker="s", markersize=3)

    ax_loss.set_xlabel("Epoch")
    ax_loss.set_ylabel("Loss")
    ax_loss.legend(fontsize=9)
    ax_loss.grid(True, alpha=0.3)

    ax_acc.set_xlabel("Epoch")
    ax_acc.set_ylabel("Accuracy")
    ax_acc.legend(fontsize=9)
    ax_acc.grid(True, alpha=0.3)

    if ax_unc is not None:
        ax_unc.set_xlabel("Epoch")
        ax_unc.set_ylabel("MC-Dropout Uncertainty (std)")
        ax_unc.legend(fontsize=9)
        ax_unc.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(jpg_path, dpi=100, bbox_inches="tight")
    plt.close()


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class ABPairDataset(Dataset):
    """
    Converts AB-pair human judgments into single-image classification.

    For each AB pair where human chose A (or B), we create two independent
    training samples:
      (img_A, label=1) and (img_B, label=0)   if human chose A
      (img_A, label=0) and (img_B, label=1)   if human chose B

    This makes the dataset size 2× the number of AB pairs.

    Path resolution
    ---------------
    If ``img_df`` contains a ``_base_dir`` column (stamped by
    ``_load_image_dataframes`` in main.py), each image path is resolved
    relative to its own source file's parent directory.  Otherwise the path
    is used as-is (absolute paths always take precedence).
    """

    def __init__(
        self,
        human_df: pd.DataFrame,
        img_df: pd.DataFrame,
        img_base_dir: str = "",   # kept for backwards-compat; ignored when _base_dir column present
        transform=None,
    ) -> None:
        self.transform = transform or IMAGE_TRANSFORM

        # Build img_id → (path, base_dir) lookup
        has_base_dir_col = "_base_dir" in img_df.columns
        self.img_lookup: dict[str, tuple[str, str]] = {}
        for _, row in img_df.iterrows():
            iid = str(row["img_id"])
            path = str(row["path"])
            base = str(row["_base_dir"]) if has_base_dir_col else img_base_dir
            self.img_lookup[iid] = (path, base)

        # Expand AB pairs into (image_id, label) samples
        self.samples: list[tuple[str, int]] = []

        for _, row in human_df.iterrows():
            img_id_a = str(row["img_id_A"])
            img_id_b = str(row["img_id_B"])
            answer = row["answer"]

            if img_id_a not in self.img_lookup or img_id_b not in self.img_lookup:
                continue

            if answer == "A":
                self.samples.append((img_id_a, 1))
                self.samples.append((img_id_b, 0))
            elif answer == "B":
                self.samples.append((img_id_a, 0))
                self.samples.append((img_id_b, 1))

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, int]:
        img_id, label = self.samples[idx]
        path, base_dir = self.img_lookup[img_id]

        # Resolve to absolute path
        if not os.path.isabs(path):
            path = os.path.join(base_dir, path) if base_dir else path

        image = Image.open(path)
        if image.mode != "RGB":
            image = image.convert("RGB")

        return self.transform(image), label


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------

def _evaluate_epoch_zero(
    model: nn.Module,
    train_loader: DataLoader,
    val_loader: DataLoader,
    criterion: nn.Module,
    device: torch.device,
) -> dict:
    """Evaluate the model before any training (epoch 0)."""
    model.eval()

    # Train loss/acc
    train_loss = 0.0
    train_correct = 0
    train_total = 0

    with torch.no_grad():
        for images, labels in train_loader:
            images = images.to(device)
            labels = labels.to(device)
            outputs = model(images)
            loss = criterion(outputs, labels)
            train_loss += loss.item() * images.size(0)
            preds = outputs.argmax(dim=1)
            train_correct += (preds == labels).sum().item()
            train_total += images.size(0)

    train_loss /= train_total
    train_acc = train_correct / train_total

    # Val loss/acc
    val_loss = 0.0
    val_correct = 0
    val_total = 0

    with torch.no_grad():
        for images, labels in val_loader:
            images = images.to(device)
            labels = labels.to(device)
            outputs = model(images)
            loss = criterion(outputs, labels)
            val_loss += loss.item() * images.size(0)
            preds = outputs.argmax(dim=1)
            val_correct += (preds == labels).sum().item()
            val_total += images.size(0)

    val_loss /= val_total
    val_acc = val_correct / val_total

    return {
        "epoch": 0,
        "train_loss": round(train_loss, 6),
        "train_acc": round(train_acc, 6),
        "val_loss": round(val_loss, 6),
        "val_acc": round(val_acc, 6),
    }


def _compute_val_metrics(
    model: nn.Module,
    val_loader: DataLoader,
    device: torch.device,
    mc_passes: int = 0,
) -> tuple[float, float, float | None]:
    """
    Compute validation metrics: score_mean, score_std, and MC-Dropout uncertainty.

    Returns (val_score_mean, val_score_std, val_uncertainty).
    val_uncertainty is None when mc_passes <= 1.
    """
    model.eval()

    all_scores = []
    all_mc_stds = []

    with torch.no_grad():
        for images, labels in val_loader:
            images = images.to(device)

            if mc_passes <= 1:
                # Deterministic pass
                logits = model(images)
                probs = torch.softmax(logits, dim=1)[:, 1]  # P(preferred)
                scores = (probs * 10).cpu().numpy().tolist()
                all_scores.extend(scores)
            else:
                # MC-Dropout passes
                model.train()  # Enable dropout
                batch_mc_scores = []
                for _ in range(mc_passes):
                    logits = model(images)
                    probs = torch.softmax(logits, dim=1)[:, 1]
                    scores = (probs * 10).cpu().numpy()
                    batch_mc_scores.append(scores)
                model.eval()  # Back to eval

                batch_mc_scores = np.array(batch_mc_scores)  # (mc_passes, batch_size)
                batch_means = batch_mc_scores.mean(axis=0)
                batch_stds = batch_mc_scores.std(axis=0)

                all_scores.extend(batch_means)
                all_mc_stds.extend(batch_stds)

    val_score_mean = float(np.mean(all_scores))
    val_score_std = float(np.std(all_scores))
    val_uncertainty = float(np.mean(all_mc_stds)) if all_mc_stds else None

    return val_score_mean, val_score_std, val_uncertainty


# ---------------------------------------------------------------------------
# Model builders
# ---------------------------------------------------------------------------

def _build_fresh_model(
    vit_weights: bool = True,
    freeze_vit: bool = False,
) -> Net:
    """Build a fresh Net model."""
    return Net(
        num_classes=2,
        vit_weights=vit_weights,
        freeze_vit=freeze_vit,
    )


def _load_model_for_resume(
    model_path: str,
    device: torch.device,
    freeze_vit: bool = False,
) -> Net:
    """Load an existing model for resuming training."""
    import model as _model_module
    sys.modules["Model_01"] = _model_module

    with torch.serialization.safe_globals([Net]):
        core = torch.load(model_path, map_location=device, weights_only=False)

    # Apply freeze if needed
    if freeze_vit:
        if isinstance(core, nn.DataParallel):
            core.module.freeze_backbone()
        else:
            core.freeze_backbone()

    return core


# ---------------------------------------------------------------------------
# Main training function
# ---------------------------------------------------------------------------

def train(
    human_df: pd.DataFrame,
    img_df: pd.DataFrame,
    metric: str,
    model_folder: str,
    val_human_df: pd.DataFrame | None = None,
    val_img_df: pd.DataFrame | None = None,
    from_checkpoint: str | None = None,
    vit_weights: bool = True,
    freeze_vit: bool = False,
    epochs: int = 10,
    batch_size: int = 16,
    lr: float = 1e-4,
    num_workers: int = 4,
    device: torch.device | None = None,
    pretrained_model_dir: str = "models/default_models",
    early_stopping_patience: int = 4,
    early_stopping_min_delta: float = 0.005,
    mc_passes: int = 0,
    checkpoint_save_tolerance: float | None = 5.0,
) -> str:
    """
    Fine-tune a perception model on AB-survey data.

    The validation set is determined entirely at the image-registry level
    (via IMG_VAL_PATHS in main.py) and passed in as *val_human_df* /
    *val_img_df*.  There is no further random split inside this function.

    Image path resolution
    ---------------------
    Each row in *img_df* (and *val_img_df*) is expected to carry a
    ``_base_dir`` column stamped by ``_load_image_dataframes`` in main.py.
    Relative ``path`` values are resolved against that per-row directory, so
    images from different source CSV files are each resolved against their
    own file's parent directory.  Absolute paths are used as-is.

    Args:
        human_df:                   AB-pair human judgments for training
        img_df:                     Image registry for training images
        metric:                     Metric name (e.g., "walk")
        model_folder:               Output directory
        val_human_df:               AB-pair human judgments for validation.
                                    When None (or empty) validation is skipped
                                    and early-stopping is disabled.
        val_img_df:                 Image registry for validation images.
                                    When None, *img_df* is reused (safe when
                                    val_human_df references a disjoint subset
                                    of the same image pool).
        from_checkpoint:            Metric to load as starting checkpoint (e.g., "safety")
        vit_weights:                Use ImageNet ViT weights
        freeze_vit:                 Freeze backbone, train MLP head only
        epochs:                     Max training epochs
        batch_size:                 Batch size
        lr:                         Learning rate
        num_workers:                DataLoader workers
        device:                     Torch device
        pretrained_model_dir:       Directory with pre-trained checkpoints
        early_stopping_patience:    Early stopping patience
        early_stopping_min_delta:   Early stopping min delta
        mc_passes:                  MC-Dropout passes for uncertainty
        checkpoint_save_tolerance:  Tolerance (%) for saving checkpoints; None to disable

    Returns:
        Path to the saved model file.
    """
    import numpy as np

    if device is None:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    print(f"Inference device: {device}  |  mc_passes={mc_passes}")

    # ----------------------------------------------------------- dataset / loaders
    train_dataset = ABPairDataset(
        human_df=human_df,
        img_df=img_df,
        transform=IMAGE_TRANSFORM,
    )
    print(f"  Train dataset size: {len(train_dataset):,} samples "
          f"(from {len(human_df):,} AB pairs)")

    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
    )

    # Build validation loader from the explicitly supplied val set (if any).
    has_val = (
        val_human_df is not None
        and len(val_human_df) > 0
    )
    if has_val:
        # val_img_df falls back to the training image registry when not supplied;
        # safe because ABPairDataset only indexes the IDs it actually needs.
        effective_val_img_df = val_img_df if val_img_df is not None else img_df
        val_dataset = ABPairDataset(
            human_df=val_human_df,
            img_df=effective_val_img_df,
            transform=IMAGE_TRANSFORM,
        )
        print(f"  Val   dataset size: {len(val_dataset):,} samples "
              f"(from {len(val_human_df):,} AB pairs)")
        val_loader = DataLoader(
            val_dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=num_workers,
        )
    else:
        val_loader = None
        print("  No validation set supplied — skipping validation and early stopping.")

    # ----------------------------------------------------------- model initialization
    model_path = os.path.join(model_folder, get_model_filename(metric))

    is_resuming = os.path.isfile(model_path)
    if is_resuming:
        print(f"Loading existing model {model_path} for resuming …")
        model = _load_model_for_resume(model_path, device, freeze_vit=freeze_vit)
    elif from_checkpoint is not None:
        if not isinstance(from_checkpoint, str):
            raise TypeError(
                f"from_checkpoint must be a str (metric name) or None, "
                f"got {type(from_checkpoint).__name__}: {from_checkpoint!r}. "
                "Check that FROM_CHECKPOINTS in main.py contains plain strings, "
                "not lists."
            )
        print(f"Loading checkpoint from {from_checkpoint} …")
        from_ckpt_path = os.path.join(
            pretrained_model_dir,
            f"{from_checkpoint.lower()}.pth",
        )
        if not os.path.isfile(from_ckpt_path):
            raise FileNotFoundError(
                f"Checkpoint not found: {from_ckpt_path}\n"
                f"Run download_pretrained_models('{pretrained_model_dir}') first."
            )
        import model as _model_module
        sys.modules["Model_01"] = _model_module
        with torch.serialization.safe_globals([Net]):
            model = torch.load(from_ckpt_path, map_location=device, weights_only=False)
        if freeze_vit:
            if isinstance(model, nn.DataParallel):
                model.module.freeze_backbone()
            else:
                model.freeze_backbone()
    else:
        print(
            f"Building fresh model "
            f"(vit_weights={vit_weights}, freeze_vit={freeze_vit}) …"
        )
        model = _build_fresh_model(
            vit_weights=vit_weights,
            freeze_vit=freeze_vit,
        )

    if torch.cuda.device_count() > 1:
        model = nn.DataParallel(model)
    model = model.to(device)

    # ----------------------------------------------------------- output paths
    Path(model_folder).mkdir(parents=True, exist_ok=True)
    save_path = os.path.join(model_folder, get_model_filename(metric))
    csv_path = os.path.join(model_folder, f"{metric}_history.csv")
    jpg_path = os.path.join(model_folder, f"{metric}_curves.jpg")

    # Remove legacy .png if it exists
    png_path_legacy = os.path.join(model_folder, f"{metric}_curves.png")
    if os.path.isfile(png_path_legacy):
        os.remove(png_path_legacy)
        print(f"  Removed legacy {png_path_legacy} (replaced by .jpg)")

    # Determine the epoch offset for resumed runs
    epoch_offset: int = 0
    if is_resuming:
        try:
            hist_existing = pd.read_csv(csv_path)
            if not hist_existing.empty and "epoch" in hist_existing.columns:
                epoch_offset = int(hist_existing["epoch"].max())
                print(f"  Resuming from epoch {epoch_offset}; "
                      f"new epochs will be numbered {epoch_offset + 1}+")
        except Exception:
            pass

    # --------------------------------------------------------------- training
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=lr,
    )
    early_stop = EarlyStopping(
        patience=early_stopping_patience,
        min_delta=early_stopping_min_delta,
    )
    best_val_acc: float = 0.0
    best_metrics: dict = {}
    history: list[dict] = []

    # ── Epoch 0: baseline evaluation before any training ──────────────────
    if epoch_offset == 0:
        if val_loader is not None:
            epoch0_row = _evaluate_epoch_zero(
                model=model,
                train_loader=train_loader,
                val_loader=val_loader,
                criterion=criterion,
                device=device,
            )
        else:
            # No val set: record train baseline only, fill val columns with nan
            epoch0_row = _evaluate_epoch_zero(
                model=model,
                train_loader=train_loader,
                val_loader=train_loader,   # dummy — val columns overwritten below
                criterion=criterion,
                device=device,
            )
            epoch0_row["val_loss"] = float("nan")
            epoch0_row["val_acc"] = float("nan")
        epoch0_row["val_score_mean"] = float("nan")
        epoch0_row["val_score_std"] = float("nan")
        epoch0_row["val_uncertainty"] = float("nan")
        history.append(epoch0_row)
        _save_history([epoch0_row], csv_path)
        _plot_curves(csv_path, jpg_path, metric)

        best_metrics = {
            "val_acc": epoch0_row["val_acc"],
            "val_score_std": float("nan"),
            "val_score_mean": float("nan"),
            "val_uncertainty": float("nan"),
        }

    # Training loop
    for epoch in range(1, epochs + 1):
        global_epoch = epoch_offset + epoch

        # ── train ──────────────────────────────────────────────────────────
        model.train()
        train_loss = 0.0
        train_correct = 0
        train_total = 0

        for images, labels in tqdm(train_loader, desc=f"Epoch {global_epoch} [train]"):
            images = images.to(device)
            labels = labels.to(device)
            optimizer.zero_grad()
            outputs = model(images)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            train_loss += loss.item() * images.size(0)
            preds = outputs.argmax(dim=1)
            train_correct += (preds == labels).sum().item()
            train_total += images.size(0)

        train_loss /= train_total
        train_acc = train_correct / train_total

        # ── validate ───────────────────────────────────────────────────────
        if val_loader is not None:
            model.eval()
            val_loss = 0.0
            val_correct = 0
            val_total = 0

            with torch.no_grad():
                for images, labels in tqdm(val_loader, desc=f"Epoch {global_epoch} [val]  "):
                    images = images.to(device)
                    labels = labels.to(device)
                    outputs = model(images)
                    loss = criterion(outputs, labels)
                    val_loss += loss.item() * images.size(0)
                    preds = outputs.argmax(dim=1)
                    val_correct += (preds == labels).sum().item()
                    val_total += images.size(0)

            val_loss /= val_total
            val_acc = val_correct / val_total

            # ── score calibration + MC-Dropout uncertainty ─────────────────
            val_score_mean, val_score_std, val_uncertainty = _compute_val_metrics(
                model=model,
                val_loader=val_loader,
                device=device,
                mc_passes=mc_passes,
            )
        else:
            val_loss = float("nan")
            val_acc = float("nan")
            val_score_mean = float("nan")
            val_score_std = float("nan")
            val_uncertainty = None

        # ── print ──────────────────────────────────────────────────────────
        unc_str = (
            f"  val_uncertainty={val_uncertainty:.3f}"
            if val_uncertainty is not None
            else ""
        )
        if val_loader is not None:
            print(
                f"Epoch {global_epoch:>3}  "
                f"train_loss={train_loss:.4f}  train_acc={train_acc:.4f}  "
                f"val_loss={val_loss:.4f}  val_acc={val_acc:.4f}  "
                f"score_mean={val_score_mean:.2f}  score_std={val_score_std:.2f}"
                f"{unc_str}"
            )
        else:
            print(
                f"Epoch {global_epoch:>3}  "
                f"train_loss={train_loss:.4f}  train_acc={train_acc:.4f}  "
                f"(no validation set)"
            )

        # ── record & plot ──────────────────────────────────────────────────
        row: dict = {
            "epoch": global_epoch,
            "train_loss": round(train_loss, 6),
            "train_acc": round(train_acc, 6),
            "val_loss": round(val_loss, 6),
            "val_acc": round(val_acc, 6),
            "val_score_mean": round(val_score_mean, 4),
            "val_score_std": round(val_score_std, 4),
            "val_uncertainty": (
                round(val_uncertainty, 4)
                if val_uncertainty is not None
                else float("nan")
            ),
        }
        history.append(row)
        _save_history([row], csv_path)
        _plot_curves(csv_path, jpg_path, metric)

        # ── smart checkpoint saving ────────────────────────────────────────
        if val_loader is not None:
            current_metrics = {
                "val_acc": val_acc,
                "val_score_std": val_score_std,
                "val_score_mean": val_score_mean,
                "val_uncertainty": val_uncertainty,
            }

            should_save, reason = _should_save_checkpoint(
                current_metrics,
                best_metrics,
                checkpoint_save_tolerance,
            )

            if should_save:
                best_val_acc = val_acc
                best_metrics = current_metrics.copy()
                core = model.module if isinstance(model, nn.DataParallel) else model
                torch.save(core, save_path)
                print(f"  ✓ Checkpoint saved: {reason}")
                print(f"    (val_acc={val_acc:.4f}, score_std={val_score_std:.2f}, "
                      f"uncertainty={val_uncertainty if val_uncertainty is not None else 'N/A'})")
            else:
                print(f"  ✗ Not saving: {reason}")

            # ── early stopping ─────────────────────────────────────────────
            if early_stop.step(val_acc):
                break
        else:
            # No validation set: always save the latest checkpoint.
            core = model.module if isinstance(model, nn.DataParallel) else model
            torch.save(core, save_path)
            print(f"  ✓ Checkpoint saved (no val set — saving every epoch)")

    # ── End-of-training summary ────────────────────────────────────────────
    use_unc = mc_passes > 1
    hdr_unc = f"  {'val_unc':>8}" if use_unc else ""
    print(f"\n{'─'*80}")
    print(f"Training complete.")
    print(f"\nPer-epoch summary (val set):")
    print(
        f"  {'epoch':>5}  {'val_acc':>8}  {'score_mean':>10}  "
        f"{'score_std':>9}{hdr_unc}  checkpoint"
    )
    for r in history:
        if r["epoch"] == 0:
            continue
        ep = r["epoch"]
        acc = r.get("val_acc", float("nan"))
        mn = r.get("val_score_mean", float("nan"))
        sd = r.get("val_score_std", float("nan"))
        unc = r.get("val_uncertainty", float("nan"))
        unc_col = f"  {unc:>8.3f}" if use_unc else ""
        print(
            f"  {ep:>5}  {acc:>8.4f}  {mn:>10.2f}  {sd:>9.2f}"
            f"{unc_col}"
        )

    print(f"\n{'─'*80}")
    print(f"Default model → {os.path.abspath(save_path)}")
    print(f"History       → {os.path.abspath(csv_path)}")
    print(f"Plot          → {os.path.abspath(jpg_path)}")
    return os.path.abspath(save_path)