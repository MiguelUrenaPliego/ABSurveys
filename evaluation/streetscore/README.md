# Street Perception Scoring — Usage Guide

Fine-tune Vision Transformer models on your own pairwise AB-survey data
and score any collection of street-level images on perception metrics such
as safety, walkability, liveliness, beauty, and more.

---

## File structure

```
.
├── main.py          # Entry point — all configuration lives here
├── train.py         # Training loop, dataset, early stopping, plotting
├── inference.py     # Scoring a GeoDataFrame of images (with uncertainty)
├── model.py         # Net architecture + shared constants
└── models/
    ├── default_models/          # Pre-trained HF checkpoints (auto-downloaded)
    │   ├── safety.pth
    │   ├── lively.pth
    │   └── …
    └── YourProject/             # Your fine-tuned models (MODEL_FOLDER)
        ├── walk.pth             # Best checkpoint (smart saving)
        ├── walk_history.csv     # Training log
        └── walk_curves.jpg      # Training plot
```

---

## Quick start

1. Edit the **CONFIG** block at the top of `main.py` (paths, metric names,
   hyperparameters).
2. Run:
   ```bash
   python main.py
   ```
   This will train a model for each metric in `METRICS`, then score every
   image in `img_df` and write results to `{MODEL_FOLDER}/scores.csv`.

---

## How training works — from AB surveys to a single-image model

### The survey data: pairwise AB comparisons

The training data comes from a crowdsourced survey where participants are
shown **two street images side by side (A and B)** and asked which one they
prefer on a given dimension (e.g. "Which street looks safer to walk on?").
This produces rows like:

```
user_id | question_id      | answer | img_id_A | img_id_B
--------+------------------+--------+----------+---------
u001    | walk-preference  | A      | img_042  | img_091
u002    | walk-preference  | B      | img_042  | img_091
```

Each row records one human's preference between two images.

### Converting pairs to single-image classification

The model is trained to predict whether an image is "preferred" or "not
preferred" in a given comparison.  Each AB row is **expanded into two
independent training samples**:

| If answer is A | Sample 1 | Sample 2 |
|----------------|----------|----------|
| image          | img_A    | img_B    |
| label          | 1 (preferred) | 0 (not preferred) |

And symmetrically when the answer is B.  This means a single AB row
contributes two training examples.  The model never sees both images from the
same pair at the same time — it learns to recognise visual features that
humans associate with being preferred, treating each image independently.

### Why this works

After seeing thousands of such examples, the model learns a mapping from
**image → probability of being preferred**.  At inference time this
probability is scaled to [0, 10] to give the final perception score.

Crucially, the relative ordering is preserved: if image X was preferred over
image Y in the training data, the model should assign X a higher score than Y.
The pairwise structure of the original survey thus survives in the final
absolute scores.

### The model architecture

The model (`Net` in `model.py`) is a **Vision Transformer ViT-B/16**
pre-trained on ImageNet, with the classification head replaced by a 3-layer
MLP:

```
ViT-B/16 backbone  →  Linear(768→512) → ReLU
                   →  Linear(512→256) → ReLU
                   →  Linear(256→2)          # 2 logits: [not-preferred, preferred]
```

The output logits are passed through `softmax`, and the probability of the
"preferred" class is the raw score. Multiplying by 10 gives the 0–10 scale.

When fine-tuning from a pre-trained checkpoint (`FROM_CHECKPOINTS`), the
backbone weights start from an already-specialised state and the MLP head is
adapted to your specific metric and local image distribution.  Setting
`FREEZE_VIT=True` freezes the backbone and only updates the MLP head, which
is faster and less likely to overfit on small datasets.

---

## The `original_{metric}` column

When `FROM_CHECKPOINTS` points to a pre-trained model (e.g. `"safety"`),
the pipeline runs inference **twice**:

1. **`{metric}`** — scores from your fine-tuned model.
2. **`original_{metric}`** — scores from the pre-trained checkpoint *before*
   any fine-tuning.

This lets you see directly how much fine-tuning changed the scores for each
image.  A large shift indicates the local context (your survey + images)
differs meaningfully from the original training distribution.  `original_` is
only produced the first time a metric is trained; on resumed runs it is
assumed to already be in `scores.csv`.

---

## Data loading and splitting

### Flexible file loading

**HUMAN_DF_PATHS**, **IMG_TRAIN_PATHS**, **IMG_VAL_PATHS**, and **IMG_TEST_PATHS**
(when given as file paths rather than a percentage) can each be:
- A single file path (string)
- A list of file paths (auto-concatenated)
- Supported formats: CSV, JSON, GeoJSON, Shapefile, GeoPackage, GeoParquet

