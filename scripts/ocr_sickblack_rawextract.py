import json
import os

import pypdfium2 as pdfium

OUT_DIR = "/app/scripts/sickblack_render/raw"
os.makedirs(OUT_DIR, exist_ok=True)

FILE_LEASE = "/tmp/sickblack/ee2d0b9f-8c9c-4a0f-814d-30a973fb4484.pdf"
FILE_CORP = "/tmp/sickblack/9df77095-a449-4210-aa43-30de5d96869f.pdf"


def extract(path, prefix):
    doc = pdfium.PdfDocument(path)
    out = []
    for i in range(len(doc)):
        page = doc[i]
        for obj in page.get_objects():
            if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                bmp = obj.get_bitmap(render=False)
                pil_img = bmp.to_pil()
                out_path = os.path.join(OUT_DIR, f"{prefix}_p{i+1}_raw.png")
                pil_img.save(out_path)
                out.append({"path": out_path, "size": pil_img.size, "mode": pil_img.mode})
    doc.close()
    return out


result = {
    "lease": extract(FILE_LEASE, "lease"),
    "corp": extract(FILE_CORP, "corp"),
}
print(json.dumps(result, ensure_ascii=False, indent=2))
