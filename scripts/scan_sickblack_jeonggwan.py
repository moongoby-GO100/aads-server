import re
import pdfplumber

P = "/tmp/sickblack/fff5a615-81c4-44a3-93cc-4611eb566628.pdf"
KEYS = ["주주총회", "특별결의", "의결", "정족수", "이사의 수", "대표이사", "이사회", "감사", "임기", "소집", "결의"]

with pdfplumber.open(P) as pdf:
    for i, pg in enumerate(pdf.pages):
        t = (pg.extract_text() or "")
        if not any(k in t for k in KEYS):
            continue
        print("=" * 55)
        print("PAGE %d" % (i + 1))
        print("=" * 55)
        print(t.strip()[:4500])