**Example:**
```python
HUMAN_DF_PATHS = [
    "surveys/round1.csv",
    "surveys/round2.json",
]

IMG_TRAIN_PATHS = [
    "/data/city_a/images.csv",
    "/data/city_b/images.geojson",
]

IMG_VAL_PATHS = [
    "/data/city_c/val_images.csv",
    "/data/city_d/val_images.csv",
]
```

#### Automatic path resolution — no `IMG_BASE_DIR` needed

Each image CSV/JSON file may contain relative `path` values (e.g.
`"frames/img_001.jpg"`).  The pipeline automatically resolves them relative
to **the parent directory of the file they came from** — not a single global
base directory.  This means you can freely mix files from different locations:

```python
IMG_TRAIN_PATHS = [
    "/data/city_a/images.csv",   # paths in here resolve against /data/city_a/
    "/data/city_b/images.csv",   # paths in here resolve against /data/city_b/
]
```

Absolute paths in any file are always used as-is.  There is no `IMG_BASE_DIR`
config variable — path resolution is fully automatic and per-file.

### Train/val/test splits

**IMG_VAL_PATHS** and **IMG_TEST_PATHS** can each be:
- A file path (or list of paths) — loaded directly as that split
- An **integer 0–100** — treated as a percentage to carve out of the training pool

#### `IMG_TEST_PATHS` semantics

| Value | Behaviour |
|-------|-----------|
| `0` | No additional test split; only orphaned images (see below) go to test |
| `1–99` | Move this % of all images to test. Orphaned images are placed in test first and count toward this quota; labeled images fill the remainder |
| `100` | **Inference-only mode**: the test set contains *all* images, but the train set is **not reduced** — training is unaffected |

#### `IMG_VAL_PATHS` semantics

`IMG_VAL_PATHS` is always applied **after** the test split, and it operates
at the **AB-pair level**, not the image level.  For each metric (after
metric-specific question/type/scenario filtering), this percentage of the
eligible AB pairs is moved to the validation set.  `0` disables the val split.

This is the **only** validation split — there is no secondary split inside
the training loop.

**Examples:**
```python
# Percentage-based splits
IMG_TRAIN_PATHS = "images.csv"
IMG_VAL_PATHS  = 15   # 15% of filtered AB pairs → val, per metric
IMG_TEST_PATHS = 10   # 10% of all images → test

# Score every image without touching the training set
IMG_TEST_PATHS = 100  # test = all images, train intact

# File-based splits (still supported)
IMG_TRAIN_PATHS = "train_images.csv"
IMG_VAL_PATHS   = "val_images.csv"
IMG_TEST_PATHS  = "test_images.csv"

# Mixed: file for test, percentage for val
IMG_TRAIN_PATHS = "candidates.csv"
IMG_VAL_PATHS   = 15                   # 15% of AB pairs → val, per metric
IMG_TEST_PATHS  = "held_out_test.csv"  # separate file
```

### Orphaned image detection

Any image that appears in **IMG_TRAIN_PATHS** but does NOT appear in any AB
pair in **HUMAN_DF_PATHS** is considered **orphaned**.  Orphaned images are
always placed in the test set first and count toward the `IMG_TEST_PATHS`
percentage quota.  The pipeline prints a warning:

```
⚠️  WARNING: 234 images do NOT appear in any AB pair.
    They will be placed in the test set and count toward IMG_TEST_PATHS %.
```

This helps identify data quality issues early and ensures unlabeled images are
never silently mixed into the training or validation sets.

---

## Smart checkpoint saving

A checkpoint (the best model saved as `{metric}.pth`) is saved when:

1. **val_acc** does NOT decrease by more than **CHECKPOINT_SAVE_TOLERANCE** (%)
2. **AND val_score_std** does NOT decrease by more than **CHECKPOINT_SAVE_TOLERANCE** (%)
3. **AND val_score_mean** does NOT decrease by more than **CHECKPOINT_SAVE_TOLERANCE** (%)
4. **AND val_uncertainty** does NOT decrease by more than **CHECKPOINT_SAVE_TOLERANCE** (%)
5. **AND at least one of these metrics improved**

This prevents saving models that optimize one metric at the expense of others.
For example, a model might have higher accuracy but lower score spread 
(less discrimination) — the old behavior would save it; the new logic rejects it.

**Configuration:**
```python
# Tolerance in percent (recommended: 5-10%)
CHECKPOINT_SAVE_TOLERANCE = 5.0

# Set to None to disable (revert to saving only on val_acc improvement)
CHECKPOINT_SAVE_TOLERANCE = None
```

