# Street Perception Mapping System

An end-to-end pipeline that ingests human A/B survey data and ML model predictions,
normalises both into a common score space, and renders them as an interactive
Leaflet map overlay with floating dashboard, colormapped markers, uncertainty
brackets, and side-by-side image popups.

---

## Project layout

```
ABsurveys/                          ← ROOT_PATH
│
├── main.py                         ← pipeline entry point (configure & run this)
├── map.py                          ← Folium/Leaflet HTML generator
├── utils.py                        ← score normalisation & simulation helpers
├── map.css / map.js                ← dashboard UI (loaded by map.py at build time)
│
├── images/
│   └── Anlagenring/
│       ├── images.csv              ← master image index  ← single source of truth for paths
│       ├── database.swm2           ← optional SQLite DB with GPS + bearing metadata
│       ├── Walk/  Bike/  Stay/     ← actual image files
│
├── user_data/
│   ├── Anlagenring_user_data_merged.csv       ← raw A/B click log
│   └── Anlagenring_user_images_merged.csv     ← TrueSkill scores per image
│
├── evaluation/streetscore/models/
│   └── FrankfurtAnlagenring/
│       └── scores.csv              ← ML model predictions
│
└── map.html                        ← ✅ generated output
```

---

## Data sources and their roles

| File | Key columns used | Notes |
|---|---|---|
| `images/Anlagenring/images.csv` | `img_id`, `path`, `img_type`, `scenario` | **Only** source of image file paths. Paths inside are relative to this file's directory and are re-expressed relative to `ROOT_PATH` at load time. |
| `user_data/Anlagenring_user_data_merged.csv` | `user_id`, `type` | Used only to count unique respondents and total A/B clicks. |
| `user_data/Anlagenring_user_images_merged.csv` | `img_id`, `img_type`, `scenario`, `score_<question_id>`, `uncertainty_<question_id>`, `n_answers_<question_id>` | TrueSkill ratings derived from pairwise comparisons. Path columns in this file are ignored. |
| `evaluation/…/scores.csv` | `img_id`, `img_type`, `scenario`, `<metric>`, `uncertainty_mc_<metric>`, `entropy_<metric>` | CNN/ML walkability, bikeability, stayability scores. Path columns (`path`, `_base_dir`, `abs_path`) are explicitly dropped on load. |
| `images/Anlagenring/database.swm2` | `lat`, `lon`, `bearing` | Optional SQLite database. When present, GPS coordinates and camera bearing are merged into the image index by filename. If absent, coordinate columns from `images.csv` are used (or random Frankfurt-area fallback values). |

**Image path resolution rule:**
1. Read `path` from `images.csv` (relative to its own directory, e.g. `Walk/image.jpg`).
2. Combine with the CSV directory to get an absolute path.
3. Re-express as a path **relative to `ROOT_PATH`** (e.g. `images/Anlagenring/Walk/image.jpg`).
4. Store that relative path in the JSON embedded in `map.html` so the browser can load images via simple relative URLs.

---

## Score normalisation

TrueSkill (human) and StreetScore (ML) scores live on different scales.
`normalize_and_align_distributions()` in `utils.py`:

1. Zero-centres both distributions independently.
2. Standardises each to unit variance.
3. Finds the **global** maximum absolute deviation across both sets.
4. Scales both so the most extreme point sits at exactly 0 or 10, with the mean anchored at 5.

This guarantees that both models are directly comparable on the map legend
(0 = worst, 5 = average, 10 = best) without distorting relative rankings.
The same scale factor is applied to each model's uncertainty values so that
confidence intervals remain proportional to the scores.

---

## Configuration (`main.py`)

All paths in the `CONFIGURATION` block are **relative to `ROOT_PATH`** and are
resolved to absolute paths at runtime — you never need to hard-code absolute paths
anywhere except `ROOT_PATH` itself.

```python
ROOT_PATH = "/path/to/ABsurveys"       # ← only absolute path you need to set

IMG_PATHS            = "images/Anlagenring/images.csv"
HUMAN_DF_PATHS       = "user_data/Anlagenring_user_data_merged.csv"
TRUESKILL_DF_PATHS   = "user_data/Anlagenring_user_images_merged.csv"
STREETSCORE_DF_PATHS = "evaluation/streetscore/models/FrankfurtAnlagenring/scores.csv"
SWM2_DATABASE_PATH   = "images/Anlagenring/database.swm2"   # set to None if unavailable
```

Each entry in `METRICS_MAP` links:
- `streetscore_metric` — column name in `scores.csv` (e.g. `"walk"`)
- `question_id` — question key in the TrueSkill CSV (e.g. `"walk-preference"`)
- `img_type` — filter value matching the `img_type` column in all CSVs
- `scenario` — optional scenario filter (e.g. `"Anlagenring"`)

---

## Running

```bash
# Install dependencies (once)
pip install folium branca pandas numpy

# Run the pipeline
python main.py
```

If any of the data files are missing the pipeline falls back to a built-in
simulation that generates realistic mock data for the Frankfurt Anlagenring
ring road, so `map.html` is always produced and the dashboard is fully
functional for UI development and demos.

The output `map.html` is a single self-contained file. Open it directly in a
browser (served via a local web server if images are to load from disk):

```bash
cd /path/to/ABsurveys
python -m http.server 8080
# then open http://localhost:8080/map.html
```

---

## Map dashboard features

- **Model switch** — toggle between TrueSkill (human), StreetScore (ML), or their difference.
- **Mode switch** — display score values or uncertainty intervals.
- **Metric buttons** — switch between walk / bike / stay perception scores.
- **Colormapped markers** — marker colour encodes the selected score on a 0–10 scale.
- **Uncertainty brackets** — visual confidence intervals rendered on each tooltip.
- **Click popup** — side-by-side image viewer with fullscreen mode.
- **Survey stats** — live respondent count and total click count in the corner panel.