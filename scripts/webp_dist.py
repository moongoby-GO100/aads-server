#!/usr/bin/env python3
"""Distributed WebP batch converter for DigitalOcean Spaces.
Usage:
  export DO_KEY=xxx DO_SECRET=xxx
  python3 webp_dist.py --start 202301 --end 202412 [--workers 50]
"""
import os, sys, io, time, threading, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
import boto3
from botocore.config import Config

ENDPOINT = "https://nyc3.digitaloceanspaces.com"
BUCKET = "newtalk"
WEBP_QUALITY = 80
MIN_SIZE = 5 * 1024
MAX_DIM = 16383
DONE_FILE = "/tmp/webp_dist_done.log"
LOG_FILE = "/tmp/webp_dist.log"
ERR_FILE = "/tmp/webp_dist_errors.log"

ACCESS_KEY = os.environ.get("DO_KEY", "")
SECRET_KEY = os.environ.get("DO_SECRET", "")

done_set = set()
done_lock = threading.Lock()
stats_lock = threading.Lock()
stats = dict(converted=0, skipped_small=0, skipped_done=0,
             bigger=0, error=0, saved_bytes=0, total_orig=0)
t0 = 0

_tls = threading.local()

def get_s3():
    if not hasattr(_tls, "s3"):
        _tls.s3 = boto3.client(
            "s3", endpoint_url=ENDPOINT,
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            config=Config(max_pool_connections=20,
                          retries={"max_attempts": 3}),
        )
    return _tls.s3

def load_done():
    global done_set
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            done_set = {l.strip() for l in f if l.strip()}
    # Also load old done log from previous single-server run
    old = "/tmp/webp_convert_done.log"
    if os.path.exists(old):
        with open(old) as f:
            done_set.update(l.strip() for l in f if l.strip())
    print(f"[INIT] skip set: {len(done_set):,}", flush=True)

def mark_done(key):
    with done_lock:
        done_set.add(key)
        with open(DONE_FILE, "a") as f:
            f.write(key + "\n")

def convert_one(key, size):
    try:
        if size < MIN_SIZE:
            with stats_lock:
                stats["skipped_small"] += 1
            return

        s3 = get_s3()
        data = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

        img = Image.open(io.BytesIO(data))
        if img.width > MAX_DIM or img.height > MAX_DIM:
            r = min(MAX_DIM / img.width, MAX_DIM / img.height)
            img = img.resize((int(img.width * r), int(img.height * r)),
                             Image.LANCZOS)

        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=WEBP_QUALITY, method=4)
        webp = buf.getvalue()

        if len(webp) >= len(data):
            with stats_lock:
                stats["bigger"] += 1
            mark_done(key)
            return

        s3.put_object(Bucket=BUCKET, Key=key, Body=webp,
                      ContentType="image/webp", ACL="public-read")

        saved = len(data) - len(webp)
        with stats_lock:
            stats["converted"] += 1
            stats["saved_bytes"] += saved
            stats["total_orig"] += len(data)
        mark_done(key)

    except Exception as e:
        with stats_lock:
            stats["error"] += 1
        with open(ERR_FILE, "a") as ef:
            ef.write(f"{key}: {e}\n")

def progress_line(listed):
    elapsed = (time.time() - t0) / 60
    done = stats["converted"] + stats["bigger"] + stats["skipped_small"]
    rate = done / max(elapsed * 60, 1)
    saved_mb = stats["saved_bytes"] / (1024 * 1024)
    pct = int(stats["saved_bytes"] * 100 /
              max(stats["total_orig"], 1)) if stats["total_orig"] else 0
    return (f"  목록:{listed:,} | 변환:{stats['converted']:,} | "
            f"더큼:{stats['bigger']} | 소형:{stats['skipped_small']} | "
            f"에러:{stats['error']} | 절감:{saved_mb:,.0f}MB({pct}%) | "
            f"{rate:.1f}/s | {elapsed:.0f}분")

def main():
    global t0
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYYMM")
    ap.add_argument("--end", required=True, help="YYYYMM")
    ap.add_argument("--workers", type=int, default=50)
    args = ap.parse_args()

    if not ACCESS_KEY or not SECRET_KEY:
        print("ERROR: export DO_KEY, DO_SECRET"); sys.exit(1)

    load_done()

    prefixes = []
    y, m = int(args.start[:4]), int(args.start[4:])
    ey, em = int(args.end[:4]), int(args.end[4:])
    while (y, m) <= (ey, em):
        prefixes.append(f"img/{y:04d}{m:02d}/")
        m += 1
        if m > 12: m, y = 1, y + 1

    print(f"[START] {args.start}~{args.end} ({len(prefixes)} months), "
          f"workers={args.workers}", flush=True)

    t0 = time.time()
    total_listed = 0
    log_interval = 5000

    s3_main = boto3.client(
        "s3", endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = []
        for pfx in prefixes:
            print(f"[SCAN] {pfx}", flush=True)
            pager = s3_main.get_paginator("list_objects_v2")
            for page in pager.paginate(Bucket=BUCKET, Prefix=pfx):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    total_listed += 1
                    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
                    if ext not in ("jpg", "jpeg", "png", "bmp", "tiff"):
                        continue
                    if key in done_set:
                        stats["skipped_done"] += 1
                        continue
                    futures.append(pool.submit(convert_one, key, obj["Size"]))

                    if len(futures) >= 500:
                        for f in as_completed(futures):
                            try: f.result()
                            except: pass
                        futures.clear()

                if total_listed % log_interval < 1000:
                    msg = progress_line(total_listed)
                    print(msg, flush=True)
                    with open(LOG_FILE, "a") as lf:
                        lf.write(msg + "\n")

        for f in as_completed(futures):
            try: f.result()
            except: pass

    elapsed = (time.time() - t0) / 60
    saved_gb = stats["saved_bytes"] / (1024 ** 3)
    final = (f"\n[DONE] 변환:{stats['converted']:,} | "
             f"절감:{saved_gb:.1f}GB | 에러:{stats['error']} | "
             f"{elapsed:.0f}분")
    print(final, flush=True)
    with open(LOG_FILE, "a") as lf:
        lf.write(final + "\n")

if __name__ == "__main__":
    main()
