import os, sys
import pdfplumber

BASE = "/root/aads/uploads/chat/files/efccec7c-0788-4564-a2cf-265c63d075f0"
OUT = "/app/docs/law_extract"
os.makedirs(OUT, exist_ok=True)

FILES = {
    "jeonggwan": "fff5a615-81c4-44a3-93cc-4611eb566628.pdf",
    "jujumyeongbu": "7bc585d9-0c48-4be1-83e5-05f98378a28f.pdf",
    "deungibu": "9df77095-a449-4210-aa43-30de5d96869f.pdf",
    "changgo": "ee2d0b9f-8c9c-4a0f-814d-30a973fb4484.pdf",
}

for key, fn in FILES.items():
    path = os.path.join(BASE, fn)
    out_path = os.path.join(OUT, key + ".txt")
    try:
        chunks = []
        with pdfplumber.open(path) as pdf:
            npages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                t = page.extract_text() or ""
                chunks.append("=== PAGE %d/%d ===\n%s" % (i + 1, npages, t))
        text = "\n".join(chunks)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print("%s pages=%d chars=%d -> %s" % (key, npages, len(text), out_path))
    except Exception as e:
        print("%s ERROR %s" % (key, e))
