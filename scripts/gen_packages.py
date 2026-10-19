"""Synthetic Czech loan packages v0.4 (FICTIONAL persons/companies only).

  python scripts/gen_packages.py            # rewrites samples/<NN_name>/ (PDF/JPG + ground_truth.json)

Design (Carlos, 19-oct): each package = a Contract that lists 5-7 DIFFERENT required document
types, Czech market (rodné číslo, Czech names/addresses/companies, Czech + English labels),
>= 2 layouts per document type, several fonts, multi-page statements with "Strana x z y".
Ground truth is written from the generator INPUTS (what is printed), never from engine output.
Expected flags are derived from what each package was built to contain (see CASES).
"""
from __future__ import annotations

import io
import json
import random
import shutil
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageFilter
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1] / "samples"
SEED = 20261019
_F = {
    "sans": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "sans-b": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "serif": "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "serif-b": "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "lib": "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "lib-b": "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "lser": "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "lser-b": "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "mono": "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
}
for _n, _p in _F.items():
    pdfmetrics.registerFont(TTFont(_n, _p))
GREY = Color(0.35, 0.35, 0.35)
LIGHT = Color(0.93, 0.95, 0.98)
MONTHS_CS = ["ledna", "února", "března", "dubna", "května", "června", "července", "srpna",
             "září", "října", "listopadu", "prosince"]
TYPES7 = ["contract", "id_card", "bank_statement", "payslip", "proof_of_address",
          "employer_confirmation", "tax_or_debt_certificate"]


# ---------------- formatting ----------------
def d_num(iso):                       # 2026-10-01 -> 01.10.2026
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def d_cs(iso):                        # 2026-10-01 -> 1. října 2026
    y, m, d = map(int, iso.split("-"))
    return f"{d}. {MONTHS_CS[m - 1]} {y}"


def money_en(x):
    return f"{x:,.2f} CZK".replace(",", " ")      # 52 000.00 CZK


def money_cs(x):
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} Kč"                                # 52 000,00 Kč


def rc_fmt(rc):
    return f"{rc[:6]}/{rc[6:]}"


def make_rc(birth_iso, female, serial):
    """Valid 10-digit rodné číslo: yymmdd(+50 women) + 3-digit serial + check digit (mod 11)."""
    y, m, d = map(int, birth_iso.split("-"))
    mm_ = m + (50 if female else 0)
    for s in range(serial, serial + 50):
        first9 = f"{y % 100:02d}{mm_:02d}{d:02d}{s % 1000:03d}"
        if int(first9) % 11 == 10:
            continue                         # legacy check digit 0 only allowed until 1985
        for cd in range(10):
            if int(first9 + str(cd)) % 11 == 0:
                return first9 + str(cd)
    raise ValueError("no valid RC")


