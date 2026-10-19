"""Hard synthetic packages 05-09 (FAKE persons only) + truth fields for every package.

Does NOT regenerate the PDFs of 01-04 (their bytes stay identical); it only adds
expected_page_types / expected_documents / expected_fields to their ground_truth.json.
Truth always comes from the generator inputs, never from engine output.
"""
from __future__ import annotations

import io
import json
import random
import sys
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageFilter
from reportlab.lib.units import mm

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_samples as g  # noqa: E402

ROOT = g.ROOT
SEED = 20261019


# ---------------- truth ----------------
def _iso(d):  # dd.mm.yyyy -> yyyy-mm-dd
    dd, mm_, yy = d.split(".")
    return f"{yy}-{mm_}-{dd}"


def truth(doc_type, p, two_page_missing_p2=False):
    if doc_type == "contract":
        return {"contract_number": p["contract_number"], "first_name": p["first_name"],
                "last_name": p["last_name"], "birth_date": _iso(p["birth_date"]),
                "birth_number": p["birth_number"], "address": p["address"],
                "loan_amount": p["loan_amount"], "declared_monthly_income": p["declared_income"],
                "contract_date": _iso(p["contract_date"]), "required_documents": p["required_documents"]}
    if doc_type == "id_card":
        return {"first_name": p["first_name"], "last_name": p["last_name"],
                "birth_date": _iso(p["birth_date"]), "birth_number": p["birth_number"],
                "address": p["address"], "document_number": p["document_number"],
                "expiry_date": _iso(p["expiry_date"])}
    if doc_type == "bank_statement":
        return {"account_holder": f"{p['first_name']} {p['last_name']}", "iban": p["iban"],
                "period_from": _iso(p["period_from"]), "period_to": _iso(p["period_to"]),
                "salary_credit": p["salary"],
                "closing_balance": None if two_page_missing_p2 else p["balance"]}
    if doc_type == "payslip":
        return {"first_name": p["first_name"], "last_name": p["last_name"],
                "birth_number": p["birth_number"], "employer": p["employer"],
                "net_income": p["net_income"], "period": p["pay_period"]}
    if doc_type == "proof_of_address":
        return {"full_name": f"{p['first_name']} {p['last_name']}", "address": p["address"],
                "issuer": p["utility"], "issue_date": _iso(p["utility_date"])}
    return {}


# ---------------- new document kinds ----------------
def bank_statement_2p(path, p, pages=(1, 2)):
    """Two-page statement with 'Page x of 2' and IBAN on both pages."""
    c = g.new(path)
    if 1 in pages:
        g.header(c, "BANK STATEMENT / VÝPIS Z ÚČTU")
        g.kv(c, 255, [("Account holder", f"{p['first_name']} {p['last_name']}"), ("IBAN", p["iban"]),
                      ("Period from", p["period_from"]), ("Period to", p["period_to"]),
                      ("Salary credit", f"{p['salary']:.2f} CZK")])
        c.setFont("DejaVu", 10)
        c.drawString(30 * mm, 200 * mm, "Transactions (continued on next page):")
        c.drawString(35 * mm, 190 * mm, f"05.09.2026  SALARY ACME s.r.o.   +{p['salary']:.2f} CZK")
        c.drawString(35 * mm, 182 * mm, "12.09.2026  Grocery store           -1 245,00 CZK")
        c.setFont("DejaVu", 9)
        c.drawString(150 * mm, 15 * mm, "Page 1 of 2 / Strana 1 z 2")
        c.showPage()
    if 2 in pages:
        g.header(c, "BANK STATEMENT / VÝPIS Z ÚČTU (continued)")
        g.kv(c, 255, [("IBAN", p["iban"]), ("Statement period", f"{p['period_from']} - {p['period_to']}")])
        c.setFont("DejaVu", 10)
        c.drawString(35 * mm, 225 * mm, "18.09.2026  Rent transfer           -14 500,00 CZK")
        c.drawString(35 * mm, 217 * mm, "25.09.2026  Card payment            -2 310,00 CZK")
        g.kv(c, 200, [("Closing balance", f"{p['balance']:.2f} CZK")])
        c.setFont("DejaVu", 9)
        c.drawString(150 * mm, 15 * mm, "Page 2 of 2 / Strana 2 z 2")
        c.showPage()
    c.save()