**Output during training:**
```
✓ Checkpoint saved: at least one metric improved
  (val_acc=0.7756, score_std=2.41, uncertainty=0.234)

✗ Not saving: metrics regressed: val_score_std -8.2%
```

---

## What the training metrics mean

All metrics are computed on the **validation set** (the percentage of AB pairs
held out via `IMG_VAL_PATHS`).  When no val set is configured (`IMG_VAL_PATHS=0`),
validation metrics are skipped, early stopping is disabled, and the checkpoint
is saved after every epoch.

### Loss (cross-entropy)

Standard classification loss.  Lower is better.  The loss is computed
on the single-image binary classification task (preferred vs. not preferred).

- **`train_loss`**: loss on the training split — directly what the model is
  minimising via gradient descent.
- **`val_loss`**: loss on the held-out validation split — the best proxy for
  generalisation.  If `val_loss` rises while `train_loss` keeps falling, the
  model is overfitting.

### Accuracy

The fraction of individual images classified correctly (label 1 predicted for
a preferred image, label 0 for a not-preferred image).

- **`train_acc`** / **`val_acc`**: 0–1 scale; higher is better.
- A model that always predicts the same class can reach 50 % acc, so anything
  below ~0.55 early in training indicates something is wrong.
- **Important caveat**: accuracy alone does not tell you whether the resulting
  0–10 scores are useful.  A model that outputs 5.01 for preferred images and
  4.99 for not-preferred images can achieve perfect accuracy while assigning
  every image a score between 4.9 and 5.1 — useless for ranking.

### Score calibration (val set)

Two additional metrics are computed each epoch by running the validation images
through the model in deterministic mode and recording the predicted 0–10 scores:

- **`val_score_mean`**: the mean score across all val images.
  - Ideal target: **≈ 5.0** — the model uses the scale symmetrically, half the
    images score above 5, half below.  A mean far from 5.0 indicates bias.
- **`val_score_std`**: the standard deviation of scores across val images.
  - Ideal target: **≥ 2.0** — scores are spread enough to meaningfully
    distinguish images.  A low std (e.g. 0.3) means every image scores near
    the same value, even if accuracy is high.

These two numbers together tell you whether your model is actually producing
useful 0–10 scores, or just ranking correctly while clustering near 5.

### Prediction uncertainty (MC-Dropout)

When `TRAINING_MC_PASSES > 1`, after each epoch the validation set is run
`TRAINING_MC_PASSES` times with **dropout layers kept active** (model in
`.train()` mode, no gradients).  Each pass produces slightly different scores
because dropout randomly deactivates neurons.

For each image:
```
uncertainty_i = std( score_pass_1, score_pass_2, …, score_pass_N )
```

The logged metric **`val_uncertainty`** is the mean of `uncertainty_i` across
all val images.  It is on the same 0–10 scale as the scores themselves.

Interpretation during training:

| Pattern | Meaning |
|---------|---------|
| High and **decreasing** each epoch | Model is converging — healthy |
| Low and stable (< 0.1) + low score_std | Model collapsed: confident but scores everything near 5 |
| High and **plateaued** for several epochs | Further training unlikely to help; consider stopping |
| Rises suddenly after many epochs | Overfitting — the model is becoming erratic on unseen images |

A plateaued `val_uncertainty` combined with a plateaued `val_acc` is a
stronger early-stopping signal than accuracy alone, because it means the
model's *confidence* has also stopped improving.

---

## Choosing the right checkpoint

`{metric}.pth` is saved whenever the smart multi-metric logic fires (see
above).  Only one file is ever written — no per-epoch copies are kept.

Use `{metric}_history.csv` (or `{metric}_curves.jpg`) to verify the saved
checkpoint is well-calibrated.  Good targets:

1. **`val_score_std ≥ 2.0`** — scores are spread across the scale.
2. **`val_score_mean ≈ 5.0`** — the scale is used symmetrically.
3. **`val_uncertainty` as low as possible** — the model is confident.
4. **`val_acc` high** — ranking is still correct.

If the saved checkpoint does not meet these targets, adjust
`CHECKPOINT_SAVE_TOLERANCE` or `EPOCHS` and retrain.

---

## Inference uncertainty (`uncertainty_{metric}`)

During inference (`inference.py → run()`), when `INFERENCE_MC_PASSES > 1`,
each image is scored `INFERENCE_MC_PASSES` times with dropout active.

Two columns are written to `scores.csv` per metric:

| Column | Description |
|--------|-------------|
| `{metric}` | Mean score across MC passes (0–10) |
| `uncertainty_{metric}` | Std of scores across MC passes (0–10 scale) |
| `original_{metric}` | Score from pre-trained baseline model |
| `uncertainty_original_{metric}` | Uncertainty of the baseline model's score |

