import json
import os

import pypdfium2 as pdfium

OUT_DIR = "/app/scripts/sickblack_render"
os.makedirs(OUT_DIR, exist_ok=True)

FILE_CORP = "/tmp/sickblack/9df77095-a449-4210-aa43-30de5d96869f.pdf"
FILE_LEASE = "/tmp/sickblack/ee2d0b9f-8c9c-4a0f-814d-30a973fb4484.pdf"


def render_pdf(path, prefix, scale=3.0):
    doc = pdfium.PdfDocument(path)
    out_paths = []
    for i in range(len(doc)):
        page = doc[i]
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()
        out_path = os.path.join(OUT_DIR, f"{prefix}_p{i+1}.png")
        pil_image.save(out_path)
        out_paths.append(out_path)
    doc.close()
    return out_paths


corp_pages = render_pdf(FILE_CORP, "corp", scale=3.0)
lease_pages = render_pdf(FILE_LEASE, "lease", scale=3.0)

print(json.dumps({"corp": corp_pages, "lease": lease_pages}, ensure_ascii=False))
