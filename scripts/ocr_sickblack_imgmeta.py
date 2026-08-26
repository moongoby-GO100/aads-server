import json
import pypdfium2 as pdfium

FILE_LEASE = "/tmp/sickblack/ee2d0b9f-8c9c-4a0f-814d-30a973fb4484.pdf"
FILE_CORP = "/tmp/sickblack/9df77095-a449-4210-aa43-30de5d96869f.pdf"


def inspect(path):
    doc = pdfium.PdfDocument(path)
    info = []
    for i in range(len(doc)):
        page = doc[i]
        w, h = page.get_size()
        page_info = {"page": i + 1, "width_pt": w, "height_pt": h, "images": []}
        for obj in page.get_objects():
            try:
                if obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                    bmp = obj.get_bitmap(render=False)
                    page_info["images"].append({
                        "px_width": bmp.width,
                        "px_height": bmp.height,
                    })
            except Exception as e:
                page_info["images"].append({"error": repr(e)})
        info.append(page_info)
    doc.close()
    return info


result = {"lease": inspect(FILE_LEASE), "corp": inspect(FILE_CORP)}
print(json.dumps(result, ensure_ascii=False, indent=2))