def insurance_certificate(path, p):
    c = g.new(path); g.header(c, "INSURANCE CERTIFICATE / POJIŠTĚNÍ DOMÁCNOSTI")
    g.kv(c, 250, [("Policy holder", f"{p['first_name']} {p['last_name']}"),
                  ("Policy number", "POJ-7781-2026"), ("Insurer", "Fiktivní pojišťovna a.s."),
                  ("Insured property", p["address"]), ("Annual premium", "3 480,00 CZK"),
                  ("Valid until", "31.12.2027")])
    c.showPage(); c.save()


# ---------------- degradations ----------------
def _render(pdf: Path, dpi: int):
    doc = fitz.open(pdf)
    out = []
    for pg in doc:
        pix = pg.get_pixmap(dpi=dpi)
        out.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    return out


def _scan(img: Image.Image, rnd: random.Random, rotate180=False):
    img = img.convert("L")
    img = img.rotate(rnd.uniform(-3, 3), resample=Image.BICUBIC, expand=True, fillcolor=235)
    arr = np.asarray(img).astype(np.float32)
    nrng = np.random.default_rng(rnd.randint(0, 10 ** 6))
    arr = arr * 0.92 + 12 + nrng.normal(0, 14, arr.shape)
    speck = nrng.random(arr.shape) < 0.002
    arr[speck] = 30
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
    if rotate180:
        img = img.rotate(180, expand=True)
    b = io.BytesIO()
    img.save(b, "JPEG", quality=40)
    return Image.open(io.BytesIO(b.getvalue())).convert("RGB")


def _images_to_pdf(imgs, out: Path, dpi: int):
    imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:], resolution=dpi)


def _photo_of_card(card_pdf: Path, out: Path, rnd: random.Random):
    page = _render(card_pdf, 150)[0]
    w, h = page.size
    card = page.crop((int(w * 0.08), int(h * 0.03), int(w * 0.92), int(h * 0.40)))
    cw, ch = card.size
    bg = Image.new("RGB", (int(cw * 1.35), int(ch * 1.6)), (120, 92, 64))  # 'wooden table'
    arr = np.asarray(bg).astype(np.float32)
    nrng = np.random.default_rng(7)
    stripes = (np.sin(np.arange(bg.size[0]) / 9.0) * 12)[None, :, None]
    arr = np.clip(arr + stripes + nrng.normal(0, 6, arr.shape), 0, 255).astype(np.uint8)
    bg = Image.fromarray(arr)
    tilted = card.rotate(-7, resample=Image.BICUBIC, expand=True, fillcolor=(0, 0, 0))
    mask = card.convert("L").point(lambda _: 255).rotate(-7, resample=Image.BICUBIC, expand=True)
    bg.paste(tilted, (int(cw * 0.12), int(ch * 0.25)), mask)
    W, H = bg.size  # mild perspective: top edge narrower
    coeffs = _perspective_coeffs([(0, 0), (W, 0), (W, H), (0, H)],
                                 [(W * 0.04, H * 0.02), (W * 0.97, 0), (W, H), (0, H * 0.98)])
    bg = bg.transform((W, H), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
    bg = bg.filter(ImageFilter.GaussianBlur(0.8))
    bg.save(out, "JPEG", quality=70)


def _perspective_coeffs(dst, src):
    A, B = [], []
    for (x, y), (u, v) in zip(dst, src):
        A += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]
        B += [u, v]
    return np.linalg.solve(np.array(A, float), np.array(B, float)).tolist()


# ---------------- packages ----------------
def _docs(entries):
    return [{"doc_type": t, "pages": pages} for t, pages in entries]


def write_gt(name, gt):
    (ROOT / name / "ground_truth.json").write_text(json.dumps(gt, indent=2, ensure_ascii=False))
    print("ground truth", name)