# ---------------- page primitives ----------------
class Page:
    def __init__(self, c, font, bold):
        self.c, self.font, self.bold = c, font, bold

    def text(self, x, y, s, size=10.5, bold=False, color=black, font=None):
        self.c.setFillColor(color)
        self.c.setFont(font or (self.bold if bold else self.font), size)
        self.c.drawString(x * mm, y * mm, str(s))
        self.c.setFillColor(black)

    def width(self, s, size=10.5, bold=False):
        return self.c.stringWidth(str(s), self.bold if bold else self.font, size) / mm

    def centered(self, y, s, size=14, bold=True):
        self.c.setFont(self.bold if bold else self.font, size)
        self.c.drawCentredString(105 * mm, y * mm, str(s))

    def synthetic(self, y=287):
        self.text(20, y, "SYNTETICKÝ DOKUMENT / SYNTHETIC — FinTechers Hackathon, fictional data", 7, color=GREY)

    def kv(self, x, y, rows, size=10.5, gap=7.5, min_val_x=80):
        for label, value in rows:
            self.text(x, y, f"{label}:", size)
            vx = max(min_val_x, x + self.width(f"{label}:", size) + 4)
            self.text(vx, y, value, size, bold=True)
            y -= gap
        return y

    def grid(self, x, y, rows, col_w=85, row_h=13, cols=2):
        """Boxed form: small grey label (CZ / EN) above the bold value."""
        for i, (label, value) in enumerate(rows):
            cx = x + (i % cols) * col_w
            cy = y - (i // cols) * row_h
            self.c.setFillColor(LIGHT)
            self.c.rect(cx * mm, (cy - row_h + 2) * mm, (col_w - 3) * mm, (row_h - 1) * mm, fill=1, stroke=1)
            self.c.setFillColor(black)
            self.text(cx + 2, cy - 3.5, label, 7, color=GREY)
            self.text(cx + 2, cy - 9, value, 10, bold=True)
        return y - ((len(rows) + cols - 1) // cols) * row_h

    def para(self, x, y, text, size=11, width=170, lead=6.2):
        words, line = text.split(), ""
        for w in words:
            t = (line + " " + w).strip()
            if self.width(t, size) > width:
                self.text(x, y, line, size); y -= lead; line = w
            else:
                line = t
        if line:
            self.text(x, y, line, size); y -= lead
        return y

    def marker(self, n, total):
        self.text(160, 12, f"Strana {n} z {total} / Page {n} of {total}", 8)


def new_canvas(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(path.stem)
    return c


# ---------------- documents (each renderer: layout A/B/C) ----------------
REQ_LABEL = {
    "en": {"id_card": "Copy of national ID card (občanský průkaz)",
           "bank_statement": "Bank statement for the last 3 months",
           "payslip": "Latest payslip (výplatní lístek)",
           "proof_of_address": "Proof of address (utility bill)",
           "employer_confirmation": "Employer confirmation of employment and income",
           "tax_or_debt_certificate": "Certificate of no tax arrears (potvrzení o bezdlužnosti)"},
    "cs": {"id_card": "Kopie občanského průkazu",
           "bank_statement": "Výpis z bankovního účtu za poslední 3 měsíce",
           "payslip": "Výplatní lístek za poslední měsíc",
           "proof_of_address": "Doklad o bydlišti (faktura za energie)",
           "employer_confirmation": "Potvrzení o zaměstnání a výši příjmu",
           "tax_or_debt_certificate": "Potvrzení o bezdlužnosti (Finanční úřad)"},
}


def contract(c, p, layout):
    k = p["case"]
    if layout == "A":
        pg = Page(c, "sans", "sans-b"); pg.synthetic()
        pg.text(20, 275, "ÚVĚROVÁ SMLOUVA / LOAN CONTRACT", 16, bold=True)
        pg.text(20, 268, "Komerční banka, a.s. — consumer loan (fictional sample)", 9, color=GREY)
        c.line(20 * mm, 265 * mm, 190 * mm, 265 * mm)
        pg.kv(25, 255, [
            ("Contract number", k["contract_number"]), ("Client first name", p["first_name"]),
            ("Client last name", p["last_name"]), ("Date of birth", d_num(p["birth_date"])),
            ("Birth number (rodné číslo)", rc_fmt(p["rc"])), ("Permanent address", p["address"]),
            ("Loan amount", money_en(k["loan_amount"])),
            ("Declared monthly net income", money_en(k["declared_income"])),
            ("Contract date", d_num(k["contract_date"]))])
        pg.text(25, 175, "Required documents:", 11, bold=True)
        for i, t in enumerate(k["required"]):
            pg.text(30, 167 - i * 7, f"- {REQ_LABEL['en'][t]}", 10.5)
        pg.text(25, 40, "Signed in Prague. Synthetic document for demo purposes only.", 9)
    else:
        pg = Page(c, "serif", "serif-b"); pg.synthetic()
        pg.centered(272, f"ÚVĚROVÁ SMLOUVA č. {k['contract_number']}", 15)
        pg.centered(265, "Loan contract — Komerční banka, a.s. (fiktivní vzor)", 9, bold=False)
        y = pg.grid(20, 255, [
            ("Číslo smlouvy / Contract number", k["contract_number"]),
            ("Datum smlouvy / Contract date", d_cs(k["contract_date"])),
            ("Jméno / First name", p["first_name"]), ("Příjmení / Last name", p["last_name"]),
            ("Datum narození / Date of birth", d_num(p["birth_date"])),
            ("Rodné číslo / Birth number", rc_fmt(p["rc"])),
            ("Výše úvěru / Loan amount", money_cs(k["loan_amount"])),
            ("Deklarovaný čistý měsíční příjem / Declared monthly net income", money_cs(k["declared_income"])),
        ])
        pg.grid(20, y, [("Trvalé bydliště / Permanent address", p["address"])], col_w=170, cols=1)
        pg.text(20, y - 22, "Požadované dokumenty:", 11.5, bold=True)
        for i, t in enumerate(k["required"]):
            pg.text(25, y - 30 - i * 6.5, f"{i + 1}) {REQ_LABEL['cs'][t]}", 10.5)
        pg.text(20, 45, "V Praze dne " + d_cs(k["contract_date"]) + ".   Podpis klienta: ____________", 10)
    c.showPage()


def id_card(c, p, layout):
    if layout == "A":
        pg = Page(c, "sans", "sans-b"); pg.synthetic()
        pg.text(20, 275, "OBČANSKÝ PRŮKAZ / CZECH ID CARD", 16, bold=True)
        c.line(20 * mm, 270 * mm, 190 * mm, 270 * mm)
        pg.kv(25, 255, [
            ("Given name", p["first_name"]), ("Surname", p["last_name"]),
            ("Date of birth", d_num(p["birth_date"])), ("Birth number", rc_fmt(p["id_rc"])),
            ("Permanent residence", p["address"]), ("Document number", p["doc_number"]),
            ("Date of expiry", d_num(p["id_expiry"]))], min_val_x=75)
        c.rect(150 * mm, 205 * mm, 35 * mm, 45 * mm)
        pg.text(160, 226, "[foto]", 8)
    else:
        pg = Page(c, "lib", "lib-b"); pg.synthetic()
        c.setFillColor(Color(0.86, 0.91, 0.97))
        c.roundRect(15 * mm, 175 * mm, 180 * mm, 100 * mm, 6 * mm, fill=1, stroke=1)
        c.setFillColor(black)
        pg.text(20, 266, "ČESKÁ REPUBLIKA · CZECH REPUBLIC", 9, bold=True)
        pg.text(20, 259, "OBČANSKÝ PRŮKAZ / IDENTITY CARD", 14, bold=True)
        c.rect(22 * mm, 190 * mm, 32 * mm, 42 * mm)
        pg.text(31, 210, "foto", 8, color=GREY)
        rows = [("Příjmení / Surname", p["last_name"]), ("Jméno / Given names", p["first_name"]),
                ("Datum narození / Date of birth", d_num(p["birth_date"])),
                ("Rodné číslo / Personal No.", rc_fmt(p["id_rc"])),
                ("Číslo dokladu / Document No.", p["doc_number"]),
                ("Platnost do / Date of expiry", d_num(p["id_expiry"]))]
        y = 248
        for lab, val in rows:
            pg.text(62, y, lab, 7, color=GREY); pg.text(62, y - 4.5, val, 10.5, bold=True); y -= 11
        pg.text(20, 165, "Trvalý pobyt / Permanent residence:", 8, color=GREY)
        pg.text(20, 159, p["address"], 10.5, bold=True)
    c.showPage()


def bank_statement(c, p, layout, pages=(1, 2), total=2):
    s = p["stmt"]
    for n in pages:
        if layout == "A":
            pg = Page(c, "sans", "sans-b"); pg.synthetic()
            pg.text(20, 275, "BANK STATEMENT / VÝPIS Z ÚČTU" + (" (continued)" if n > 1 else ""), 15, bold=True)
            pg.text(20, 268, s["bank"], 9, color=GREY)
            c.line(20 * mm, 265 * mm, 190 * mm, 265 * mm)
            if n == 1:
                pg.kv(25, 255, [("Account holder", f"{p['first_name']} {p['last_name']}"),
                                ("IBAN", s["iban"]), ("Period from", d_num(s["from"])),
                                ("Period to", d_num(s["to"])), ("Salary credit", money_en(s["salary"]))])
                y = 205
                pg.text(25, y, "Transactions:", 10, bold=True)
                for i, (dt, txt, amt) in enumerate(s["tx"][:3]):
                    pg.text(28, y - 8 - i * 7, f"{d_num(dt)}  {txt:<34} {amt}", 9.5, font="mono")
            else:
                pg.kv(25, 255, [("IBAN", s["iban"]), ("Statement period", f"{d_num(s['from'])} - {d_num(s['to'])}")])
                for i, (dt, txt, amt) in enumerate(s["tx"][3:]):
                    pg.text(28, 230 - i * 7, f"{d_num(dt)}  {txt:<34} {amt}", 9.5, font="mono")
                pg.kv(25, 190, [("Closing balance", money_en(s["balance"]))])
        else:
            pg = Page(c, "lib", "lib-b"); pg.synthetic()
            c.setFillColor(Color(0.1, 0.25, 0.45)); c.rect(0, 272 * mm, 210 * mm, 15 * mm, fill=1, stroke=0)
            c.setFillColor(black)
            pg.text(15, 277, s["bank"] + "  ·  VÝPIS Z ÚČTU / ACCOUNT STATEMENT", 12, bold=True, color=Color(1, 1, 1))
            if n == 1:
                y = pg.grid(15, 265, [("Majitel účtu / Account holder", f"{p['first_name']} {p['last_name']}"),
                                      ("IBAN", s["iban"]),
                                      ("Období od / Period from", d_num(s["from"])),
                                      ("Období do / Period to", d_num(s["to"]))], col_w=90)
                pg.text(15, y - 6, "Datum      Popis / Description                     Částka / Amount", 8.5,
                        bold=True, font="mono")
                for i, (dt, txt, amt) in enumerate(s["tx"][:3]):
                    pg.text(15, y - 14 - i * 6.5, f"{d_num(dt)} {txt:<38} {amt:>16}", 8.5, font="mono")
                pg.text(15, y - 40, f"Připsaná mzda / Salary credit: {money_cs(s['salary'])}", 10.5, bold=True)
            else:
                pg.text(15, 262, f"IBAN: {s['iban']}   Období: {d_num(s['from'])} – {d_num(s['to'])}", 9.5)
                for i, (dt, txt, amt) in enumerate(s["tx"][3:]):
                    pg.text(15, 250 - i * 6.5, f"{d_num(dt)} {txt:<38} {amt:>16}", 8.5, font="mono")
                pg.text(15, 220, f"Konečný zůstatek / Closing balance: {money_cs(s['balance'])}", 10.5, bold=True)
        pg.marker(n, total)
        c.showPage()


def payslip(c, p, layout):
    w = p["pay"]
    if layout == "A":
        pg = Page(c, "sans", "sans-b"); pg.synthetic()
        pg.text(20, 275, "PAYSLIP / VÝPLATNÍ LÍSTEK", 16, bold=True)
        c.line(20 * mm, 270 * mm, 190 * mm, 270 * mm)
        pg.kv(25, 255, [("Employee first name", p["first_name"]), ("Employee last name", w["last_name"]),
                        ("Birth number", rc_fmt(p["rc"])), ("Employer", w["employer"]),
                        ("Pay period", w["period_en"]), ("Gross pay", money_en(w["gross"])),
                        ("Net pay (net income)", money_en(w["net"]))])
    else:
        pg = Page(c, "lser", "lser-b"); pg.synthetic()
        pg.text(15, 272, w["employer"], 13, bold=True)
        pg.text(15, 266, f"IČO {p['ico']} · mzdová účtárna", 9, color=GREY)
        pg.centered(252, f"VÝPLATNÍ LÍSTEK — {w['period_cs']}", 14)
        pg.grid(15, 244, [("Zaměstnanec — jméno / First name", p["first_name"]),
                          ("Zaměstnanec — příjmení / Last name", w["last_name"]),
                          ("Rodné číslo / Birth number", rc_fmt(p["rc"])),
                          ("Zaměstnavatel / Employer", w["employer"]),
                          ("Hrubá mzda / Gross pay", money_cs(w["gross"])),
                          ("Odvody a daň / Deductions", money_cs(w["gross"] - w["net"])),
                          ("Čistá mzda k výplatě / Net pay", money_cs(w["net"])),
                          ("Období / Pay period", w["period_cs"])], col_w=90)
    c.showPage()


def proof_of_address(c, p, layout):
    u = p["util"]
    if layout == "A":
        pg = Page(c, "sans", "sans-b"); pg.synthetic()
        pg.text(20, 275, "PROOF OF ADDRESS / DOKLAD O BYDLIŠTI", 15, bold=True)
        c.line(20 * mm, 270 * mm, 190 * mm, 270 * mm)
        pg.kv(25, 255, [("Customer full name", f"{p['first_name']} {p['last_name']}"),
                        ("Supply address", p["address"]), ("Issuer", u["issuer"]),
                        ("Issue date", d_num(u["date"])), ("Amount due", money_en(u["amount"]))])
    else:
        pg = Page(c, "lib", "lib-b"); pg.synthetic()
        pg.text(15, 272, u["issuer"], 13, bold=True)
        pg.text(15, 266, "FAKTURA — VYÚČTOVÁNÍ ELEKTŘINY / ELECTRICITY BILL", 10.5)
        pg.grid(15, 255, [("Odběratel / Customer", f"{p['first_name']} {p['last_name']}"),
                          ("Datum vystavení / Issue date", d_num(u["date"])),
                          ("Adresa odběrného místa / Supply address", p["address"]),
                          ("K úhradě / Amount due", money_cs(u["amount"]))], col_w=90)
    c.showPage()


def employer_confirmation(c, p, layout):
    e = p["emp"]
    he = "pan" if not p["female"] else "paní"
    employed = "zaměstnán" if not p["female"] else "zaměstnána"
    if layout == "B":
        pg = Page(c, "lib", "lib-b"); pg.synthetic()
        pg.centered(272, "POTVRZENÍ O ZAMĚSTNÁNÍ A VÝŠI PŘÍJMU", 14)
        pg.centered(265, "Confirmation of employment and income (pro účely úvěru)", 9, bold=False)
        y = pg.grid(15, 255, [
            ("Zaměstnanec / Employee", e["employee_name"]),
            ("Rodné číslo / Birth number", rc_fmt(p["rc"])),
            ("Zaměstnavatel / Employer", e["employer"]), ("IČO / Company ID", p["ico"]),
            ("Pracovní pozice / Position", e["position"]),
            ("V pracovním poměru od / Employed since", d_num(e["start"])),
            ("Druh pracovního poměru / Contract type", e["contract_type"]),
            ("Průměrný měsíční čistý příjem / Average monthly net income", money_cs(e["net"])),
            ("Datum vystavení / Issue date", d_num(e["issued"]))], col_w=90)
        pg.text(15, y - 10, "Zaměstnanec není ve zkušební ani výpovědní lhůtě.", 10)
        c.rect(120 * mm, (y - 45) * mm, 70 * mm, 25 * mm)
        pg.text(124, y - 25, "razítko a podpis zaměstnavatele", 8, color=GREY)
    else:
        pg = Page(c, "serif", "serif-b"); pg.synthetic()
        pg.text(20, 272, e["employer"], 14, bold=True)
        pg.text(20, 266, f"{p['employer_address']} · IČO {p['ico']}", 9, color=GREY)
        c.line(20 * mm, 262 * mm, 190 * mm, 262 * mm)
        pg.text(20, 250, f"V {p['city']} dne {d_cs(e['issued'])}", 10.5)
        pg.text(20, 236, "POTVRZENÍ O ZAMĚSTNÁNÍ", 13, bold=True)
        y = pg.para(20, 224, (
            f"Potvrzujeme, že {he} {e['employee_name']}, rodné číslo {rc_fmt(p['rc'])}, je {employed} "
            f"v naší společnosti {e['employer']} (IČO {p['ico']}) od {d_num(e['start'])} na pozici "
            f"{e['position']}. Pracovní poměr je sjednán {e['contract_type']}. Zaměstnanec není ve "
            f"zkušební ani výpovědní lhůtě."))
        y = pg.para(20, y - 4, (f"Průměrný měsíční čistý příjem za poslední tři měsíce činí "
                                 f"{money_cs(e['net'])}."))
        pg.para(20, y - 4, "Toto potvrzení se vydává pro účely žádosti o úvěr.")
        pg.text(120, 120, "Ing. Hana Malá, personální oddělení", 10)
        pg.text(120, 113, "(razítko, podpis)", 8, color=GREY)
    c.showPage()


def tax_certificate(c, p, layout):
    t = p["tax"]
    if layout == "B":
        pg = Page(c, "lib", "lib-b"); pg.synthetic()
        pg.text(15, 272, f"FINANČNÍ ÚŘAD {t['office_head']}", 12, bold=True)
        pg.text(15, 266, f"Územní pracoviště {t['branch']}", 9.5)
        pg.centered(250, "POTVRZENÍ O BEZDLUŽNOSTI", 15)
        pg.centered(243, "Certificate of no tax arrears", 9, bold=False)
        y = pg.grid(15, 235, [("Č. j. / Reference No.", t["number"]),
                              ("Datum vydání / Date of issue", d_num(t["issued"])),
                              ("Daňový subjekt / Taxpayer", t["holder"]),
                              ("Rodné číslo / Birth number", rc_fmt(t["rc"]))], col_w=90)
        pg.para(15, y - 8, t["status"], 11)
        pg.text(15, y - 30, f"Správce daně: Finanční úřad {t['office_head']}", 9.5)
    else:
        pg = Page(c, "lser", "lser-b"); pg.synthetic()
        pg.centered(272, f"Finanční úřad {t['office_head']}", 13)
        pg.centered(265, f"Územní pracoviště {t['branch']}", 10, bold=False)
        pg.text(20, 250, f"Č. j.: {t['number']}", 10.5)
        pg.text(140, 250, f"Dne {d_cs(t['issued'])}", 10.5)
        pg.text(20, 232, "POTVRZENÍ O BEZDLUŽNOSTI", 14, bold=True)
        y = pg.para(20, 220, (f"Na základě žádosti daňového subjektu {t['holder']}, rodné číslo "
                              f"{rc_fmt(t['rc'])}, správce daně potvrzuje následující skutečnost:"), 11.5)
        y = pg.para(20, y - 4, t["status"], 11.5)
        pg.para(20, y - 4, "Potvrzení se vydává na žádost daňového subjektu a je platné ke dni vydání.", 11)
        pg.text(120, 120, "otisk úředního razítka", 8, color=GREY)
    c.showPage()


def insurance(c, p, layout="A"):
    pg = Page(c, "sans", "sans-b"); pg.synthetic()
    pg.text(20, 275, "POJISTNÁ SMLOUVA / HOME INSURANCE POLICY", 15, bold=True)
    pg.kv(25, 255, [("Policy holder", f"{p['first_name']} {p['last_name']}"), ("Policy number", "POJ-7781-2026"),
                    ("Insurer", "Fiktivní pojišťovna a.s."), ("Insured property", p["address"]),
                    ("Annual premium", "3 480,00 Kč"), ("Valid until", "31.12.2027")])
    c.showPage()


RENDER = {"contract": contract, "id_card": id_card, "bank_statement": bank_statement,
          "payslip": payslip, "proof_of_address": proof_of_address,
          "employer_confirmation": employer_confirmation, "tax_or_debt_certificate": tax_certificate,
          "other": insurance}


# ---------------- persons & cases ----------------
PEOPLE = [
    # first, last, female, birth, address, city(locative), employer, employer address, position
    ("Jana", "Nováková", True, "1988-04-12", "Vinohradská 12, 120 00 Praha 2", "Praze",
     "Vltava Software s.r.o.", "Karlínské nám. 5, 186 00 Praha 8", "softwarová analytička"),
    ("Petr", "Dvořák", False, "1979-11-03", "Masarykova 45, 602 00 Brno", "Brně",
     "Moravská strojírna a.s.", "Hněvkovského 30, 617 00 Brno", "vedoucí výroby"),
    ("Lucie", "Černá", True, "1995-02-27", "Nádražní 8, 702 00 Ostrava", "Ostravě",
     "Ostravská logistika s.r.o.", "Místecká 120, 703 00 Ostrava", "dispečerka"),
    ("Tomáš", "Procházka", False, "1983-07-19", "Husova 17, 301 00 Plzeň", "Plzni",
     "Plzeňské pivovary Trend a.s.", "U Prazdroje 7, 301 00 Plzeň", "technolog"),
    ("Eva", "Horáková", True, "1990-09-05", "Lidická 3, 370 01 České Budějovice", "Českých Budějovicích",
     "Jihočeská energie s.r.o.", "Rudolfovská 66, 370 01 České Budějovice", "účetní"),
    ("Martin", "Kučera", False, "1986-01-30", "Palackého 22, 779 00 Olomouc", "Olomouci",
     "Hanácké stavby a.s.", "Tovární 41, 779 00 Olomouc", "stavbyvedoucí"),
    ("Kateřina", "Veselá", True, "1992-12-14", "Třída Míru 9, 530 02 Pardubice", "Pardubicích",
     "Východočeská nemocniční s.r.o.", "Kyjevská 44, 532 03 Pardubice", "zdravotní sestra"),
    ("Pavel", "Novotný", False, "1975-05-08", "Gočárova 14, 500 02 Hradec Králové", "Hradci Králové",
     "Orlické mlýny a.s.", "Pražská 3, 500 04 Hradec Králové", "obchodní zástupce"),
    ("Lenka", "Pokorná", True, "1984-03-21", "Jiráskova 6, 460 01 Liberec", "Liberci",
     "Ještěd Textil s.r.o.", "Tovární 12, 460 06 Liberec", "designérka"),
    ("Jiří", "Marek", False, "1981-08-16", "Komenského 31, 586 01 Jihlava", "Jihlavě",
     "Vysočina Data s.r.o.", "Znojemská 78, 586 01 Jihlava", "správce sítě"),
    ("Alena", "Krejčí", True, "1987-10-02", "Sokolovská 50, 360 01 Karlovy Vary", "Karlových Varech",
     "Lázně Vřídlo s.r.o.", "Vřídelní 1, 360 01 Karlovy Vary", "recepční"),
    ("Ondřej", "Fiala", False, "1993-06-25", "Školní 2, 400 01 Ústí nad Labem", "Ústí nad Labem",
     "Labská chemie a.s.", "Revoluční 9, 400 01 Ústí nad Labem", "chemik"),
]
OFFICES = {"Praha": ("PRO HLAVNÍ MĚSTO PRAHU", "pro Prahu 2"), "Brno": ("PRO JIHOMORAVSKÝ KRAJ", "Brno I"),
           "Ostrava": ("PRO MORAVSKOSLEZSKÝ KRAJ", "Ostrava I"), "Plzeň": ("PRO PLZEŇSKÝ KRAJ", "Plzeň-město"),
           "České Budějovice": ("PRO JIHOČESKÝ KRAJ", "České Budějovice"),
           "Olomouc": ("PRO OLOMOUCKÝ KRAJ", "Olomouc"), "Pardubice": ("PRO PARDUBICKÝ KRAJ", "Pardubice"),
           "Hradec Králové": ("PRO KRÁLOVÉHRADECKÝ KRAJ", "Hradec Králové"),
           "Liberec": ("PRO LIBERECKÝ KRAJ", "Liberec"), "Jihlava": ("PRO KRAJ VYSOČINA", "Jihlava"),
           "Karlovy Vary": ("PRO KARLOVARSKÝ KRAJ", "Karlovy Vary"),
           "Ústí nad Labem": ("PRO ÚSTECKÝ KRAJ", "Ústí nad Labem")}
CLEAR = "Daňový subjekt nemá u správce daně evidovány žádné daňové nedoplatky."
ARREARS = "Daňový subjekt má u správce daně evidován daňový nedoplatek ve výši 18 450 Kč."


def person(i, rnd, contract_date, income):
    fn, ln, female, birth, addr, city_loc, employer, emp_addr, position = PEOPLE[i]
    rc = make_rc(birth, female, 100 + 37 * i)
    city = next(k for k in OFFICES if k in addr.split(", ")[1])
    cd = date.fromisoformat(contract_date)
    net = income
    to = (cd.replace(day=1) - timedelta(days=1))
    frm = to.replace(day=1)
    month_en = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December"][to.month - 1]
    month_cs = ["leden", "únor", "březen", "duben", "květen", "červen", "červenec", "srpen", "září",
                "říjen", "listopad", "prosinec"][to.month - 1]
    iban = f"CZ{rnd.randint(10, 99)} 0800 0000 {rnd.randint(1000, 9999)} {rnd.randint(1000, 9999)} {rnd.randint(1000, 9999)}"
    p = {
        "first_name": fn, "last_name": ln, "female": female, "birth_date": birth, "rc": rc, "id_rc": rc,
        "address": addr, "city": city_loc, "ico": f"{rnd.randint(10000000, 99999999)}",
        "employer_address": emp_addr, "doc_number": f"{rnd.randint(100000000, 999999999)}",
        "id_expiry": f"{2029 + i % 6}-{(i % 12) + 1:02d}-{10 + i % 15:02d}",
        "stmt": {"bank": ["Fiktivní banka a.s.", "Česká spořitelní (fiktivní) a.s.", "Moravská banka (fiktivní)"][i % 3],
                 "iban": iban, "from": frm.isoformat(), "to": to.isoformat(), "salary": net,
                 "balance": round(rnd.uniform(15000, 180000), 2),
                 "tx": [(frm.replace(day=5).isoformat(), f"MZDA {employer[:22]}", f"+{money_cs(net)}"),
                        (frm.replace(day=9).isoformat(), "Albert supermarket", "-1 245,00 Kč"),
                        (frm.replace(day=12).isoformat(), "ČEZ záloha elektřina", "-1 890,00 Kč"),
                        (frm.replace(day=18).isoformat(), "Nájem byt", "-14 500,00 Kč"),
                        (frm.replace(day=25).isoformat(), "Platba kartou", "-2 310,00 Kč")]},
        "pay": {"employer": employer, "last_name": ln, "net": net, "gross": round(net * 1.32, 0),
                "period_en": f"{month_en} {to.year}", "period_cs": f"{month_cs} {to.year}"},
        "util": {"issuer": ["Pražská energetika a.s.", "E.ON Energie (fiktivní) a.s.", "innogy (fiktivní) s.r.o."][i % 3],
                 "date": (cd - timedelta(days=25 + i)).isoformat(), "amount": 1890.0 + 10 * i},
        "emp": {"employee_name": f"{fn} {ln}", "employer": employer, "position": position,
                "start": f"{2015 + i % 8}-{(i % 9) + 1:02d}-01", "net": net,
                "contract_type": "na dobu neurčitou", "issued": (cd - timedelta(days=6 + i % 9)).isoformat()},
        "tax": {"office_head": OFFICES[city][0], "branch": OFFICES[city][1],
                "number": f"{rnd.randint(1000000, 9999999)}/26/{2000 + rnd.randint(100, 999)}-{rnd.randint(10000, 99999)}-{rnd.randint(100000, 999999)}",
                "issued": (cd - timedelta(days=4 + i % 10)).isoformat(), "holder": f"{fn} {ln}", "rc": rc,
                "status": CLEAR},
    }
    return p


def truth(dt, p, statement_pages=(1, 2)):
    k = p["case"]
    if dt == "contract":
        return {"contract_number": k["contract_number"], "first_name": p["first_name"], "last_name": p["last_name"],
                "birth_date": p["birth_date"], "birth_number": p["rc"], "address": p["address"],
                "loan_amount": k["loan_amount"], "declared_monthly_income": k["declared_income"],
                "contract_date": k["contract_date"], "required_documents": sorted(k["required"])}
    if dt == "id_card":
        return {"first_name": p["first_name"], "last_name": p["last_name"], "birth_date": p["birth_date"],
                "birth_number": p["id_rc"], "address": p["address"], "document_number": p["doc_number"],
                "expiry_date": p["id_expiry"]}
    if dt == "bank_statement":
        s = p["stmt"]
        return {"account_holder": f"{p['first_name']} {p['last_name']}", "iban": s["iban"], "period_from": s["from"],
                "period_to": s["to"], "salary_credit": s["salary"],
                "closing_balance": s["balance"] if 2 in statement_pages else None}
    if dt == "payslip":
        w = p["pay"]
        return {"first_name": p["first_name"], "last_name": w["last_name"], "birth_number": p["rc"],
                "employer": w["employer"], "net_income": w["net"], "period": w["period_en"] if p["layouts"]["payslip"] == "A" else w["period_cs"]}
    if dt == "proof_of_address":
        u = p["util"]
        return {"full_name": f"{p['first_name']} {p['last_name']}", "address": p["address"], "issuer": u["issuer"],
                "issue_date": u["date"]}
    if dt == "employer_confirmation":
        e = p["emp"]
        return {"employee_name": e["employee_name"], "birth_number": p["rc"], "employer": e["employer"],
                "employer_ico": p["ico"], "position": e["position"], "employment_start": e["start"],
                "contract_type": e["contract_type"], "monthly_net_income": e["net"], "issue_date": e["issued"]}
    if dt == "tax_or_debt_certificate":
        t = p["tax"]
        return {"holder_name": t["holder"], "birth_number": t["rc"], "tax_office": f"Finanční úřad {t['office_head']}",
                "certificate_number": t["number"], "issue_date": t["issued"],
                "has_tax_arrears": "yes" if t["status"] == ARREARS else "no"}
    return {}


# ---------------- degradations ----------------
def _render(pdf: Path, dpi: int):
    out = []
    for pg in fitz.open(pdf):
        pix = pg.get_pixmap(dpi=dpi)
        out.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    return out


def _scan(img, rnd, rotate180=False):
    img = img.convert("L").rotate(rnd.uniform(-2.5, 2.5), resample=Image.BICUBIC, expand=True, fillcolor=235)
    arr = np.asarray(img).astype(np.float32)
    nrng = np.random.default_rng(rnd.randint(0, 10 ** 6))
    arr = arr * 0.92 + 12 + nrng.normal(0, 12, arr.shape)
    arr[nrng.random(arr.shape) < 0.0015] = 30
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.5))
    if rotate180:
        img = img.rotate(180, expand=True)
    b = io.BytesIO(); img.save(b, "JPEG", quality=45)
    return Image.open(io.BytesIO(b.getvalue())).convert("RGB")


def _perspective_coeffs(dst, src):
    A, B = [], []
    for (x, y), (u, v) in zip(dst, src):
        A += [[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]]
        B += [u, v]
    return np.linalg.solve(np.array(A, float), np.array(B, float)).tolist()


def _photo(pdf: Path, out: Path, crop=(0.05, 0.02, 0.95, 0.58)):
    page = _render(pdf, 150)[0]
    w, h = page.size
    card = page.crop((int(w * crop[0]), int(h * crop[1]), int(w * crop[2]), int(h * crop[3])))
    cw, ch = card.size
    bg = Image.new("RGB", (int(cw * 1.3), int(ch * 1.5)), (120, 92, 64))
    arr = np.asarray(bg).astype(np.float32)
    arr = np.clip(arr + (np.sin(np.arange(bg.size[0]) / 9.0) * 12)[None, :, None]
                  + np.random.default_rng(7).normal(0, 6, arr.shape), 0, 255).astype(np.uint8)
    bg = Image.fromarray(arr)
    tilted = card.rotate(-6, resample=Image.BICUBIC, expand=True, fillcolor=(0, 0, 0))
    mask = card.convert("L").point(lambda _: 255).rotate(-6, resample=Image.BICUBIC, expand=True)
    bg.paste(tilted, (int(cw * 0.1), int(ch * 0.2)), mask)
    W, H = bg.size
    bg = bg.transform((W, H), Image.PERSPECTIVE,
                      _perspective_coeffs([(0, 0), (W, 0), (W, H), (0, H)],
                                          [(W * 0.04, H * 0.02), (W * 0.97, 0), (W, H), (0, H * 0.98)]),
                      Image.BICUBIC).filter(ImageFilter.GaussianBlur(0.7))
    bg.save(out, "JPEG", quality=72)


# ---------------- package builder ----------------
CASES = [
    # name, person, contract layout, required types (besides contract), overrides, expectations
    dict(name="01_happy_all7_A", person=0, layout="A", required=TYPES7[1:], next="approve_ready", flags=[]),
    dict(name="02_happy_all7_B", person=1, layout="B", required=TYPES7[1:], next="approve_ready", flags=[]),
    dict(name="03_missing_tax_certificate", person=2, layout="mix", required=TYPES7[1:],
         omit=["tax_or_debt_certificate"], next="request_missing_documents", flags=[],
         missing=["tax_or_debt_certificate"]),
    dict(name="04_missing_statement_page", person=3, layout="B",
         required=["id_card", "bank_statement", "payslip", "employer_confirmation", "tax_or_debt_certificate"],
         statement_pages=(1,), next="request_missing_documents", flags=["pages_complete"]),
    dict(name="05_income_mismatch_employer", person=4, layout="A", required=TYPES7[1:],
         emp_net=41000, next="manual_review_discrepancy",
         flags=["income_contract_vs_employer", "income_payslip_vs_employer"]),
    dict(name="06_employer_mismatch_stale_cert", person=5, layout="mix", required=TYPES7[1:],
         emp_employer="Brněnská logistika s.r.o.", tax_age=75, next="manual_review_discrepancy",
         flags=["employer_consistent", "tax_certificate_recent"]),
    dict(name="07_invalid_rc_name_typo", person=6, layout="B",
         required=["id_card", "bank_statement", "payslip", "proof_of_address", "employer_confirmation"],
         id_rc_bad=True, pay_last_name="Veselova", next="manual_review_discrepancy",
         flags=["birth_number_valid", "birth_number_consistent", "last_name_consistent"]),
    dict(name="08_scanned_noisy_5types", person=7, layout="A",
         required=["id_card", "bank_statement", "payslip", "employer_confirmation"],
         scanned=True, next="approve_ready", flags=[]),
    dict(name="09_shuffled_6types", person=8, layout="mix",
         required=["id_card", "bank_statement", "payslip", "employer_confirmation", "tax_or_debt_certificate"],
         shuffle=True, next="approve_ready", flags=[]),
    dict(name="10_photo_id_tax_arrears", person=9, layout="B", required=TYPES7[1:],
         photo_id=True, tax_status=ARREARS, next="manual_review_discrepancy", flags=["no_tax_arrears"]),
    dict(name="11_extra_doc_cert_other_person", person=10, layout="A",
         required=["id_card", "bank_statement", "payslip", "employer_confirmation", "tax_or_debt_certificate"],
         extra_other=True, tax_holder=("Jan Krejčí", "1985-02-11", False), next="manual_review_discrepancy",
         flags=["certificate_holder_matches_id", "birth_number_consistent", "no_unrequested_documents"],
         unexpected=["other"]),
    dict(name="12_happy_5types_B", person=11, layout="B",
         required=["id_card", "payslip", "proof_of_address", "tax_or_debt_certificate"],
         next="approve_ready", flags=[]),
]
LAYOUT_MIX = {"contract": "A", "id_card": "B", "bank_statement": "A", "payslip": "B",
              "proof_of_address": "A", "employer_confirmation": "C", "tax_or_debt_certificate": "C"}


def layouts_for(case, idx):
    if case["layout"] == "A":
        base = {t: "A" for t in TYPES7} | {"employer_confirmation": "B", "tax_or_debt_certificate": "B"}
    elif case["layout"] == "B":
        base = {t: "B" for t in TYPES7} | ({"employer_confirmation": "C"} if idx % 2 else {})
    else:
        base = dict(LAYOUT_MIX)
    return base


def build(case, idx, rnd):
    d = ROOT / case["name"]
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    contract_date = (date(2026, 10, 1) + timedelta(days=idx % 10)).isoformat()
    income = [52000, 61500, 38900, 47200, 56000, 68000, 42300, 73500, 49800, 58700, 44100, 39600][idx]
    p = person(case["person"], rnd, contract_date, income)
    p["layouts"] = layouts_for(case, idx)
    p["case"] = {"contract_number": f"KB-2026-{10421 + idx * 37:05d}", "loan_amount": [250000, 480000, 150000,
                 320000, 600000, 900000, 200000, 1200000, 350000, 750000, 280000, 180000][idx],
                 "declared_income": income, "contract_date": contract_date, "required": list(case["required"])}
    # ---- seeded defects (truth follows what is printed) ----
    if case.get("emp_net"):
        p["emp"]["net"] = case["emp_net"]
    if case.get("emp_employer"):
        p["emp"]["employer"] = case["emp_employer"]
    if case.get("tax_age"):
        p["tax"]["issued"] = (date.fromisoformat(contract_date) - timedelta(days=case["tax_age"])).isoformat()
    if case.get("id_rc_bad"):
        p["id_rc"] = p["rc"][:-1] + str((int(p["rc"][-1]) + 3) % 10)   # breaks mod 11
    if case.get("pay_last_name"):
        p["pay"]["last_name"] = case["pay_last_name"]
    if case.get("tax_status"):
        p["tax"]["status"] = case["tax_status"]
    if case.get("tax_holder"):
        hn, hb, hf = case["tax_holder"]
        p["tax"]["holder"], p["tax"]["rc"] = hn, make_rc(hb, hf, 211)

    present = ["contract"] + [t for t in case["required"] if t not in case.get("omit", [])]
    if case.get("extra_other"):
        present.append("other")
    st_pages = case.get("statement_pages", (1, 2))
    # one PDF per document, then the package
    parts = []                                    # (type, path, n_pages)
    for t in present:
        path = d / f"{t}.pdf"
        c = new_canvas(path)
        if t == "bank_statement":
            RENDER[t](c, p, p["layouts"][t], pages=st_pages, total=2)
        else:
            RENDER[t](c, p, p["layouts"].get(t, "A"))
        c.save()
        parts.append((t, path, len(PdfReader(str(path)).pages)))

    inputs, expected_docs, page_types = [], [], []
    if case.get("shuffle"):
        flat = [(t, path, i) for t, path, n in parts for i in range(n)]
        order = list(range(len(flat)))
        rnd2 = random.Random(SEED + idx)
        while True:
            rnd2.shuffle(order)
            st = [k for k, (t, _, _) in enumerate(flat) if t == "bank_statement"]
            # make sure page 2 of the statement comes before page 1 (the hard case)
            if order.index(st[-1]) < order.index(st[0]) and order[0] != 0:
                break
        w = PdfWriter()
        readers = {}
        pos = {}
        for phys, k in enumerate(order, start=1):
            t, path, i = flat[k]
            r = readers.setdefault(path, PdfReader(str(path)))
            w.add_page(r.pages[i])
            page_types.append(t)
            pos.setdefault(t, []).append((i, phys))
        with open(d / "package.pdf", "wb") as f:
            w.write(f)
        inputs = ["package.pdf"]
        for t, _, _ in parts:
            pages = [phys for _, phys in sorted(pos[t])]
            expected_docs.append({"doc_type": t, "pages": pages, "source_file": "package.pdf"})
    elif case.get("photo_id"):
        w = PdfWriter(); n = 0
        for t, path, cnt in parts:
            if t == "id_card":
                continue
            r = PdfReader(str(path))
            for i in range(cnt):
                w.add_page(r.pages[i]); n += 1
                page_types.append(t)
            expected_docs.append({"doc_type": t, "pages": list(range(n - cnt + 1, n + 1)), "source_file": "package.pdf"})
        with open(d / "package.pdf", "wb") as f:
            w.write(f)
        _photo(d / "id_card.pdf", d / "id_photo.jpg")
        inputs = ["package.pdf", "id_photo.jpg"]
        page_types.append("id_card")
        expected_docs.append({"doc_type": "id_card", "pages": [1], "source_file": "id_photo.jpg"})
    else:
        w = PdfWriter(); n = 0
        for t, path, cnt in parts:
            r = PdfReader(str(path))
            for i in range(cnt):
                w.add_page(r.pages[i]); n += 1
                page_types.append(t)
            expected_docs.append({"doc_type": t, "pages": list(range(n - cnt + 1, n + 1)), "source_file": "package.pdf"})
        with open(d / "package.pdf", "wb") as f:
            w.write(f)
        inputs = ["package.pdf"]
        if case.get("scanned"):
            imgs = [_scan(im, rnd, rotate180=(i == 2)) for i, im in enumerate(_render(d / "package.pdf", 110))]
            (d / "package.pdf").unlink()
            imgs[0].save(d / "package_scan.pdf", "PDF", save_all=True, append_images=imgs[1:], resolution=110)
            inputs = ["package_scan.pdf"]
            for e in expected_docs:
                e["source_file"] = "package_scan.pdf"

    fields = {t: truth(t, p, st_pages) for t in present if t != "other"}
    missing = case.get("missing", [])
    flags = list(case["flags"])
    gt = {
        "package_id": case["name"],
        "description": {k: v for k, v in case.items() if k not in ("name",)},
        "person": {"first_name": p["first_name"], "last_name": p["last_name"], "birth_number": p["rc"]},
        "layouts": p["layouts"],
        "inputs": inputs,
        "required_documents": ["contract"] + list(case["required"]),
        "expected_docs": present,
        "expected_page_types": page_types,
        "expected_documents": expected_docs,
        "expected_fields": fields,
        "expected_missing": missing,
        "expected_missing_pages": ([{"doc_type": "bank_statement", "page_count": 2, "missing_page_numbers": [2]}]
                                   if st_pages == (1,) else []),
        "expected_unexpected": case.get("unexpected", []),
        "expected_flags": flags,
        "expected_next_step": case["next"],
    }
    (d / "ground_truth.json").write_text(json.dumps(gt, indent=2, ensure_ascii=False, default=str))
    print(f"{case['name']:<34} types={len(set(present) - {'other'})} pages={len(page_types)} inputs={inputs}")


def main():
    rnd = random.Random(SEED)
    for old in ROOT.iterdir():                 # regenerate ALL packages (old 01-09 removed)
        if old.is_dir() and old.name[:2].isdigit():
            shutil.rmtree(old)
    for i, case in enumerate(CASES):
        build(case, i, rnd)


if __name__ == "__main__":
    main()
