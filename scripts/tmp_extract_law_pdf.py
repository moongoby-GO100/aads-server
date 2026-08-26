import sys
from pypdf import PdfReader

BASE = "/root/aads/uploads/chat/files/efccec7c-0788-4564-a2cf-265c63d075f0/"
FILES = {
    "deunggi": "9df77095-a449-4210-aa43-30de5d96869f.pdf",
    "jujumyeongbu": "7bc585d9-0c48-4be1-83e5-05f98378a28f.pdf",
    "jeonggwan": "fff5a615-81c4-44a3-93cc-4611eb566628.pdf",
}

key = sys.argv[1]
reader = PdfReader(BASE + FILES[key])
out = []
for i, page in enumerate(reader.pages):
    t = page.extract_text() or ""
    out.append("=== PAGE %d ===" % (i + 1))
    out.append(t)
text = "\n".join(out)
with open("/tmp/law_%s.txt" % key, "w") as f:
    f.write(text)
print("pages=%d chars=%d" % (len(reader.pages), len(text)))
