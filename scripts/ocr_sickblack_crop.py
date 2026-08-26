import json
import os

from PIL import Image

SRC_DIR = "/app/scripts/sickblack_render"
OUT_DIR = "/app/scripts/sickblack_render/crops"
os.makedirs(OUT_DIR, exist_ok=True)

# (filename, num_row_slices, overlap_fraction)
TARGETS = [
    ("corp_p1.png", 2, 0.08),
    ("corp_p2.png", 2, 0.08),
    ("corp_p3.png", 2, 0.08),
    ("lease_p1.png", 3, 0.08),
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
        out_paths.append(out_path)
    return out_paths


result = {}
for fname, n, overlap in TARGETS:
    src = os.path.join(SRC_DIR, fname)
    result[fname] = crop_rows(src, n, overlap)

print(json.dumps(result, ensure_ascii=False, indent=2))
