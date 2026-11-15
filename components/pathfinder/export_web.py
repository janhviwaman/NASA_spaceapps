"""Export downsampled terrain layers (80 m) for the in-browser planner in web/."""
import json
import os

import numpy as np
import pandas as pd
from PIL import Image

from terrain import Terrain
from viz import _hillshade

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "web")
F = 4  # 20 m -> 80 m

t = Terrain(os.path.join(HERE, "data", "M20_JezeroCrater_CTXDEM_20m.tif"))
H, W = t.shape[0] // F, t.shape[1] // F


def blk(a):
    return a[:H * F, :W * F].reshape(H, F, W, F).transpose(0, 2, 1, 3).reshape(H, W, F * F)


z = blk(t.elev).mean(-1)
q = np.clip(np.rint((z + 3600) * 10), 0, 65535).astype(np.uint16)
Image.fromarray(np.dstack([q >> 8, q & 255, np.zeros_like(q)]).astype(np.uint8)).save(
    os.path.join(OUT, "elev.png"), optimize=True)

slope75 = np.percentile(blk(t.slope), 75, axis=-1)
rough = blk(t.rough).max(-1)
nogo_frac = blk(t.nogo.astype(np.float32)).mean(-1)
# cliffs/bad data independent of the slope limit (so the page's slope slider stays honest)
lay = np.dstack([np.clip(slope75 * 4, 0, 255), np.clip(rough * 20, 0, 255),
                 np.clip(nogo_frac * 255, 0, 255)]).astype(np.uint8)
Image.fromarray(lay).save(os.path.join(OUT, "layers.png"), optimize=True)

hs = _hillshade(t.elev[::2, ::2], t.res * 2)
Image.fromarray((np.clip(hs * 1.1, 0, 1) * 255).astype(np.uint8)).save(
    os.path.join(OUT, "hillshade.jpg"), quality=72)

w = pd.read_csv(os.path.join(HERE, "data", "M20_waypoints.csv"))
trav = [[round((~t.transform * (x, y))[1] / F - 0.5, 2), round((~t.transform * (x, y))[0] / F - 0.5, 2)]
        for x, y in zip(w.easting, w.northing)]
meta = {"H": H, "W": W, "res": t.res * F, "x0": t.transform.c, "y0": t.transform.f,
        "hsH": hs.shape[0], "hsW": hs.shape[1], "traverse": trav}
with open(os.path.join(OUT, "meta.json"), "w") as f:
    json.dump(meta, f)
print("exported", H, W)