def main():
    rnd = random.Random(SEED)
    p = g.base()
    five = ["contract", "id_card", "bank_statement", "payslip", "proof_of_address"]

    # --- enrich 01-04 ground truth (PDFs untouched) ---
    low = {**p, "net_income": 38000, "salary": 38000}
    id_bad = {**p, "birth_number": "885412/0001"}
    pay_typo = {**p, "last_name": "Novakowa"}
    variants = {
        "01_clean": (five, {t: p for t in five}),
        "02_missing_payslip": (["contract", "id_card", "bank_statement", "proof_of_address"], {t: p for t in five}),
        "03_amount_mismatch": (five, {**{t: p for t in five}, "bank_statement": low, "payslip": low}),
        "04_name_typo_invalid_rc": (five, {**{t: p for t in five}, "id_card": id_bad, "payslip": pay_typo}),
    }
    for name, (types, persons) in variants.items():
        gtp = ROOT / name / "ground_truth.json"
        gt = json.loads(gtp.read_text())
        gt.update({
            "inputs": ["package.pdf"],
            "expected_page_types": types,
            "expected_documents": _docs([(t, [i + 1]) for i, t in enumerate(types)]),
            "expected_fields": {t: truth(t, persons[t]) for t in types},
            "expected_missing_pages": [], "expected_unexpected": [],
            "expected_flags": gt.get("expected_issues", []),
        })
        gtp.write_text(json.dumps(gt, indent=2, ensure_ascii=False))
        print("enriched", name)

    # --- 05 scanned / noisy / low-DPI / one page upside down (from 01) ---
    d = ROOT / "05_scanned_noisy"; d.mkdir(exist_ok=True)
    imgs = [_scan(im, rnd, rotate180=(i == 2)) for i, im in enumerate(_render(ROOT / "01_clean" / "package.pdf", 100))]
    _images_to_pdf(imgs, d / "package.pdf", 100)
    write_gt("05_scanned_noisy", {
        "package_id": "05_scanned_noisy", "inputs": ["package.pdf"],
        "description": "01_clean re-rendered at 100 dpi, ±3° skew, noise, JPEG q40, page 3 rotated 180°",
        "expected_docs": five, "expected_page_types": five,
        "expected_documents": _docs([(t, [i + 1]) for i, t in enumerate(five)]),
        "expected_fields": {t: truth(t, p) for t in five},
        "expected_missing": [], "expected_missing_pages": [], "expected_unexpected": [],
        "expected_flags": [], "expected_next_step": "approve_ready"})

    # --- 06 shuffled pages, one multi-doc PDF, 2-page statement ---
    req3 = ["id_card", "bank_statement", "payslip"]
    p6 = {**p, "required_documents": req3}
    d = ROOT / "06_shuffled"; d.mkdir(exist_ok=True)
    tmp = d / "_parts"; tmp.mkdir(exist_ok=True)
    g.contract(tmp / "contract.pdf", p6); g.id_card(tmp / "id_card.pdf", p6); g.payslip(tmp / "payslip.pdf", p6)
    bank_statement_2p(tmp / "statement.pdf", p6)
    from pypdf import PdfReader, PdfWriter
    st = PdfReader(str(tmp / "statement.pdf"))
    order = [(st, 1), (PdfReader(str(tmp / "payslip.pdf")), 0), (PdfReader(str(tmp / "contract.pdf")), 0),
             (PdfReader(str(tmp / "id_card.pdf")), 0), (st, 0)]
    w = PdfWriter()
    for r, i in order:
        w.add_page(r.pages[i])
    with open(d / "package.pdf", "wb") as f:
        w.write(f)
    for x in tmp.iterdir():
        x.unlink()
    tmp.rmdir()
    write_gt("06_shuffled", {
        "package_id": "06_shuffled", "inputs": ["package.pdf"],
        "description": "physical order: statement p2, payslip, contract, id, statement p1",
        "expected_docs": ["bank_statement", "payslip", "contract", "id_card"],
        "expected_page_types": ["bank_statement", "payslip", "contract", "id_card", "bank_statement"],
        "expected_documents": _docs([("bank_statement", [5, 1]), ("payslip", [2]), ("contract", [3]), ("id_card", [4])]),
        "expected_fields": {"contract": truth("contract", p6), "id_card": truth("id_card", p6),
                            "bank_statement": truth("bank_statement", p6), "payslip": truth("payslip", p6)},
        "expected_missing": [], "expected_missing_pages": [], "expected_unexpected": [],
        "expected_flags": [], "expected_next_step": "approve_ready"})

    # --- 07 missing page: statement page 2 of 2 absent ---
    req2 = ["id_card", "bank_statement"]
    p7 = {**p, "required_documents": req2}
    d = ROOT / "07_missing_page"; d.mkdir(exist_ok=True)
    g.contract(d / "contract.pdf", p7); g.id_card(d / "id_card.pdf", p7)
    bank_statement_2p(d / "bank_statement_page1.pdf", p7, pages=(1,))
    g.merge_pdfs([d / "contract.pdf", d / "id_card.pdf", d / "bank_statement_page1.pdf"], d / "package.pdf")
    for x in ("contract.pdf", "id_card.pdf", "bank_statement_page1.pdf"):
        (d / x).unlink()
    write_gt("07_missing_page", {
        "package_id": "07_missing_page", "inputs": ["package.pdf"],
        "expected_docs": ["contract", "id_card", "bank_statement"],
        "expected_page_types": ["contract", "id_card", "bank_statement"],
        "expected_documents": _docs([("contract", [1]), ("id_card", [2]), ("bank_statement", [3])]),
        "expected_fields": {"contract": truth("contract", p7), "id_card": truth("id_card", p7),
                            "bank_statement": truth("bank_statement", p7, two_page_missing_p2=True)},
        "expected_missing": [],
        "expected_missing_pages": [{"doc_type": "bank_statement", "page_count": 2, "missing_page_numbers": [2]}],
        "expected_unexpected": [], "expected_flags": ["pages_complete"],
        "expected_next_step": "request_missing_documents"})

    # --- 08 extra unrequested documents ---
    req8 = ["id_card", "payslip"]
    p8 = {**p, "required_documents": req8}
    d = ROOT / "08_extra_document"; d.mkdir(exist_ok=True)
    g.contract(d / "contract.pdf", p8); g.id_card(d / "id_card.pdf", p8); g.payslip(d / "payslip.pdf", p8)
    g.proof(d / "proof_of_address.pdf", p8); insurance_certificate(d / "insurance.pdf", p8)
    parts = ["contract", "id_card", "payslip", "proof_of_address", "insurance"]
    g.merge_pdfs([d / f"{x}.pdf" for x in parts], d / "package.pdf")
    for x in parts:
        (d / f"{x}.pdf").unlink()
    types8 = ["contract", "id_card", "payslip", "proof_of_address", "other"]
    write_gt("08_extra_document", {
        "package_id": "08_extra_document", "inputs": ["package.pdf"],
        "expected_docs": types8, "expected_page_types": types8,
        "expected_documents": _docs([(t, [i + 1]) for i, t in enumerate(types8)]),
        "expected_fields": {t: truth(t, p8) for t in types8 if t != "other"},
        "expected_missing": [], "expected_missing_pages": [],
        "expected_unexpected": ["other", "proof_of_address"], "expected_flags": ["no_unrequested_documents"],
        "expected_next_step": "spot_check_warnings"})

    # --- 09 photo of the ID card (skew, perspective, background) ---
    req1 = ["id_card"]
    p9 = {**p, "required_documents": req1}
    d = ROOT / "09_photo_id"; d.mkdir(exist_ok=True)
    g.contract(d / "contract.pdf", p9)
    g.id_card(d / "_id.pdf", p9)
    _photo_of_card(d / "_id.pdf", d / "id_photo.jpg", rnd)
    (d / "_id.pdf").unlink()
    write_gt("09_photo_id", {
        "package_id": "09_photo_id", "inputs": ["contract.pdf", "id_photo.jpg"],
        "expected_docs": ["contract", "id_card"], "expected_page_types": ["contract", "id_card"],
        "expected_documents": [{"doc_type": "contract", "pages": [1], "source_file": "contract.pdf"},
                               {"doc_type": "id_card", "pages": [1], "source_file": "id_photo.jpg"}],
        "expected_fields": {"contract": truth("contract", p9), "id_card": truth("id_card", p9)},
        "expected_missing": [], "expected_missing_pages": [], "expected_unexpected": [],
        "expected_flags": [], "expected_next_step": "approve_ready"})


if __name__ == "__main__":
    main()