A **low uncertainty** (e.g. < 0.2) means the model is confident: the score
would be essentially the same regardless of which dropout mask was applied.
A **high uncertainty** (e.g. > 1.5) means the score is sensitive to the
specific dropout mask and should be treated with more caution — perhaps the
image contains unusual content the model has not seen much of in training.

---

## Configuration reference (`main.py`)

### Data loading

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `HUMAN_DF_PATHS` | str or list[str] | — | Path(s) to human survey data (CSV/JSON) |
| `IMG_TRAIN_PATHS` | str or list[str] | — | Path(s) to training images |
| `IMG_VAL_PATHS` | str, list[str], or int 0–100 | `15` | Path(s) to val images, or % of filtered AB pairs (per metric) to move to val; `0` = no validation |
| `IMG_TEST_PATHS` | str, list[str], or int 0–100 | `15` | Path(s) to test images, or % of all images to move to test; `100` = all images, train untouched |

### Metrics and filtering

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `METRICS` | list[str] | `["walk"]` | Names of the metrics to train |
| `QUESTION_IDS` | str, list, or list-of-lists | — | Filter human_df by `question_id` |
| `IMG_TYPES` | str, list, or list-of-lists | — | Filter by `img_type` (train + inference) |
| `SCENARIOS` | str, list, or list-of-lists | — | Filter by `scenario` (train + inference) |
| `FROM_CHECKPOINTS` | str, list, or list-of-lists | — | Pre-trained metric to fine-tune from (or `None`) |

### Training and checkpointing

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `RESUME_TRAINING` | bool | `True` | Continue training if `.pth` already exists |
| `CHECKPOINT_SAVE_TOLERANCE` | float or None | 5.0 | Tolerance (%) for multi-metric checkpoint saving |
| `MODEL_FOLDER` | str | — | Output directory for your models + scores |
| `PRETRAINED_MODEL_DIR` | str | `"models/default_models"` | Where HF checkpoints are downloaded |
| `VIT_WEIGHTS` | bool | `True` | Use ImageNet ViT weights when starting fresh |
| `FREEZE_VIT` | bool | `True` | Freeze backbone, train MLP head only |
| `EPOCHS` | int | 2 | Max training epochs |
| `BATCH_SIZE` | int | 16 | Images per gradient update |
| `LEARNING_RATE` | float | 1e-4 | Adam learning rate |
| `NUM_WORKERS` | int | 4 | DataLoader worker processes |
| `EARLY_STOPPING_PATIENCE` | int | 4 | Epochs without improvement before stopping |
| `EARLY_STOPPING_MIN_DELTA` | float | 0.005 | Minimum val_acc gain that counts as progress |
| `TRAINING_MC_PASSES` | int | 10 | MC-Dropout passes per image during training monitoring |
| `INFERENCE_MC_PASSES` | int | 20 | MC-Dropout passes per image during final inference |

### Inference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `DEVICE` | torch.device | auto | `cuda:0` if available, else `cpu` |
| `IMAGE_COLUMN` | str | `"abs_path"` | Column in img_df that holds absolute image paths |

---

## Output files

### `scores.csv`

One row per image scored.  Columns:

| Column | Description |
|--------|-------------|
| `img_id` | Image identifier |
| `geometry` | Point geometry (if available) |
| `{metric}` | Fine-tuned model score (0–10) |
| `uncertainty_{metric}` | MC-Dropout std of the score (0–10), when `INFERENCE_MC_PASSES > 1` |
| `original_{metric}` | Pre-trained baseline score (0–10), first run only |
| `uncertainty_original_{metric}` | MC-Dropout std for the baseline score |

### `{metric}_history.csv`

One row per training epoch.  Columns: `epoch`, `train_loss`, `train_acc`,
`val_loss`, `val_acc`, `val_score_mean`, `val_score_std`, `val_uncertainty`.

### `{metric}_curves.jpg`

Live-updated training plot with up to three panels:
- **Loss** — train (solid) vs val (dashed)
- **Accuracy** — train (solid) vs val (dashed)
- **Uncertainty** — val MC-Dropout mean std (dashed), only when
  `TRAINING_MC_PASSES > 1`

Each resumed training run is assigned a new colour; the legend distinguishes
train from val via both linestyle and label.

### `{metric}.pth`

The single best checkpoint, selected by the smart multi-metric logic (or
highest `val_acc` if `CHECKPOINT_SAVE_TOLERANCE=None`).  This is the model
used by inference.  Only one file is ever kept — no per-epoch copies are
written, so disk usage stays minimal.