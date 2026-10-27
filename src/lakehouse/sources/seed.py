"""Synthetic source systems for a fictional industrial estate.

Everything here is generated: company names, people, IDs and documents are made up.
- ERP (Postgres `erp`): tenants, plots, employees (with PII), assets, purchase orders
- Weighbridge: one CSV per day in the landing zone
- Service desk: one JSON file per day in the landing zone
- Documents: lease agreements (text PDFs) and handover certificates (scanned image PDFs)
"""

from __future__ import annotations

import csv
import io
import json
import random
from datetime import date, datetime, timedelta, timezone

import psycopg
from faker import Faker

from ..settings import settings
from ..storage import put_bytes, s3_key

RNG_SEED = 42
TENANTS = [
    ("T001", "Aurora Packaging", "packaging"), ("T002", "Bluepeak Ceramics", "ceramics"),
    ("T003", "Cendana Food Processing", "food"), ("T004", "Delta Polymer Works", "plastics"),
    ("T005", "Equator Steel Fabrication", "metal"), ("T006", "Fajar Garment Lines", "textile"),
    ("T007", "Granite Logistics Hub", "logistics"), ("T008", "Horizon Battery Cells", "electronics"),
    ("T009", "Indigo Pharma Labs", "pharma"), ("T010", "Jade Furniture Craft", "furniture"),
    ("T011", "Kestrel Auto Parts", "automotive"), ("T012", "Lumen Solar Modules", "electronics"),
]
DEPARTMENTS = ["Operations", "Finance", "HR", "Security", "Engineering", "Tenant Relations", "IT"]
TICKET_CATEGORIES = ["electricity", "water", "road", "security", "waste", "permit", "internet"]

