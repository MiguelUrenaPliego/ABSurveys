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

    img_id, path, img_type, scenario, incompatible_ids, …

    Only ``img_id`` and ``path`` columns are consumed.
    Paths are resolved relative to ``img_base_dir``.

Training objective
------------------
Each AB pair is treated as two independent (image, label) samples:
  - image_A with label 1  (preferred)  and image_B with label 0  (not preferred)
  … when the human chose A, and vice-versa when the human chose B.

This turns pairwise preference data into standard 2-class classification.

Outputs (all written to model_folder)
--------------------------------------
  {metric}.pth              – best checkpoint by val accuracy (default for inference)
  {metric}_epochN.pth       – per-epoch checkpoint saved after every epoch
  {metric}_history.csv      – per-epoch metrics: loss, accuracy, score_mean, score_std
  {metric}_curves.jpg       – loss + accuracy curves (updated each epoch)
  {metric}_calibration.jpg  – score mean + std curves (updated each epoch)

Public API
----------
``train(human_df, img_df, metric, model_folder, …)``
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
from torch.utils.data import DataLoader, Dataset, random_split
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
# Plotting / history helpers
# ---------------------------------------------------------------------------

def _save_history(history: list[dict], csv_path: str) -> None:
    """Append epoch rows to the history CSV (creates it on first call)."""
    df = pd.DataFrame(history)
    write_header = not os.path.isfile(csv_path)
    df.to_csv(csv_path, mode="a", header=write_header, index=False)


def _save_epoch_checkpoint(
    model: nn.Module,
    model_folder: str,
    metric: str,
    global_epoch: int,
) -> str:
    """
    Save a per-epoch checkpoint named ``{metric}_epoch{N}.pth``.

    These checkpoints let you pick the best epoch *after* training by
    inspecting the calibration columns in the history CSV, rather than
    being locked into whichever epoch had the highest pairwise val_acc.

    Returns the path the file was saved to.
    """
    core = model.module if isinstance(model, nn.DataParallel) else model
    epoch_path = os.path.join(model_folder, f"{metric}_epoch{global_epoch}.pth")
    torch.save(core, epoch_path)
    return epoch_path


