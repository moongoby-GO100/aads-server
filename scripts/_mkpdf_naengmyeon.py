# -*- coding: utf-8 -*-
from playwright.sync_api import sync_playwright

SRC = "file:///app/app/static/reports/naengmyeon_oem_report.html"
OUT = "/app/app/static/reports/naengmyeon_oem_report.pdf"

FOOTER = (
    '<div style="width:100%;font-size:7.5px;color:#94a3b8;'
    'font-family:Pretendard,sans-serif;padding:0 12mm;'
    'display:flex;justify-content:space-between;">'
    '<span>냉면육수 OEM/ODM 제조공장 조사보고서 · FOOD-FIND-002 · 2026-08-04</span>'
    '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span>'
    '</div>'
)

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page()
    page.goto(SRC, wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.pdf(
        path=OUT,
        format="A4",
        print_background=True,
        prefer_css_page_size=False,
        margin={"top": "12mm", "bottom": "16mm", "left": "12mm", "right": "12mm"},
        display_header_footer=True,
        header_template='<div></div>',
        footer_template=FOOTER,
    )
    browser.close()

print("PDF OK", OUT)