ERP_DDL = """
CREATE TABLE IF NOT EXISTS plot (
  plot_id text PRIMARY KEY, zone text, area_m2 int, status text, updated_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS tenant (
  tenant_id text PRIMARY KEY, name text, sector text, plot_id text, pic_name text, pic_email text,
  pic_phone text, tax_id text, status text, updated_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS employee (
  employee_id text PRIMARY KEY, full_name text, department text, position text, email text, phone text,
  national_id text, salary numeric(14,2), hired_at date, updated_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS asset (
  asset_id text PRIMARY KEY, asset_type text, zone text, purchase_value numeric(16,2), purchased_at date,
  status text, updated_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS purchase_order (
  po_id text PRIMARY KEY, vendor text, department text, amount numeric(16,2), currency text, status text,
  ordered_at date, updated_at timestamptz NOT NULL);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _phone(r: random.Random) -> str:
    return f"+62 8{r.randint(11, 59)}-{r.randint(1000, 9999)}-{r.randint(1000, 9999)}"


def _plot_id(i: int) -> str:
    return f"{'ABCD'[i % 4]}-{i + 1:02d}"


def seed_erp() -> dict[str, int]:
    """(Re)create the ERP source with deterministic synthetic rows."""
    r, fake = random.Random(RNG_SEED), Faker("id_ID")
    Faker.seed(RNG_SEED)
    now = _now()
    with psycopg.connect(settings().pg_dsn("erp"), autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS plot, tenant, employee, asset, purchase_order")
        conn.execute(ERP_DDL)
        plots = [(_plot_id(i), f"Zone {'ABCD'[i % 4]}", r.choice([5000, 7500, 10000, 12500, 20000]),
                  "leased" if i < len(TENANTS) else "available", now) for i in range(20)]
        conn.cursor().executemany("INSERT INTO plot VALUES (%s,%s,%s,%s,%s)", plots)
        tenants = []
        for i, (tid, name, sector) in enumerate(TENANTS):
            slug = name.split()[0].lower()
            tenants.append((tid, name, sector, _plot_id(i), fake.name(), f"pic@{slug}.example",
                            _phone(r), f"{r.randint(10, 99)}.{r.randint(100, 999)}.{r.randint(100, 999)}.{r.randint(1, 9)}"
                            f"-{r.randint(100, 999)}.000", "active", now))
        conn.cursor().executemany("INSERT INTO tenant VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", tenants)
        employees = []
        for i in range(60):
            first = fake.first_name()
            employees.append((f"E{i + 1:04d}", f"{first} {fake.last_name()}", r.choice(DEPARTMENTS),
                              r.choice(["Staff", "Supervisor", "Manager", "Officer"]),
                              f"{first.lower()}.{i + 1}@estate.example", _phone(r),
                              f"{r.randint(31, 36)}{r.randint(10, 99)}{r.randint(10**11, 10**12 - 1)}",
                              round(r.uniform(6_000_000, 45_000_000), -3),
                              date(2015, 1, 1) + timedelta(days=r.randint(0, 3500)), now))
        conn.cursor().executemany("INSERT INTO employee VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", employees)
        assets = [(f"A{i + 1:04d}", r.choice(["genset", "water pump", "street light", "cctv", "transformer",
                                             "e-bus charger", "weighbridge"]),
                   f"Zone {'ABCD'[i % 4]}", round(r.uniform(20e6, 2e9), -3),
                   date(2018, 1, 1) + timedelta(days=r.randint(0, 2500)),
                   r.choice(["active", "active", "active", "maintenance"]), now) for i in range(40)]
        conn.cursor().executemany("INSERT INTO asset VALUES (%s,%s,%s,%s,%s,%s,%s)", assets)
        pos = [(f"PO-{i + 1:05d}", fake.company(), r.choice(DEPARTMENTS), round(r.uniform(5e6, 900e6), -3), "IDR",
                r.choice(["approved", "approved", "received", "pending"]),
                date.today() - timedelta(days=r.randint(0, 365)), now) for i in range(150)]
        conn.cursor().executemany("INSERT INTO purchase_order VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", pos)
    return {"plot": 20, "tenant": len(TENANTS), "employee": 60, "asset": 40, "purchase_order": 150}


def write_weighbridge(days: int = 30, start: date | None = None) -> int:
    """One CSV per day: trucks weighed at the estate gate, attributed to tenants."""
    r = random.Random(RNG_SEED + 1)
    start = start or date.today() - timedelta(days=days)
    rows_written = 0
    for d in range(days):
        day = start + timedelta(days=d)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ticket_no", "tenant_id", "truck_plate", "gross_kg", "tare_kg", "weighed_at"])
        for n in range(r.randint(25, 60)):
            tid = r.choice(TENANTS)[0]
            tare = r.randint(7000, 14000)
            gross = tare + r.randint(2000, 28000)
            ts = datetime.combine(day, datetime.min.time()) + timedelta(minutes=r.randint(360, 1320))
            w.writerow([f"WB{day:%Y%m%d}{n:03d}", tid, f"B {r.randint(1000, 9999)} {r.choice('KLMNPRSTUVWXYZ')}"
                        f"{r.choice('ABCDEFGH')}", gross, tare, ts.isoformat()])
            rows_written += 1
        if d == days - 1:  # a duplicated row, as real exports have
            w.writerow([f"WB{day:%Y%m%d}000", "T001", "B 1000 KA", 20000, 10000,
                        datetime.combine(day, datetime.min.time()).isoformat()])
        put_bytes(s3_key("landing", "weighbridge", f"{day:%Y-%m-%d}.csv"), buf.getvalue().encode())
    return rows_written


def write_tickets(days: int = 30, start: date | None = None) -> int:
    """One JSON-lines file per day from the service desk."""
    r = random.Random(RNG_SEED + 2)
    start = start or date.today() - timedelta(days=days)
    n_total = 0
    for d in range(days):
        day = start + timedelta(days=d)
        lines = []
        for n in range(r.randint(3, 10)):
            opened = datetime.combine(day, datetime.min.time()) + timedelta(minutes=r.randint(420, 1080))
            closed = opened + timedelta(hours=r.uniform(1, 96)) if r.random() < 0.8 else None
            cat = r.choice(TICKET_CATEGORIES)
            lines.append(json.dumps({
                "ticket_id": f"TCK-{day:%Y%m%d}-{n:02d}", "tenant_id": r.choice(TENANTS)[0], "category": cat,
                "priority": r.choice(["low", "medium", "high"]), "opened_at": opened.isoformat(),
                "closed_at": closed.isoformat() if closed else None, "status": "closed" if closed else "open",
                "description": f"{cat.capitalize()} issue reported near {r.choice(['gate 1', 'gate 2', 'warehouse row', 'main road', 'substation'])}",
            }))
            n_total += 1
        put_bytes(s3_key("landing", "ticketing", f"{day:%Y-%m-%d}.jsonl"), "\n".join(lines).encode())
    return n_total


# ------------------------------------------------------------------------------------------ documents

def _lease_text(tid: str, name: str, plot: str, start: date, years: int, fee: int, signed: date,
                with_end: bool) -> list[str]:
    end = date(start.year + years, start.month, 1) - timedelta(days=1)
    term = (f"The lease starts on {start:%d %B %Y} and ends on {end:%d %B %Y}."
            if with_end else f"The lease starts on {start:%d %B %Y} for a period agreed separately by both parties.")
    return [
        f"LEASE AGREEMENT No. LA-{start.year}-{tid}",
        "",
        f"This Lease Agreement is made on {signed:%d %B %Y} between the Estate Management Company",
        f"(the Lessor) and {name}, tenant ID {tid} (the Lessee).",
        "",
        f"1. Premises. The Lessor leases to the Lessee industrial plot {plot}.",
        f"2. Term. {term}",
        f"3. Service fee. The Lessee pays a monthly service fee of IDR {fee:,} for estate services,",
        "   payable on the tenth day of each month.",
        "4. Utilities. Electricity, water and waste services are billed according to actual usage.",
        "5. Termination. Either party may terminate with six months written notice.",
        "",
        "Signed for and on behalf of the Lessor and the Lessee.",
    ]


def _handover_text(tid: str, name: str, plot: str, handed: date) -> list[str]:
    return [
        "HANDOVER CERTIFICATE",
        "",
        f"Plot {plot} is handed over to {name} (tenant ID {tid})",
        f"on {handed:%d %B %Y} in good condition.",
        "",
        "Checklist: boundary markers, utility connections, access road.",
        "Remarks: the tenant accepts the plot as inspected.",
    ]


def _text_pdf(lines: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in lines:
        c.setFont("Helvetica-Bold" if y == 800 else "Helvetica", 13 if y == 800 else 11)
        c.drawString(60, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


def _scanned_pdf(lines: list[str], r: random.Random) -> bytes:
    """Render the text to a slightly skewed, noisy image and wrap it in a PDF: no text layer, OCR needed."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    img = Image.new("L", (1240, 1754), 250)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 30)
    except OSError:
        font = ImageFont.load_default(size=30)
    y = 150
    for line in lines:
        draw.text((110, y), line, fill=25, font=font)
        y += 52
    for _ in range(2500):  # paper noise
        img.putpixel((r.randrange(1240), r.randrange(1754)), r.randint(150, 230))
    img = img.rotate(r.uniform(-0.8, 0.8), fillcolor=250).filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PDF", resolution=150)
    return buf.getvalue()


