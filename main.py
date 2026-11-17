import os
import hashlib
import io
import re
from datetime import datetime
import httpx
import pdfplumber
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

app = FastAPI()

# KONFIGURASI (Akan diisi lewat Environment Variables di Render.com nanti)
CALLBACK_URL = os.environ.get('CALLBACK_URL', 'https://domain-anda.com/api/arkas_callback.php')
SECRET_TOKEN = os.environ.get('SECRET_TOKEN', 'CakraArkAs2026!@#')
CURRENT_YEAR = datetime.now().year

class TriggerRequest(BaseModel):
    source_id: int
    url: str

def normalize_status(raw):
    if not raw: return ("Kosong", "")
    cleaned = re.sub(r'\s+', ' ', str(raw)).strip()
    if re.search(r'disetujui', cleaned, re.I): return ("Disetujui", cleaned)
    if re.search(r'belum\s*mengajukan', cleaned, re.I): return ("Belum Mengajukan", cleaned)
    if re.search(r'ditolak', cleaned, re.I): return ("Ditolak (silakan diperbaiki)", cleaned)
    if re.search(r'diajukan', cleaned, re.I): return ("Diajukan", cleaned)
    return (cleaned, cleaned)

def parse_pdf(content: bytes):
    rows = []
    seen_npsn = set()
    page_count = 0

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                raise ValueError("Struktur tabel tidak dikenali di halaman")
            
            for table in tables:
                for row in table:
                    if not row or all(c is None for c in row): continue
                    
                    joined = " ".join(str(c) for c in row if c).lower()
                    if "npsn" in joined and "nama sekolah" in joined: continue

                    cells = [re.sub(r'\s+', ' ', str(c)).strip() for c in row if c is not None]
                    if len(cells) < 15: continue

                    npsn = cells[0]
                    if not re.match(r'^\d{8}$', npsn): continue
                    if npsn in seen_npsn: raise ValueError(f"NPSN duplikat: {npsn}")
                    seen_npsn.add(npsn)

                    months_data = []
                    for m in cells[3:15]:
                        status, raw = normalize_status(m)
                        months_data.append({"status": status, "raw_status": raw})

                    rows.append({
                        "npsn": npsn,
                        "school_name": cells[1],
                        "kecamatan": cells[2],
                        "months": months_data
                    })
            page.flush_cache()

    return page_count, rows

@app.post("/api/poll")
async def poll_source(req: TriggerRequest, authorization: str = Header(None)):
    if authorization != f"Bearer {SECRET_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        # 1. Download PDF
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            resp = await client.get(req.url)
            if resp.status_code != 200 or 'pdf' not in resp.headers.get('content-type', '').lower():
                raise ValueError(f"Gagal download PDF. HTTP {resp.status_code}")
            
            content = resp.content
            file_hash = hashlib.sha256(content).hexdigest()

        # 2. Parse PDF
        page_count, schools = parse_pdf(content)
        
        if len(schools) < 10:
            raise ValueError("Jumlah sekolah terlalu sedikit, kemungkinan parsing gagal")

        # 3. Kirim ke cPanel (PHP)
        payload = {
            "source_id": req.source_id,
            "file_hash": file_hash,
            "page_count": page_count,
            "total_schools": len(schools),
            "report_year": CURRENT_YEAR,
            "schools": schools
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            cb_resp = await client.post(
                CALLBACK_URL,
                json=payload,
                headers={"Authorization": f"Bearer {SECRET_TOKEN}"}
            )
            
            if cb_resp.status_code != 200:
                raise ValueError(f"Gagal simpan ke DB cPanel: {cb_resp.text}")

        return {"status": "success", "message": f"Berhasil memproses {len(schools)} sekolah"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def health_check():
    return {"status": "ok", "service": "SI CAKRA - ARKAS Parser"}
