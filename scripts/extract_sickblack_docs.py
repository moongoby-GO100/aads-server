import os, sys
import pdfplumber

BASE = "/tmp/sickblack"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "all"
MAXP = int(sys.argv[2]) if len(sys.argv) > 2 else 6

FILES = {
    "juju": ("주주명부", "7bc585d9-0c48-4be1-83e5-05f98378a28f.pdf"),
    "deunggi": ("법인등기부등본", "9df77095-a449-4210-aa43-30de5d96869f.pdf"),
    "jeonggwan": ("정관", "fff5a615-81c4-44a3-93cc-4611eb566628.pdf"),
    "gyeyak": ("남양주창고계약서", "ee2d0b9f-8c9c-4a0f-814d-30a973fb4484.pdf"),
}

keys = list(FILES.keys()) if TARGET == "all" else [TARGET]
for k in keys:
    name, fn = FILES[k]
    p = os.path.join(BASE, fn)
    print("=" * 60)
    print("### " + name)
    print("=" * 60)
    if not os.path.exists(p):
        print("MISSING")
        continue
    try:
        with pdfplumber.open(p) as pdf:
            print("pages=%d" % len(pdf.pages))
            for i, pg in enumerate(pdf.pages):
                if i >= MAXP:
                    print("... (truncated)")
                    break
                t = (pg.extract_text() or "").strip()
                print("--- p%d chars=%d ---" % (i + 1, len(t)))
                print(t[:5000])
    except Exception as e:
        print("ERROR: %r" % (e,))
