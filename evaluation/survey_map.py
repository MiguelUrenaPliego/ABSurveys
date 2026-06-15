import os
import sqlite3
import pandas as pd
import geopandas as gpd 

import maps 

root = "/home/miguel/Documents/UNI/Master/2/ProjektVerkehr/ABsurveys/"

#img_df = pd.read_json(root + "user_data/image_state.json")
img_df = pd.read_csv(root + "user_data/Anlagenring_user_images_merged.csv")

location_db = root + "images/Anlagenring/database.swm2"
conn = sqlite3.connect(location_db)

# --- database metadata ---
metadata = pd.read_sql_query("""
SELECT
    photo_path,
    lon AS x,
    lat AS y,
    bearing
FROM photos p
JOIN points pt
    ON p.uuid = pt.fid
""", conn)

# normalize to filename only
metadata["filename"] = metadata["photo_path"].apply(os.path.basename)

# --- img_df side ---
img_df["filename"] = img_df["path"].apply(os.path.basename)

img_df = img_df.merge(
    metadata[["filename", "x", "y", "bearing"]],
    on="filename",
    how="left"
)

img_df = gpd.GeoDataFrame(img_df,geometry=gpd.points_from_xy(img_df["x"],img_df["y"],crs=4326))
img_df["path"] = "images/Anlagenring/" + img_df["path"]

columns = [
    'img_id', 'path', 
    'score_stay-preference',
    'uncertainty_stay-preference', 
    'n_answers_stay-preference',
    'x', 'y', 
    'bearing',
]
rename_columns = {
    'score_stay-preference':'score', 
    'uncertainty_stay-preference':'uncertainty',
    'n_answers_stay-preference':'n_answers', 
}
stay_df = img_df[img_df["img_type"] == "stay"]
stay_df = stay_df[columns + ["geometry"]].rename(columns=rename_columns)
m = maps.create_map(stay_df,column="score",layer_name="stay")
columns = [
    'img_id', 'path', 
    'score_bike-preference',
    'uncertainty_bike-preference', 
    'n_answers_bike-preference',
    'x', 'y', 
    'bearing',
]
rename_columns = {
    'score_bike-preference':'score', 
    'uncertainty_bike-preference':'uncertainty',
    'n_answers_bike-preference':'n_answers', 
}
bike_df = img_df[img_df["img_type"] == "bike"]
bike_df = bike_df[bike_df.geometry.is_valid]
bike_df = bike_df[columns + ["geometry"]].rename(columns=rename_columns)
m = maps.create_map(bike_df,column="score",m=m,layer_name="bike")
columns = [
    'img_id', 'path', 
    'score_walk-preference', 
    'uncertainty_walk-preference',
    'n_answers_walk-preference', 
    'x', 'y', 
    'bearing',
]
rename_columns = {
    'score_walk-preference':'score', 
    'uncertainty_walk-preference':'uncertainty',
    'n_answers_walk-preference':'n_answers', 
}
walk_df = img_df[img_df["img_type"] == "walk"]
walk_df = walk_df[walk_df.geometry.is_valid]
walk_df = walk_df[columns + ["geometry"]].rename(columns=rename_columns)
m = maps.create_map(walk_df,column="score",m=m,layer_name="walk")
m.save(root + "index.html")
print(f"Saved map to {root + 'index.html'}")