def _plot_curves(csv_path: str, jpg_path: str, metric: str) -> None:
    """
    Read the full history CSV and save a 2-panel figure:
      left  – train vs val loss
      right – train vs val accuracy

    The figure is overwritten after every epoch so you can inspect it
    mid-training without waiting for the run to finish.

    Each training run is plotted as a continuous line; runs are separated by
    detecting where the epoch counter resets (or by a ``run`` column if
    present in the CSV).  A sequential global step index is used on the x-axis
    so epochs from multiple resumed runs appear in order.
    """
    df = pd.read_csv(csv_path)

    # Assign a monotonically increasing global step across all runs so that
    # resumed runs appear as a continuous x-axis rather than folding back.
    df = df.reset_index(drop=True)
    df["_step"] = range(len(df))

    # Detect run boundaries: wherever epoch does NOT increment by 1
    # (handles epoch=0 baseline followed by epoch=1, or a reset after resume).
    boundaries = [0] + (
        df.index[df["epoch"].diff().fillna(0) <= 0].tolist()
    ) + [len(df)]
    boundaries = sorted(set(boundaries))

    fig, (ax_loss, ax_acc) = plt.subplots(1, 2, figsize=(12, 4))
    fig.suptitle(f"Training curves — {metric}", fontsize=13)

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    for run_idx, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:])):
        seg = df.iloc[start:end]
        color = colors[run_idx % len(colors)]
        label_sfx = f" (run {run_idx + 1})" if len(boundaries) > 2 else ""

        ax_loss.plot(seg["_step"], seg["train_loss"],
                     label=f"train{label_sfx}", color=color,
                     marker="o", markersize=3, linestyle="-")
        ax_loss.plot(seg["_step"], seg["val_loss"],
                     label=f"val{label_sfx}", color=color,
                     marker="o", markersize=3, linestyle="--")

        ax_acc.plot(seg["_step"], seg["train_acc"],
                    label=f"train{label_sfx}", color=color,
                    marker="o", markersize=3, linestyle="-")
        ax_acc.plot(seg["_step"], seg["val_acc"],
                    label=f"val{label_sfx}", color=color,
                    marker="o", markersize=3, linestyle="--")

    # X-tick labels: show epoch value from the CSV
    tick_steps  = df["_step"].tolist()
    tick_labels = df["epoch"].astype(str).tolist()

    for ax in (ax_loss, ax_acc):
        ax.set_xticks(tick_steps)
        ax.set_xticklabels(tick_labels, fontsize=6, rotation=45, ha="right")
        ax.set_xlabel("Epoch")
        ax.legend(fontsize=7)
        ax.grid(alpha=0.3)

    ax_loss.set_ylabel("Cross-entropy loss")
    ax_loss.set_title("Loss")
    ax_acc.set_ylabel("Accuracy")
    ax_acc.set_title("Accuracy")
    ax_acc.set_ylim(0, 1)

    fig.tight_layout()
    fig.savefig(jpg_path, dpi=120, format="jpeg")
    plt.close(fig)

    # ── Optional second figure: score calibration ────────────────────────────
    # Only generated when the calibration columns are present (i.e. after at
    # least one real training epoch).
    if "val_score_mean" not in df.columns:
        return

    cal_jpg = jpg_path.replace("_curves.jpg", "_calibration.jpg")
    fig2, (ax_mean, ax_std) = plt.subplots(1, 2, figsize=(12, 4))
    fig2.suptitle(
        f"Score calibration — {metric}\n"
        "(ideal: mean ≈ 5.0 and std ≥ 2.0 → scores spread across 0–10)",
        fontsize=11,
    )

    for run_idx, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:])):
        seg = df.iloc[start:end].dropna(subset=["val_score_mean"])
        if seg.empty:
            continue
        color = colors[run_idx % len(colors)]
        label_sfx = f" (run {run_idx + 1})" if len(boundaries) > 2 else ""

        ax_mean.plot(seg["_step"], seg["val_score_mean"],
                     label=f"val mean{label_sfx}", color=color,
                     marker="o", markersize=3)
        ax_std.plot(seg["_step"], seg["val_score_std"],
                    label=f"val std{label_sfx}", color=color,
                    marker="o", markersize=3)

    ax_mean.axhline(5.0, color="gray", linestyle="--", linewidth=0.8,
                    label="ideal mean (5.0)")
    ax_mean.set_ylabel("Mean predicted score (0–10)")
    ax_mean.set_title("Score mean (want ≈ 5)")
    ax_mean.set_ylim(0, 10)
    ax_mean.legend(fontsize=7)
    ax_mean.grid(alpha=0.3)

    ax_std.axhline(2.0, color="gray", linestyle="--", linewidth=0.8,
                   label="min useful std (2.0)")
    ax_std.set_ylabel("Std-dev of predicted scores")
    ax_std.set_title("Score spread (want ≥ 2)")
    ax_std.set_ylim(0, 5)
    ax_std.legend(fontsize=7)
    ax_std.grid(alpha=0.3)

    tick_steps_cal  = seg["_step"].tolist() if not seg.empty else []
    tick_labels_cal = seg["epoch"].astype(str).tolist() if not seg.empty else []
    for ax in (ax_mean, ax_std):
        ax.set_xticks(tick_steps_cal)
        ax.set_xticklabels(tick_labels_cal, fontsize=6, rotation=45, ha="right")
        ax.set_xlabel("Epoch")

    fig2.tight_layout()
    fig2.savefig(cal_jpg, dpi=120, format="jpeg")
    plt.close(fig2)


# ---------------------------------------------------------------------------
# Epoch-0 baseline evaluation
# ---------------------------------------------------------------------------

def _evaluate_epoch_zero(
    model: nn.Module,
    train_loader: "DataLoader",
    val_loader: "DataLoader",
    criterion: nn.Module,
    device: torch.device,
) -> dict:
    """
    Evaluate the model *before* any gradient updates (epoch 0).

    Returns a history row dict with the same keys as regular epoch rows,
    but with ``epoch=0``.
    """
    model.eval()

    def _run_loader(loader):
        total_loss = 0.0
        correct = 0
        total = 0
        with torch.no_grad():
            for images, labels in loader:
                images, labels = images.to(device), labels.to(device)
                outputs = model(images)
                loss = criterion(outputs, labels)
                total_loss += loss.item() * images.size(0)
                preds = outputs.argmax(dim=1)
                correct += (preds == labels).sum().item()
                total += images.size(0)
        return total_loss / total, correct / total

    train_loss, train_acc = _run_loader(train_loader)
    val_loss,   val_acc   = _run_loader(val_loader)

    print(
        f"Epoch   0/? (baseline, no training)  "
        f"train_loss={train_loss:.4f}  train_acc={train_acc:.4f}  "
        f"val_loss={val_loss:.4f}  val_acc={val_acc:.4f}"
    )

    return {
        "epoch":      0,
        "train_loss": round(train_loss, 6),
        "train_acc":  round(train_acc,  6),
        "val_loss":   round(val_loss,   6),
        "val_acc":    round(val_acc,    6),
    }


