import sys, json

FILES = [
    "/tmp/sickblack/9df77095-a449-4210-aa43-30de5d96869f.pdf",
    "/tmp/sickblack/ee2d0b9f-8c9c-4a0f-814d-30a973fb4484.pdf",
]

def diag(path):
    info = {"path": path}
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
        info["header_8"] = head[:8]
        info["has_%PDF"] = b"%PDF" in head[:16]
        # find startxref / trailer near end
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            info["size"] = size
            f.seek(max(0, size - 2048))
            tail = f.read()
        info["tail_has_startxref"] = b"startxref" in tail
        info["tail_has_EOF"] = b"%%EOF" in tail
        info["tail_last200"] = tail[-200:]
    except Exception as e:
        info["read_error"] = repr(e)

    try:
        import pdfplumber
        try:
            with pdfplumber.open(path) as pdf:
                info["pdfplumber_pages"] = len(pdf.pages)
        except Exception as e:
            info["pdfplumber_open_error"] = repr(e)
    except Exception as e:
        info["pdfplumber_import_error"] = repr(e)

    try:
        import pypdfium2 as pdfium
        try:
            doc = pdfium.PdfDocument(path)
            info["pypdfium2_pages"] = len(doc)
            info["pypdfium2_is_encrypted"] = getattr(doc, "get_metadata_dict", lambda: {})()
            doc.close()
        except Exception as e:
            info["pypdfium2_open_error"] = repr(e)
    except Exception as e:
        info["pypdfium2_import_error"] = repr(e)

    try:
        from pdfminer.pdfparser import PDFParser
        from pdfminer.pdfdocument import PDFDocument
        with open(path, "rb") as f:
            parser = PDFParser(f)
            try:
                doc = PDFDocument(parser)
                info["pdfminer_is_extractable"] = doc.is_extractable
                info["pdfminer_encryption"] = doc.encryption is not None
            except Exception as e:
                info["pdfminer_doc_error"] = repr(e)
    except Exception as e:
        info["pdfminer_import_error"] = repr(e)

    return info


def safe(o):
    if isinstance(o, bytes):
        return o.decode("latin1", errors="replace")
    if isinstance(o, dict):
        return {k: safe(v) for k, v in o.items()}
    if isinstance(o, list):
        return [safe(v) for v in o]
    return o


results = [diag(p) for p in FILES]
print(json.dumps(safe(results), ensure_ascii=False, indent=2))
