#!/usr/bin/env python3
"""냉면육수 OEM 문의 이메일 초안 PDF 생성 (Playwright)"""
import os, shutil

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, "naengmyeon_oem_email_draft.html")
PDF  = os.path.join(BASE, "naengmyeon_oem_email_draft.pdf")
PDF2 = os.path.join(BASE, "냉면육수_OEM_문의_이메일_초안.pdf")

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(f"file://{HTML}", wait_until="networkidle")
    page.pdf(
        path=PDF,
        format="A4",
        margin={"top": "18mm", "right": "16mm", "bottom": "20mm", "left": "16mm"},
        print_background=True,
    )
    browser.close()

shutil.copy2(PDF, PDF2)
size = os.path.getsize(PDF)

pages = "unknown"
try:
    import fitz
    doc = fitz.open(PDF)
    pages = doc.page_count
    doc.close()
except Exception:
    try:
        import pypdf
        reader = pypdf.PdfReader(PDF)
        pages = len(reader.pages)
    except Exception:
        pass

print(f"OK|{size}|{pages}")
