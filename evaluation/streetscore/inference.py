# inference.py
# coding: utf-8

"""
Inference utilities for street perception scoring.

Public API
----------
run(gdf, metric, model_dir, ...)  →  GeoDataFrame with score column added
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import torch
import torch.nn as nn
from huggingface_hub import snapshot_download
from PIL import Image
from tqdm import tqdm

from model import (
    HF_REPO_ID,
    IMAGE_TRANSFORM,
    Net,
    get_model_filename,
)


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def download_pretrained_models(model_dir: str) -> None:
    """
    Download the pretrained .pth files from Hugging Face into *model_dir*.

    Safe to call even when the files already exist (HF hub is idempotent).
    """
    Path(model_dir).mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=HF_REPO_ID,
        allow_patterns=["*.pth", "README.md"],
        local_dir=model_dir,
    )


def load_model(
    metric: str,
    model_dir: str,
    device: torch.device,
) -> nn.Module:
    """
    Load a trained perception model from *model_dir*.

    The file is expected at ``{model_dir}/{metric_lowercase}.pth``.
    The .pth files saved by *train.py* store the full ``Net`` object, so we
    register ``Net`` as a safe global before deserialisation.

    Args:
        metric:    Metric name (e.g. ``"safety"``).
        model_dir: Directory that contains the .pth files.
        device:    Torch device to map the model to.

    Returns:
        Loaded model in eval mode.
    """
    model_path = os.path.join(model_dir, get_model_filename(metric))

    if not os.path.isfile(model_path):
        raise FileNotFoundError(
            f"Model file not found: {model_path}\n"
            f"Run download_pretrained_models('{model_dir}') first, or check "
            "the model_dir / metric name."
        )

    # Support .pth files saved both from this project and from the original
    # repo (which used ``Model_01.Net``).
    import model as _model_module
    sys.modules["Model_01"] = _model_module

    with torch.serialization.safe_globals([Net]):
        loaded = torch.load(
            model_path,
            map_location=device,
            weights_only=False,
        )

    if torch.cuda.device_count() > 1:
        loaded = nn.DataParallel(loaded)

    loaded = loaded.to(device)
    loaded.eval()
    return loaded


# ---------------------------------------------------------------------------
# Single-image prediction
# ---------------------------------------------------------------------------

def predict_image_score(
    model: nn.Module,
    image_path: str,
    device: torch.device,
    mc_passes: int = 0,
) -> tuple[float, float | None]:
    """
    Return a perception score (and optionally its uncertainty) for one image.

    Score
    -----
    The score is ``softmax(logits)[1] * 10``, i.e. the probability the model
    assigns to the image being "preferred", scaled to 0–10.

    Uncertainty via MC-Dropout
    --------------------------
    When ``mc_passes > 1`` the model is run *mc_passes* times with dropout
    layers kept active (``model.train()`` mode, but no gradients).  Each pass
    produces a slightly different score because dropout randomly masks units.
    The standard deviation of those scores is the uncertainty estimate: a
    small value means the model is confident; a large value means the model
    is uncertain (the score could easily be higher or lower).

    When ``mc_passes <= 1`` a single deterministic forward pass is used and
    ``None`` is returned for the uncertainty.

    Args:
        model:       Loaded Net in eval mode (dropout still active if mc_passes>1).
        image_path:  Absolute path to the image file.
        device:      Torch device.
        mc_passes:   Number of stochastic forward passes for uncertainty
                     estimation.  0 or 1 → deterministic, no uncertainty.

    Returns:
        (score, uncertainty) where score ∈ [0, 10] and uncertainty is the
        standard deviation of MC-Dropout scores (also on the 0–10 scale),
        or None when mc_passes ≤ 1.
    """
    image = Image.open(image_path)
    if image.mode != "RGB":
        image = image.convert("RGB")

    tensor = IMAGE_TRANSFORM(image).unsqueeze(0).to(device)

    if mc_passes <= 1:
        # Deterministic single pass
        with torch.no_grad():
            logits = model(tensor)
            prob = torch.softmax(logits, dim=1)[0][1].item()
        return round(prob * 10, 2), None

    # MC-Dropout: enable dropout by switching to train() mode,
    # but disable gradient computation to keep memory/speed reasonable.
    model.train()
    mc_scores: list[float] = []
    with torch.no_grad():
        for _ in range(mc_passes):
            logits = model(tensor)
            prob = torch.softmax(logits, dim=1)[0][1].item()
            mc_scores.append(prob * 10)
    model.eval()   # restore eval mode after MC passes

    mean_score = sum(mc_scores) / len(mc_scores)
    # Population std over the MC passes
    variance = sum((s - mean_score) ** 2 for s in mc_scores) / len(mc_scores)
    std_score = variance ** 0.5

    return round(mean_score, 2), round(std_score, 3)


# ---------------------------------------------------------------------------
# Batch inference on a GeoDataFrame / DataFrame
# ---------------------------------------------------------------------------

def run(
    gdf: gpd.GeoDataFrame,
    metrics: str | Iterable[str],
    model_dir: str,
    image_column: str = "path",
    device: torch.device | None = None,
    download_missing_models: bool = False,
    mc_passes: int = 0,
) -> gpd.GeoDataFrame:
    """
    Score all images in *gdf* for one or more perception metrics.

    Args:
        gdf:
            GeoDataFrame (or plain DataFrame) with at least one column
            containing absolute image paths.
        metrics:
            Single metric name or list of metric names to score.
        model_dir:
            Directory containing the .pth model files.
        image_column:
            Column in *gdf* that holds the image paths.
        device:
            Torch device.  Defaults to CUDA if available, else CPU.
        download_missing_models:
            When *True* the pretrained HF models are downloaded into
            *model_dir* before inference.  Only useful for the default
            metrics; custom models must be present locally.
        mc_passes:
            Number of MC-Dropout forward passes per image for uncertainty
            estimation.  0 or 1 → deterministic (no uncertainty column).
            Recommended: 20–50 for a good uncertainty estimate; note that
            this multiplies inference time by *mc_passes*.

    Returns:
        Copy of *gdf* with one or two extra columns per metric:

        - ``{metric}``              – predicted score (float, 0–10, or None)
        - ``uncertainty_{metric}``  – MC-Dropout std (float, 0–10 scale),
                                      only present when ``mc_passes > 1``.

        A high uncertainty value means the model was inconsistent across
        dropout passes and the score should be treated with more caution.
    """
    if device is None:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    print(f"Inference device: {device}  |  mc_passes={mc_passes}")

    if isinstance(metrics, str):
        metrics = [metrics]
    metrics = list(metrics)

    if download_missing_models:
        from model import DEFAULT_METRICS
        non_default = [m for m in metrics if m not in DEFAULT_METRICS]
        if non_default:
            raise ValueError(
                f"download_missing_models=True only works for the built-in default "
                f"metrics {DEFAULT_METRICS}.  The following metrics are custom and "
                f"must be present locally: {non_default}"
            )
        missing = [
            m for m in metrics
            if not os.path.isfile(os.path.join(model_dir, get_model_filename(m)))
        ]
        if missing:
            print(f"Downloading pretrained models for: {missing} …")
            download_pretrained_models(model_dir)
        else:
            print("All pretrained models already present locally — skipping download.")

    result = gdf.copy()

    for metric in metrics:
        print(f"\n######### {metric} #########")
        model = load_model(metric=metric, model_dir=model_dir, device=device)

        scores:        list[float | None] = []
        uncertainties: list[float | None] = []

        for img_path in tqdm(result[image_column], desc=metric):
            if (
                img_path is None
                or not isinstance(img_path, str)
                or not os.path.isfile(img_path)
            ):
                scores.append(None)
                uncertainties.append(None)
                continue

            score, unc = predict_image_score(
                model=model,
                image_path=img_path,
                device=device,
                mc_passes=mc_passes,
            )
            scores.append(score)
            uncertainties.append(unc)

        result[metric] = scores

        if mc_passes > 1:
            result[f"uncertainty_{metric}"] = uncertainties

        print(f"{metric} inference complete.")

    return result