def write_documents(only: list[str] | None = None, seed_offset: int = 0) -> int:
    """Lease agreements (text PDFs) for every tenant and scanned handover certificates for most of them."""
    r = random.Random(RNG_SEED + 3 + seed_offset)
    written = 0
    for i, (tid, name, _sector) in enumerate(TENANTS):
        if only and tid not in only:
            continue
        plot = _plot_id(i)
        start = date(2023 + i % 3, 1 + (i * 5) % 12, 1)
        signed = start - timedelta(days=r.randint(10, 40))
        lease = _lease_text(tid, name, plot, start, r.choice([3, 5, 10]), r.randint(40, 250) * 1_000_000,
                            signed, with_end=(i % 5 != 4))  # every fifth lease lacks an end date
        put_bytes(s3_key("landing", "documents", f"{tid}_lease_agreement.pdf"), _text_pdf(lease))
        written += 1
        if i % 4 != 3:
            handed = start + timedelta(days=r.randint(5, 30))
            put_bytes(s3_key("landing", "documents", f"{tid}_handover_certificate.pdf"),
                      _scanned_pdf(_handover_text(tid, name, plot, handed), r))
            written += 1
    return written


def increment() -> dict[str, int]:
    """Simulate a day of changes so CDC and incremental processing have something to do."""
    r = random.Random()
    now = _now()
    with psycopg.connect(settings().pg_dsn("erp"), autocommit=True) as conn:
        upd_po = conn.execute(
            "UPDATE purchase_order SET status='received', updated_at=%s WHERE po_id IN "
            "(SELECT po_id FROM purchase_order WHERE status<>'received' ORDER BY random() LIMIT 20)", (now,)).rowcount
        n = conn.execute("SELECT count(*) FROM purchase_order").fetchone()[0]
        new = [(f"PO-{n + i + 1:05d}", "New Vendor Ltd", r.choice(DEPARTMENTS), round(r.uniform(5e6, 300e6), -3),
                "IDR", "pending", date.today(), now) for i in range(15)]
        conn.cursor().executemany("INSERT INTO purchase_order VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", new)
        upd_t = conn.execute("UPDATE tenant SET pic_phone=%s, updated_at=%s WHERE tenant_id='T003'",
                             (_phone(r), now)).rowcount
    wb = write_weighbridge(days=1, start=date.today())
    tk = write_tickets(days=1, start=date.today())
    docs = write_documents(only=["T003", "T008"], seed_offset=r.randint(1, 10_000))
    return {"po_updated": upd_po, "po_inserted": len(new), "tenant_updated": upd_t,
            "weighbridge_rows": wb, "tickets": tk, "documents": docs}


def seed_all() -> dict[str, object]:
    return {"erp": seed_erp(), "weighbridge_rows": write_weighbridge(), "tickets": write_tickets(),
            "documents": write_documents()}