# ---------------------------------------------------------------------------
# Score calibration helper
# ---------------------------------------------------------------------------

def _compute_score_calibration(
    model: nn.Module,
    val_loader: "DataLoader",
    device: torch.device,
) -> tuple[float, float]:
    """
    Run the validation set through the model and compute the mean and
    standard deviation of the predicted 0–10 scores.

    The score formula mirrors inference.py:
        score = softmax(logits)[1] * 10

    Why this matters
    ----------------
    Pairwise val_acc tells you only whether the model ranks images
    correctly relative to each other.  It says nothing about whether
    the absolute score values are useful.  A model that outputs 5.01
    for "preferred" and 4.99 for "not preferred" can achieve 100 %
    val_acc while giving every image a score between 4.9 and 5.1 —
    completely useless as a 0–10 descriptor.

    Good calibration looks like:
      - mean  ≈ 5.0  (the scale is being used symmetrically)
      - std   ≥ 2.0  (scores are spread enough to discriminate images)

    Returns
    -------
    (mean_score, std_score)  — floats on the 0–10 scale.
    """
    model.eval()
    scores: list[float] = []

    with torch.no_grad():
        for images, _ in val_loader:
            images = images.to(device)
            logits = model(images)
            probs = torch.softmax(logits, dim=1)[:, 1]   # P(preferred)
            scores.extend((probs * 10).cpu().tolist())

    if not scores:
        return float("nan"), float("nan")

    import statistics
    mean = statistics.mean(scores)
    std  = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    return round(mean, 4), round(std, 4)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class PairwisePerceptionDataset(Dataset):
    """
    Converts AB pairwise judgements into (image_tensor, label) pairs.

    For each row where the human chose A:
        (img_A, 1),  (img_B, 0)
    For each row where the human chose B:
        (img_A, 0),  (img_B, 1)

    Images that cannot be opened are silently skipped.
    """

    def __init__(
        self,
        human_df: pd.DataFrame,
        img_df: pd.DataFrame,
        img_base_dir: str = "",
    ) -> None:
        ab = human_df.copy()

        # Build img_id → absolute path look-up
        path_map: dict[str, str] = {}
        for _, row in img_df[["img_id", "path"]].dropna().iterrows():
            raw = str(row["path"])
            full = (
                raw
                if os.path.isabs(raw)
                else os.path.join(img_base_dir, raw)
            )
            path_map[str(row["img_id"])] = full

        # Expand pairs into (path, label) samples
        samples: list[tuple[str, int]] = []
        for _, row in ab.iterrows():
            id_a = str(row.get("img_id_A", ""))
            id_b = str(row.get("img_id_B", ""))
            answer = str(row["answer"]).upper()

            path_a = path_map.get(id_a)
            path_b = path_map.get(id_b)

            if path_a and path_b:
                if answer == "A":
                    samples.append((path_a, 1))
                    samples.append((path_b, 0))
                else:  # "B"
                    samples.append((path_a, 0))
                    samples.append((path_b, 1))

        # Filter to existing files
        self.samples = [
            (p, lbl) for p, lbl in samples if os.path.isfile(p)
        ]

        if not self.samples:
            raise ValueError(
                "No valid image pairs found. Check that img_base_dir is "
                "correct and that img_df paths point to existing files."
            )

        print(f"Dataset: {len(self.samples)} (image, label) samples from "
              f"{len(ab)} AB rows.")

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, int]:
        path, label = self.samples[idx]
        img = Image.open(path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        return IMAGE_TRANSFORM(img), label


# ---------------------------------------------------------------------------
# Model initialisation helpers
# ---------------------------------------------------------------------------

def _build_fresh_model(
    vit_weights: bool,
    freeze_vit: bool,
    num_classes: int = 2,
) -> Net:
    """Return a newly constructed Net (backbone + MLP head)."""
    return Net(
        num_classes=num_classes,
        vit_weights=vit_weights,
        freeze_vit=freeze_vit,
    )


def _load_pretrained_model(
    from_checkpoint: str,
    model_dir: str,
    device: torch.device,
    freeze_vit: bool,
) -> Net:
    """
    Load a full model checkpoint (.pth) and optionally freeze the backbone.

    ``from_checkpoint`` can be:
    - A **file path** ending in ``.pth`` (relative or absolute).  Resolved
      against the current working directory if relative.  Used both for the
      resume-training flow and for pointing at any locally saved model.
    - A **metric name** string (e.g. ``"safety"``).  The corresponding .pth is
      looked up in *model_dir*; if absent and the metric is one of the built-in
      HF ones the snapshot is downloaded automatically.

    The distinction is made purely by suffix (``.pth`` → path, anything else →
    metric name), so callers never need to worry about absolute vs. relative
    paths.
    """
    if from_checkpoint.endswith(".pth"):
        # Treat as a direct file path (relative or absolute).
        model_path = str(Path(from_checkpoint).resolve())
    else:
        # Treat as a metric name and look it up in model_dir.
        filename = get_model_filename(from_checkpoint)
        model_path = str(Path(model_dir, filename).resolve())
        # Auto-download known default metrics from HF if missing.
        if not os.path.isfile(model_path) and from_checkpoint in DEFAULT_METRICS:
            print(f"Model for '{from_checkpoint}' not found locally – downloading from HF …")
            from huggingface_hub import snapshot_download
            Path(model_dir).mkdir(parents=True, exist_ok=True)
            snapshot_download(
                repo_id=HF_REPO_ID,
                allow_patterns=["*.pth", "README.md"],
                local_dir=model_dir,
            )

    if not os.path.isfile(model_path):
        raise FileNotFoundError(
            f"Checkpoint file not found: {model_path}\n"
            f"Provide a valid metric name from {DEFAULT_METRICS} or a path "
            "to a locally saved .pth file."
        )

    import model as _model_module
    sys.modules["Model_01"] = _model_module

    with torch.serialization.safe_globals([Net]):
        loaded = torch.load(model_path, map_location=device, weights_only=False)

    # Ensure it's a bare Net (unwrap DataParallel if needed)
    if isinstance(loaded, nn.DataParallel):
        loaded = loaded.module

    if freeze_vit:
        loaded.freeze_backbone()
    else:
        loaded.unfreeze_backbone()

    return loaded


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------

def train(
    human_df: pd.DataFrame,
    img_df: pd.DataFrame,
    metric: str,
    model_folder: str,
    *,
    # Initialisation options:
    from_checkpoint: Optional[str] = None,  # metric name → fine-tune; None → fresh model
    vit_weights: bool = True,               # use ImageNet ViT weights (fresh only)
    freeze_vit: bool = False,               # freeze ViT backbone; only train MLP head
    # Source dir for resolving relative paths in img_df
    img_base_dir: str = "",
    # Hyperparameters
    epochs: int = 10,
    batch_size: int = 16,
    lr: float = 1e-4,
    val_split: float = 0.2,
    num_workers: int = 4,
    device: Optional[torch.device] = None,
    # Early stopping
    early_stopping_patience: int = 4,
    early_stopping_min_delta: float = 0.005,
    # Where to look for (and optionally download) pretrained .pth files
    pretrained_model_dir: str = "models",
) -> str:
    """
    Fine-tune (or train from scratch) a perception model.

    Parameters
    ----------
    human_df:
        Pairwise judgement data (see module docstring).
    img_df:
        Image registry with at least ``img_id`` and ``path`` columns.
    metric:
        Name of the perception metric being trained (e.g. ``"walk"``).
        The saved file will be ``{model_folder}/{metric_lowercase}.pth``.
    model_folder:
        Directory where the trained model and training artefacts are saved:
          - ``{metric}.pth``             best model by val_acc (default for inference)
          - ``{metric}_epochN.pth``      per-epoch checkpoint saved after every epoch
          - ``{metric}_history.csv``     per-epoch metrics: loss, acc, score_mean, score_std
          - ``{metric}_curves.jpg``      loss + accuracy plot (updated each epoch)
          - ``{metric}_calibration.jpg`` score mean + std plot (updated each epoch)

        After training, inspect ``{metric}_history.csv`` (or the calibration plot)
        and copy the epoch with the best score_std + score_mean≈5.0 over
        ``{metric}.pth`` if you want calibration over ranking accuracy.
    from_checkpoint:
        *None* → build a fresh model.
        A metric name string (e.g. ``"safety"``) → load that checkpoint from
        *pretrained_model_dir* (downloading from HF if needed) and fine-tune.
    vit_weights:
        Only used when ``from_checkpoint=None``.  If *True* the ViT backbone
        is initialised with ImageNet pretrained weights.
    freeze_vit:
        Freeze the ViT backbone so only the MLP head is updated.
    img_base_dir:
        Base directory used to resolve relative paths in ``img_df["path"]``.
    epochs, batch_size, lr, val_split, num_workers:
        Standard training hyperparameters.
    device:
        Torch device.  Defaults to CUDA if available, else CPU.
    early_stopping_patience:
        Epochs without meaningful improvement before stopping.
    early_stopping_min_delta:
        Minimum val_acc gain that counts as improvement (absolute, 0–1 scale).
    pretrained_model_dir:
        Where to look for (and optionally download) pretrained .pth files.

    Returns
    -------
    str
        Absolute path to the saved model file.
    """
    if device is None:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    print(f"Training device: {device}")

    # ------------------------------------------------------------------ data
    dataset = PairwisePerceptionDataset(
        human_df=human_df,
        img_df=img_df,
        img_base_dir=img_base_dir,
    )

    n_val = max(1, int(len(dataset) * val_split))
    n_train = len(dataset) - n_val
    train_set, val_set = random_split(
        dataset,
        [n_train, n_val],
        generator=torch.Generator().manual_seed(42),
    )

    train_loader = DataLoader(
        train_set,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=(device.type == "cuda"),
    )
    val_loader = DataLoader(
        val_set,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=(device.type == "cuda"),
    )

    print(f"Train samples: {n_train}  |  Val samples: {n_val}")

    # ----------------------------------------------------------------- model
    if from_checkpoint is not None:
        print(f"Loading pretrained checkpoint from metric='{from_checkpoint}' …")
        model = _load_pretrained_model(
            from_checkpoint=from_checkpoint,
            model_dir=pretrained_model_dir,
            device=device,
            freeze_vit=freeze_vit,
        )
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
    save_path  = os.path.join(model_folder, get_model_filename(metric))
    csv_path   = os.path.join(model_folder, f"{metric}_history.csv")
    jpg_path   = os.path.join(model_folder, f"{metric}_curves.jpg")

    # Remove legacy .png if it exists so we don't leave stale files around.
    png_path_legacy = os.path.join(model_folder, f"{metric}_curves.png")
    if os.path.isfile(png_path_legacy):
        os.remove(png_path_legacy)
        print(f"  Removed legacy {png_path_legacy} (replaced by .jpg)")

    # Determine the epoch offset for resumed runs:
    # read the last epoch number from the history CSV so we can continue
    # numbering from there rather than restarting at 1.
    epoch_offset: int = 0
    is_resuming = os.path.isfile(csv_path)
    if is_resuming:
        try:
            hist_existing = pd.read_csv(csv_path)
            if not hist_existing.empty and "epoch" in hist_existing.columns:
                epoch_offset = int(hist_existing["epoch"].max())
                print(f"  Resuming from epoch {epoch_offset}; "
                      f"new epochs will be numbered {epoch_offset + 1}+")
        except Exception:
            pass  # malformed CSV — start from 0

    # --------------------------------------------------------------- training
    criterion     = nn.CrossEntropyLoss()
    optimizer     = torch.optim.Adam(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=lr,
    )
    early_stop    = EarlyStopping(
        patience=early_stopping_patience,
        min_delta=early_stopping_min_delta,
    )
    best_val_acc: float = 0.0
    history: list[dict] = []

    # ── Epoch 0: baseline evaluation before any training ──────────────────
    # Only record epoch 0 on the very first run (epoch_offset == 0) so that
    # resumed runs don't duplicate it.
    if epoch_offset == 0:
        epoch0_row = _evaluate_epoch_zero(
            model=model,
            train_loader=train_loader,
            val_loader=val_loader,
            criterion=criterion,
            device=device,
        )
        # No calibration at epoch 0 — model weights haven't moved yet.
        # Fill with NaN so the CSV schema is consistent from the first row.
        epoch0_row["val_score_mean"] = float("nan")
        epoch0_row["val_score_std"]  = float("nan")
        history.append(epoch0_row)
        _save_history([epoch0_row], csv_path)
        _plot_curves(csv_path, jpg_path, metric)

    for epoch in range(1, epochs + 1):
        global_epoch = epoch_offset + epoch   # continuous epoch number for CSV

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

            train_loss    += loss.item() * images.size(0)
            preds          = outputs.argmax(dim=1)
            train_correct += (preds == labels).sum().item()
            train_total   += images.size(0)

        train_loss /= train_total
        train_acc   = train_correct / train_total

        # ── validate ───────────────────────────────────────────────────────
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
                val_loss    += loss.item() * images.size(0)
                preds        = outputs.argmax(dim=1)
                val_correct += (preds == labels).sum().item()
                val_total   += images.size(0)

        val_loss /= val_total
        val_acc   = val_correct / val_total

        # ── score calibration (runs a second val pass, no gradients) ───────
        val_score_mean, val_score_std = _compute_score_calibration(
            model=model,
            val_loader=val_loader,
            device=device,
        )

        print(
            f"Epoch {global_epoch:>3}  "
            f"train_loss={train_loss:.4f}  train_acc={train_acc:.4f}  "
            f"val_loss={val_loss:.4f}  val_acc={val_acc:.4f}  "
            f"score_mean={val_score_mean:.2f}  score_std={val_score_std:.2f}"
        )

        # ── save per-epoch checkpoint ──────────────────────────────────────
        # Every epoch is saved so you can pick the best checkpoint *after*
        # training by inspecting val_score_std in the history CSV, rather
        # than being locked into whichever epoch had the highest val_acc
        # (which says nothing about score spread/calibration).
        epoch_path = _save_epoch_checkpoint(
            model=model,
            model_folder=model_folder,
            metric=metric,
            global_epoch=global_epoch,
        )

        # ── record & plot ──────────────────────────────────────────────────
        row = {
            "epoch":           global_epoch,
            "train_loss":      round(train_loss,      6),
            "train_acc":       round(train_acc,       6),
            "val_loss":        round(val_loss,        6),
            "val_acc":         round(val_acc,         6),
            "val_score_mean":  round(val_score_mean,  4),
            "val_score_std":   round(val_score_std,   4),
        }
        history.append(row)
        _save_history([row], csv_path)
        _plot_curves(csv_path, jpg_path, metric)

        # ── update {metric}.pth when val_acc improves ─────────────────────
        # This "best accuracy" file is what inference.py loads by default.
        # You can override it by copying any epoch checkpoint over it, e.g.:
        #   cp walk_epoch5.pth walk.pth
        if val_acc >= best_val_acc:
            best_val_acc = val_acc
            core = model.module if isinstance(model, nn.DataParallel) else model
            torch.save(core, save_path)
            print(f"  ✓ New best val_acc={best_val_acc:.4f} → saved to {save_path}")

        # ── early stopping ─────────────────────────────────────────────────
        if early_stop.step(val_acc):
            break

    # ── End-of-training summary ────────────────────────────────────────────
    # Print the full per-epoch calibration table so you can decide which
    # epoch checkpoint to promote to {metric}.pth for inference.
    print(f"\n{'─'*72}")
    print(f"Training complete.  Best val_acc={best_val_acc:.4f}")
    print(f"\nPer-epoch calibration summary (val set):")
    print(f"  {'epoch':>5}  {'val_acc':>8}  {'score_mean':>10}  {'score_std':>10}  checkpoint")
    for r in history:
        if r["epoch"] == 0:
            continue   # skip baseline row — no checkpoint was saved
        ep  = r["epoch"]
        acc = r.get("val_acc", float("nan"))
        mn  = r.get("val_score_mean", float("nan"))
        sd  = r.get("val_score_std",  float("nan"))
        ckpt = os.path.join(model_folder, f"{metric}_epoch{ep}.pth")
        flag = "  ← best val_acc" if abs(acc - best_val_acc) < 1e-9 else ""
        print(f"  {ep:>5}  {acc:>8.4f}  {mn:>10.2f}  {sd:>10.2f}  {ckpt}{flag}")
    print(
        f"\nTo use a different epoch for inference, copy it over the default:\n"
        f"  cp {metric}_epochN.pth {os.path.basename(save_path)}\n"
        f"  (look for epochs with score_std ≥ 2.0 and score_mean ≈ 5.0)\n"
        f"{'─'*72}"
    )
    print(f"Default model → {os.path.abspath(save_path)}")
    print(f"History       → {os.path.abspath(csv_path)}")
    print(f"Plot          → {os.path.abspath(jpg_path)}")
    return os.path.abspath(save_path)