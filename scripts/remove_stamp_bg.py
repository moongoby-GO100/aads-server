#!/usr/bin/env python3
"""Remove background from stamp images, save each stamp as transparent PNG."""
import numpy as np
from PIL import Image
import os

def remove_background(img_array, threshold=220):
    """Make near-white/near-gray pixels transparent."""
    rgba = img_array.copy()
    if rgba.shape[2] == 3:
        alpha = np.full((*rgba.shape[:2], 1), 255, dtype=np.uint8)
        rgba = np.concatenate([rgba, alpha], axis=2)

    r, g, b = rgba[:,:,0], rgba[:,:,1], rgba[:,:,2]
    is_bg = (r > threshold) & (g > threshold) & (b > threshold)
    near_white = (r.astype(int) + g.astype(int) + b.astype(int)) > (threshold * 3 - 30)
    low_saturation = (np.max(rgba[:,:,:3], axis=2).astype(int) - np.min(rgba[:,:,:3], axis=2).astype(int)) < 30
    bg_mask = is_bg | (near_white & low_saturation)
    rgba[bg_mask, 3] = 0
    return rgba

def find_stamp_regions(alpha_channel, min_area=500):
    """Find bounding boxes of non-transparent regions using simple flood-fill approach."""
    binary = (alpha_channel > 0).astype(np.uint8)
    
    rows = np.any(binary, axis=1)
    if not np.any(rows):
        return []
    
    row_groups = []
    in_group = False
    start = 0
    for i in range(len(rows)):
        if rows[i] and not in_group:
            start = i
            in_group = True
        elif not rows[i] and in_group:
            row_groups.append((start, i))
            in_group = False
    if in_group:
        row_groups.append((start, len(rows)))
    
    merged = []
    for rg in row_groups:
        if merged and rg[0] - merged[-1][1] < 15:
            merged[-1] = (merged[-1][0], rg[1])
        else:
            merged.append(rg)
    
    regions = []
    for y_start, y_end in merged:
        strip = binary[y_start:y_end, :]
        cols = np.any(strip, axis=0)
        col_groups = []
        in_group = False
        start = 0
        for j in range(len(cols)):
            if cols[j] and not in_group:
                start = j
                in_group = True
            elif not cols[j] and in_group:
                col_groups.append((start, j))
                in_group = False
        if in_group:
            col_groups.append((start, len(cols)))
        
        col_merged = []
        for cg in col_groups:
            if col_merged and cg[0] - col_merged[-1][1] < 15:
                col_merged[-1] = (col_merged[-1][0], cg[1])
            else:
                col_merged.append(cg)
        
        for x_start, x_end in col_merged:
            area = (y_end - y_start) * (x_end - x_start)
            if area >= min_area:
                regions.append((x_start, y_start, x_end, y_end))
    
    return regions

def main():
    input_path = '/app/exports/stamps/original_stamps.png'
    output_dir = '/app/exports/stamps'
    static_dir = '/app/static/docs/stamps'
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(static_dir, exist_ok=True)
    
    img = Image.open(input_path).convert('RGBA')
    img_array = np.array(img)
    print(f"Original image: {img.size}, mode: {img.mode}")
    
    result = remove_background(img_array, threshold=215)
    
    regions = find_stamp_regions(result[:,:,3], min_area=800)
    print(f"Found {len(regions)} stamp region(s)")
    
    if not regions:
        result_img = Image.fromarray(result)
        out_path = os.path.join(output_dir, 'stamp_all_transparent.png')
        result_img.save(out_path)
        static_path = os.path.join(static_dir, 'stamp_all_transparent.png')
        result_img.save(static_path)
        print(f"No regions detected, saved full image: {out_path}")
        return
    
    saved = []
    for idx, (x1, y1, x2, y2) in enumerate(regions, 1):
        pad = 5
        x1 = max(0, x1 - pad)
        y1 = max(0, y1 - pad)
        x2 = min(result.shape[1], x2 + pad)
        y2 = min(result.shape[0], y2 + pad)
        
        crop = result[y1:y2, x1:x2]
        crop_img = Image.fromarray(crop)
        
        fname = f'stamp_{idx:02d}.png'
        out_path = os.path.join(output_dir, fname)
        static_path = os.path.join(static_dir, fname)
        crop_img.save(out_path, 'PNG')
        crop_img.save(static_path, 'PNG')
        
        w, h = crop_img.size
        saved.append((fname, w, h))
        print(f"  [{idx}] {fname}: {w}x{h}px, region=({x1},{y1})-({x2},{y2})")
    
    full_img = Image.fromarray(result)
    full_path = os.path.join(output_dir, 'stamp_all_transparent.png')
    full_static = os.path.join(static_dir, 'stamp_all_transparent.png')
    full_img.save(full_path, 'PNG')
    full_img.save(full_static, 'PNG')
    print(f"  [ALL] stamp_all_transparent.png: {full_img.size[0]}x{full_img.size[1]}px")
    
    print(f"\nDone! {len(saved)} stamps + 1 combined = {len(saved)+1} files saved")

if __name__ == '__main__':
    main()
