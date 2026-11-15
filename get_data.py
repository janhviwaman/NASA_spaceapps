#!/usr/bin/env python3
"""
Download the public data this project needs into data/ (about 90 MB, one time).

    python get_data.py

1. USGS / JPL "Mars 2020 Science Investigation CTX DEM Mosaic" of Jezero crater (20 m/px).
2. NASA Mars 2020 (Perseverance) waypoint log from the mission's public map, saved as CSV.
"""
import csv
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

DEM_NAME = "M20_JezeroCrater_CTXDEM_20m.tif"
DEM_URLS = [
    "https://planetarymaps.usgs.gov/mosaic/mars2020_trn/CTX/ScienceInvestigationMaps_JPL/" + DEM_NAME,
    "https://asc-pds-services.s3.us-west-2.amazonaws.com/mosaic/mars2020_trn/CTX/ScienceInvestigationMaps_JPL/" + DEM_NAME,
]
DEM_BYTES = 90_345_300
WAYPOINTS_URL = "https://mars.nasa.gov/mmgis-maps/M20/Layers/json/M20_waypoints.json"
UA = {"User-Agent": "jezero-pathfinder/1.0 (+https://github.com)"}


def download(urls, dest, expected=None):
    for url in urls:
        try:
            print(f"Downloading {os.path.basename(dest)}\n  from {url}")
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as f:
                total = int(r.headers.get("Content-Length") or expected or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        sys.stdout.write(f"\r  {got/1e6:6.1f} / {total/1e6:.1f} MB")
                        sys.stdout.flush()
            print()
            if expected and got != expected:
                raise IOError(f"size {got} != expected {expected}")
            os.replace(dest + ".part", dest)
            return True
        except Exception as e:
            print(f"\n  failed: {e}")
            if os.path.exists(dest + ".part"):
                os.remove(dest + ".part")
    return False


def main():
    os.makedirs(DATA, exist_ok=True)
    dem = os.path.join(DATA, DEM_NAME)
    if os.path.exists(dem) and os.path.getsize(dem) == DEM_BYTES:
        print(f"Elevation model already present: {dem}")
    elif not download(DEM_URLS, dem, DEM_BYTES):
        sys.exit("Could not download the DEM. Download it manually from the first URL above "
                 f"and put it in {DATA}/")

    csv_path = os.path.join(DATA, "M20_waypoints.csv")
    if os.path.exists(csv_path):
        print(f"Rover waypoints already present: {csv_path}")
    else:
        print(f"Downloading Perseverance waypoints\n  from {WAYPOINTS_URL}")
        try:
            req = urllib.request.Request(WAYPOINTS_URL, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                gj = json.load(r)
            keys = ["sol", "site", "drive", "easting", "northing", "lon", "lat",
                    "elev_geoid", "tilt", "dist_total_m"]
            with open(csv_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(keys)
                for feat in gj["features"]:
                    p = feat["properties"]
                    w.writerow([p.get(k) for k in keys])
            print(f"  saved {len(gj['features'])} waypoints")
        except Exception as e:
            print(f"  failed ({e}). The planner still works; only the rover-track overlay "
                  "and validation need this file.")
    print("\nDone. Start the app with:  python app.py")


if __name__ == "__main__":
    main()
