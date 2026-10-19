"""Generate synthetic Czech loan packages (FAKE persons only) as multi-page PDFs."""
from pathlib import Path
import json
import subprocess
import sys

try:
    from pypdf import PdfWriter
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pypdf"])
    from pypdf import PdfWriter

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.units import mm

pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DejaVuBold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
ROOT = Path(__file__).resolve().parents[1] / "samples"


def new(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(path.stem)
    return c


def header(c, title):
    c.setFont("DejaVuBold", 16)
    c.drawString(25 * mm, 280 * mm, title)
    c.setFont("DejaVu", 9)
    c.drawString(25 * mm, 274 * mm, "SYNTHETIC / FICTIONAL DATA — FinTechers Hackathon sample")
    c.line(25 * mm, 272 * mm, 185 * mm, 272 * mm)


def kv(c, y, rows):
    c.setFont("DejaVu", 11)
    for label, value in rows:
        c.drawString(30 * mm, y * mm, f"{label}:")
        c.setFont("DejaVuBold", 11)
        c.drawString(85 * mm, y * mm, str(value))
        c.setFont("DejaVu", 11)
        y -= 8
    return y


def contract(path, p):
    c = new(path); header(c, "ÚVĚROVÁ SMLOUVA / LOAN CONTRACT")
    kv(c, 255, [
        ("Contract number", p["contract_number"]),
        ("Client first name", p["first_name"]),
        ("Client last name", p["last_name"]),
        ("Date of birth", p["birth_date"]),
        ("Birth number (rodné číslo)", p["birth_number"]),
        ("Permanent address", p["address"]),
        ("Loan amount", f"{p['loan_amount']:,.0f} CZK".replace(",", " ")),
        ("Declared monthly net income", f"{p['declared_income']:,.0f} CZK".replace(",", " ")),
        ("Contract date", p["contract_date"]),
    ])
    c.setFont("DejaVuBold", 11)
    c.drawString(30 * mm, 175 * mm, "Required documents:")
    c.setFont("DejaVu", 11)
    labels = {
        "id_card": "Copy of national ID card (občanský průkaz)",
        "bank_statement": "Bank statement for the last 3 months",
        "payslip": "Latest payslip (výplatní lístek)",
        "proof_of_address": "Proof of address (utility bill)",
    }
    for i, d in enumerate(p["required_documents"]):
        c.drawString(35 * mm, (165 - i * 7) * mm, f"- {labels.get(d, d)}")
    c.setFont("DejaVu", 9)
    c.drawString(30 * mm, 40 * mm, "Signed in Prague. Synthetic document for demo purposes only.")
    c.showPage(); c.save()


def id_card(path, p):
    c = new(path); header(c, "OBČANSKÝ PRŮKAZ / CZECH ID CARD")
    kv(c, 250, [
        ("Given name", p["first_name"]),
        ("Surname", p["last_name"]),
        ("Date of birth", p["birth_date"]),
        ("Birth number", p["birth_number"]),
        ("Permanent residence", p["address"]),
        ("Document number", p["document_number"]),
        ("Date of expiry", p["expiry_date"]),
    ])
    c.rect(140 * mm, 200 * mm, 40 * mm, 50 * mm)
    c.setFont("DejaVu", 8); c.drawString(145 * mm, 220 * mm, "[photo]")
    c.showPage(); c.save()


def bank_statement(path, p):
    c = new(path); header(c, "BANK STATEMENT / VÝPIS Z ÚČTU")
    kv(c, 255, [
        ("Account holder", f"{p['first_name']} {p['last_name']}"),
        ("IBAN", p["iban"]),
        ("Period from", p["period_from"]),
        ("Period to", p["period_to"]),
        ("Salary credit", f"{p['salary']:,.2f} CZK".replace(",", " ")),
        ("Closing balance", f"{p['balance']:,.2f} CZK".replace(",", " ")),
    ])
    c.setFont("DejaVu", 10)
    c.drawString(30 * mm, 195 * mm, "Recent transactions:")
    c.drawString(35 * mm, 185 * mm, f"05.09.2026  SALARY ACME s.r.o.   +{p['salary']:,.2f} CZK")
    c.drawString(35 * mm, 177 * mm, "12.09.2026  Grocery store           -1 245,00 CZK")
    c.showPage(); c.save()


def payslip(path, p):
    c = new(path); header(c, "PAYSLIP / VÝPLATNÍ LÍSTEK")
    kv(c, 250, [
        ("Employee first name", p["first_name"]),
        ("Employee last name", p["last_name"]),
        ("Birth number", p["birth_number"]),
        ("Employer", p["employer"]),
        ("Pay period", p["pay_period"]),
        ("Net pay (net income)", f"{p['net_income']:,.2f} CZK".replace(",", " ")),
        ("Gross pay", f"{p['net_income'] * 1.35:,.2f} CZK".replace(",", " ")),
    ])
    c.showPage(); c.save()


def proof(path, p):
    c = new(path); header(c, "PROOF OF ADDRESS / POTVRZENÍ O ADRESE")
    kv(c, 250, [
        ("Customer full name", f"{p['first_name']} {p['last_name']}"),
        ("Supply address", p["address"]),
        ("Issuer", p["utility"]),
        ("Issue date", p["utility_date"]),
        ("Amount due", "1 890,00 CZK"),
    ])
    c.showPage(); c.save()


def merge_pdfs(paths, out):
    w = PdfWriter()
    for p in paths:
        w.append(str(p))
    with open(out, "wb") as f:
        w.write(f)


def base():
    return dict(
        first_name="Jana", last_name="Nováková",
        birth_date="12.04.1988", birth_number="885412/0000",
        address="Vinohradská 12, 120 00 Praha 2",
        contract_number="KB-2026-00421", loan_amount=250000,
        declared_income=52000, net_income=52000, salary=52000,
        contract_date="01.10.2026",
        required_documents=["id_card", "bank_statement", "payslip", "proof_of_address"],
        document_number="123456789", expiry_date="12.04.2032",
        iban="CZ65 0800 0000 0000 1234 5678",
        period_from="01.09.2026", period_to="30.09.2026",
        balance=87450.55, employer="ACME s.r.o.", pay_period="September 2026",
        utility="Pražská energetika a.s.", utility_date="15.09.2026",
    )


def build(name, parts, gt):
    pkg = ROOT / name
    pkg.mkdir(parents=True, exist_ok=True)
    for f in pkg.glob("*.pdf"):
        f.unlink()
    paths = []
    for key, fn, person in parts:
        path = pkg / f"{key}.pdf"
        fn(path, person)
        paths.append(path)
    merge_pdfs(paths, pkg / "package.pdf")
    (pkg / "ground_truth.json").write_text(json.dumps(gt, indent=2, ensure_ascii=False))
    print("wrote", name, "docs", [k for k, _, _ in parts])


def main():
    p = base()
    build("01_clean", [
        ("contract", contract, p), ("id_card", id_card, p),
        ("bank_statement", bank_statement, p), ("payslip", payslip, p),
        ("proof_of_address", proof, p),
    ], {
        "package_id": "01_clean",
        "person": {"first_name": "Jana", "last_name": "Nováková",
                   "birth_number": "885412/0000", "income": 52000},
        "expected_docs": ["contract", "id_card", "bank_statement", "payslip", "proof_of_address"],
        "expected_missing": [],
        "expected_issues": [],
        "expected_next_step": "approve_ready",
    })

    build("02_missing_payslip", [
        ("contract", contract, p), ("id_card", id_card, p),
        ("bank_statement", bank_statement, p), ("proof_of_address", proof, p),
    ], {
        "package_id": "02_missing_payslip",
        "expected_docs": ["contract", "id_card", "bank_statement", "proof_of_address"],
        "expected_missing": ["payslip"],
        "expected_issues": [],
        "expected_next_step": "request_missing_documents",
    })

    low = {**p, "net_income": 38000, "salary": 38000}
    build("03_amount_mismatch", [
        ("contract", contract, p),  # declared 52000
        ("id_card", id_card, p),
        ("bank_statement", bank_statement, low),
        ("payslip", payslip, low),
        ("proof_of_address", proof, p),
    ], {
        "package_id": "03_amount_mismatch",
        "declared_income": 52000, "actual_income": 38000,
        "expected_docs": ["contract", "id_card", "bank_statement", "payslip", "proof_of_address"],
        "expected_missing": [],
        "expected_issues": ["income_contract_vs_payslip", "income_contract_vs_statement"],
        "expected_next_step": "manual_review_discrepancy",
    })

    id_bad = {**p, "birth_number": "885412/0001"}
    pay_typo = {**p, "last_name": "Novakowa"}
    build("04_name_typo_invalid_rc", [
        ("contract", contract, p), ("id_card", id_card, id_bad),
        ("bank_statement", bank_statement, p), ("payslip", payslip, pay_typo),
        ("proof_of_address", proof, p),
    ], {
        "package_id": "04_name_typo_invalid_rc",
        "expected_docs": ["contract", "id_card", "bank_statement", "payslip", "proof_of_address"],
        "expected_missing": [],
        "expected_issues": ["birth_number_valid", "last_name_consistent", "birth_number_consistent"],
        "expected_next_step": "manual_review_discrepancy",
    })


if __name__ == "__main__":
    main()
