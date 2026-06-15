#!/usr/bin/env python3
"""도장 이미지 배경 제거 (누끼) 스크립트"""
import os
from PIL import Image
import numpy as np
from collections import deque

SRC = '/tmp/aads-codex-images/95c53d3f-2863-49f5-948e-53e4bab877e2/image_01.png'
OUT_DIR = '/root/aads/aads-server/app/static/docs/contracts/stamps'
os.makedirs(OUT_DIR, exist_ok=True)

img = Image.open(SRC).convert('RGBA')
arr = np.array(img)
h, w = arr.shape[:2]
print(f"Image: {w}x{h}")

r, g, b = arr[:,:,0].astype(int), arr[:,:,1].astype(int), arr[:,:,2].astype(int)
brightness = (r + g + b) / 3
stamp_mask = brightness > 70
print(f"Non-dark pixels: {stamp_mask.sum()} ({stamp_mask.sum()*100/(h*w):.1f}%)")

labeled = np.zeros((h, w), dtype=int)
label_id = 0
regions = []

def bfs_label(start_y, start_x, label):
    queue = deque([(start_y, start_x)])
    labeled[start_y, start_x] = label
    min_y, max_y, min_x, max_x = start_y, start_y, start_x, start_x
    count = 1
    while queue:
        cy, cx = queue.popleft()
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                ny, nx = cy + dy, cx + dx
                if 0 <= ny < h and 0 <= nx < w and labeled[ny, nx] == 0 and stamp_mask[ny, nx]:
                    labeled[ny, nx] = label
                    queue.append((ny, nx))
                    count += 1
                    if ny < min_y: min_y = ny
                    if ny > max_y: max_y = ny
                    if nx < min_x: min_x = nx
                    if nx > max_x: max_x = nx
    return count, (min_y, min_x, max_y, max_x)

for y in range(h):
    for x in range(w):
        if stamp_mask[y, x] and labeled[y, x] == 0:
            label_id += 1
            area, bbox = bfs_label(y, x, label_id)
            by, bx, ey, ex = bbox
            bw, bh = ex - bx + 1, ey - by + 1
            if bw > 50 and bh > 50 and area > 2000:
                regions.append({'id': label_id, 'bbox': bbox, 'size': (bw, bh), 'area': area})
                print(f"Region {label_id}: ({bx},{by})-({ex},{ey}), {bw}x{bh}, area={area}")

print(f"\nFound {len(regions)} stamp regions")

PAD = 10
for i, region in enumerate(regions):
    by, bx, ey, ex = region['bbox']
    cy1 = max(0, by - PAD)
    cx1 = max(0, bx - PAD)
    cy2 = min(h, ey + PAD + 1)
    cx2 = min(w, ex + PAD + 1)
    cropped = arr[cy1:cy2, cx1:cx2].copy()
    ch, cw = cropped.shape[:2]
    cr = cropped[:,:,0].astype(int)
    cg = cropped[:,:,1].astype(int)
    cb = cropped[:,:,2].astype(int)
    c_bright = (cr + cg + cb) / 3
    alpha = np.zeros((ch, cw), dtype=np.uint8)
    alpha[c_bright > 80] = 255
    semi = (c_bright >= 50) & (c_bright <= 80)
    alpha[semi] = ((c_bright[semi] - 50) * 255 / 30).astype(np.uint8)
    cropped[:,:,3] = alpha
    stamp_img = Image.fromarray(cropped)
    bbox_a = stamp_img.split()[3].getbbox()
    if bbox_a:
        stamp_img = stamp_img.crop(bbox_a)
    out_path = os.path.join(OUT_DIR, f'stamp_{i+1}.png')
    stamp_img.save(out_path, 'PNG')
    print(f"Saved stamp_{i+1}.png: {stamp_img.size[0]}x{stamp_img.size[1]}")

print(f"\nDone. Files in {OUT_DIR}/")
for f in sorted(os.listdir(OUT_DIR)):
    fp = os.path.join(OUT_DIR, f)
    print(f"  {f}: {os.path.getsize(fp):,} bytes")
