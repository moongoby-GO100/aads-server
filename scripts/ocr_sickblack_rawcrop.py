import json
import os

from PIL import Image

SRC_DIR = "/app/scripts/sickblack_render/raw"
OUT_DIR = "/app/scripts/sickblack_render/raw/crops"
os.makedirs(OUT_DIR, exist_ok=True)

TARGETS = [
    ("lease_p1_raw.png", 4, 0.06),
    ("corp_p1_raw.png", 4, 0.06),
    ("corp_p2_raw.png", 4, 0.06),
    ("corp_p3_raw.png", 4, 0.06),
]


def crop_rows(path, n, overlap):
    im = Image.open(path)
    w, h = im.size
    slice_h = h / n
    out_paths = []
    base = os.path.splitext(os.path.basename(path))[0]
    for i in range(n):
        top = max(0, int(i * slice_h - slice_h * overlap))
        bottom = min(h, int((i + 1) * slice_h + slice_h * overlap))
        crop = im.crop((0, top, w, bottom))
        out_path = os.path.join(OUT_DIR, f"{base}_row{i+1}.png")
        crop.save(out_path)
        out_paths.append({"path": out_path, "size": crop.size})
    return out_paths


result = {}
for fname, n, overlap in TARGETS:
    result[fname] = crop_rows(os.path.join(SRC_DIR, fname), n, overlap)

print(json.dumps(result, ensure_ascii=False, indent=2))
