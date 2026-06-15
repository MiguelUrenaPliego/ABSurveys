# streetscore.py
# coding: utf-8

"""
Street perception scoring using Vision Transformers.

This module predicts human perception metrics from street-level imagery.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import pandas as pd
import torch
import torch.nn as nn
from huggingface_hub import snapshot_download
from PIL import Image
from torchvision import transforms as T
from torchvision.models import ViT_B_16_Weights, vit_b_16
from tqdm import tqdm

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"


DEFAULT_METRICS = [
    "safety",
    "lively",
    "wealthy",
    "beautiful",
    "boring",
    "depressing",
]

MODEL_FILENAMES = {
    "safety": "safety.pth",
    "lively": "lively.pth",
    "wealthy": "wealthy.pth",
    "beautiful": "beautiful.pth",
    "boring": "boring.pth",
    "depressing": "depressing.pth",
}


IMAGE_TRANSFORM = T.Compose(
    [
        T.Resize((384, 384)),
        T.ToTensor(),
        T.Normalize(
            mean=[0.485, 0.456, 0.406],
            std=[0.229, 0.224, 0.225],
        ),
    ]
)


class Net(nn.Module):
    """
    Vision Transformer model for perception classification.
    """

    def __init__(self, num_classes: int) -> None:
        """
        Initialize the model.

        Args:
            num_classes: Number of output classes.
        """
        super().__init__()

        self.model = vit_b_16(
            weights=ViT_B_16_Weights.IMAGENET1K_SWAG_E2E_V1
        )

        num_features = self.model.heads.head.in_features

        self.model.heads.head = nn.Sequential(
            nn.Linear(num_features, 512),
            nn.ReLU(inplace=True),
            nn.Linear(512, 256),
            nn.ReLU(inplace=True),
            nn.Linear(256, num_classes),
        )

        nn.init.xavier_uniform_(self.model.heads.head[0].weight)
        nn.init.xavier_uniform_(self.model.heads.head[2].weight)
        nn.init.xavier_uniform_(self.model.heads.head[4].weight)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass.

        Args:
            x: Input tensor.

        Returns:
            Output tensor.
        """
        return self.model(x)


def predict_image_score(
    model: nn.Module,
    image_path: str,
    device: torch.device,
) -> float:
    """
    Predict a perception score for a single image.

    Args:
        model: Loaded PyTorch model.
        image_path: Path to image file.
        device: Torch device.

    Returns:
        Predicted score between 0 and 10.
    """
    image = Image.open(image_path)

    if image.mode != "RGB":
        image = image.convert("RGB")

    image_tensor = IMAGE_TRANSFORM(image)
    image_tensor = image_tensor.unsqueeze(0).to(device)

    with torch.no_grad():
        prediction = model(image_tensor)

        probabilities = torch.softmax(prediction, dim=1)

        score = probabilities[0][1].item()

    return round(score * 10, 2)


def download_models(model_dir: str) -> None:
    """
    Download StreetScore models from Hugging Face.

    Args:
        model_dir: Local model directory.
    """
    Path(model_dir).mkdir(parents=True, exist_ok=True)

    snapshot_download(
        repo_id="Jiani11/human-perception-place-pulse",
        allow_patterns=["*.pth", "README.md"],
        local_dir=model_dir,
    )


def load_model(
    metric: str,
    model_dir: str,
    device: torch.device,
) -> nn.Module:
    """
    Load a trained perception model.

    Args:
        metric: Perception metric name.
        model_dir: Directory containing model files.
        device: Torch device.

    Returns:
        Loaded model.
    """
    model_path = os.path.join(
        model_dir,
        MODEL_FILENAMES[metric],
    )

    sys.modules["Model_01"] = sys.modules[__name__]

    with torch.serialization.safe_globals([Net]):
        model = torch.load(
            model_path,
            map_location=device,
            weights_only=False,
        )

    if torch.cuda.device_count() > 1:
        model = nn.DataParallel(model)

    model = model.to(device)
    model.eval()

    return model


def run(
    gdf: gpd.GeoDataFrame,
    metrics: Iterable[str] = DEFAULT_METRICS,
    model_dir: str = "models",
    image_column: str = "path",
    download_missing_models: bool = True,
) -> gpd.GeoDataFrame:
    """
    Run StreetScore inference on a GeoDataFrame.

    Args:
        gdf: Input GeoDataFrame containing image paths.
        metrics: Perception metrics to predict.
        model_dir: Directory containing model weights.
        image_column: Column containing image paths.
        download_missing_models: Whether to download models automatically.

    Returns:
        GeoDataFrame with added metric columns.
    """
    device = torch.device(
        "cuda:0" if torch.cuda.is_available() else "cpu"
    )

    print(f"Using device: {device}")

    metrics = list(metrics)

    if download_missing_models:
        download_models(model_dir)

    result_gdf = gdf.copy()

    for metric in metrics:

        print(f"\n######### {metric} #########")

        model = load_model(
            metric=metric,
            model_dir=model_dir,
            device=device,
        )

        scores: list[float | None] = []

        for image_path in tqdm(result_gdf[image_column]):

            if (
                image_path is None
                or not isinstance(image_path, str)
                or not os.path.isfile(image_path)
            ):
                scores.append(None)
                continue

            score = predict_image_score(
                model=model,
                image_path=image_path,
                device=device,
            )

            scores.append(score)

        result_gdf[metric] = scores

        print(f"{metric} prediction complete.")

    return result_gdf
