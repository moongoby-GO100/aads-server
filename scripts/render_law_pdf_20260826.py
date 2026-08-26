import os
import pypdfium2 as pdfium

BASE = "/root/aads/uploads/chat/files/efccec7c-0788-4564-a2cf-265c63d075f0"
OUT = "/app/docs/law_extract/img"
os.makedirs(OUT, exist_ok=True)

targets = {
    "deungibu": "9df77095-a449-4210-aa43-30de5d96869f.pdf",
}

for key, fn in targets.items():
    path = os.path.join(BASE, fn)
    try:
        pdf = pdfium.PdfDocument(path)
        n = len(pdf)
        print("%s pages=%d" % (key, n))
        for i in range(min(n, 12)):
            page = pdf[i]
            bmp = page.render(scale=2.0)
            img = bmp.to_pil()
            img = img.convert("RGB")
            op = os.path.join(OUT, "%s_p%02d.jpg" % (key, i + 1))
            img.save(op, "JPEG", quality=80)
            print("saved", op, os.path.getsize(op))
    except Exception as e:
        print("%s ERROR %r" % (key, e))
