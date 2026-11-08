#!/usr/bin/env python3
"""Generate a reproducible synthetic invoice dataset with ground truth.

Every vendor, buyer, address and tax ID in this file is fictional.

Output (default: data/synthetic/):
    pdf/doc_NNNN.pdf            one PDF per invoice
    scans/doc_NNNN.png          ~30% of invoices re-rendered as "scanned" images
    ground_truth/doc_NNNN.json  exact values as printed on the document
    manifest.csv                one row per invoice
A curated subset is also copied to data/samples/ (committed to git).

Usage:
    python src/generate_invoices.py [--seed 42] [--n 120] [--out data/synthetic]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import arabic_reshaper
import numpy as np
import pypdfium2 as pdfium
from bidi.algorithm import get_display
from PIL import Image, ImageFilter
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "assets" / "fonts"
DEFAULT_OUT = ROOT / "data" / "synthetic"
DEFAULT_SAMPLES = ROOT / "data" / "samples"

DEFAULT_SEED = 42
DEFAULT_N = 120
SCAN_RATIO = 0.30
ANOMALY_RATIO = 0.10

TEMPLATES = ("T1_classic", "T2_modern", "T3_receipt", "T4_two_column", "T5_bilingual_ar_en")
ANOMALY_TYPES = (
    "total_mismatch",
    "missing_invoice_date",
    "duplicate_invoice_number",
    "tax_amount_mismatch",
)

CENT = Decimal("0.01")
W, H = A4


def q2(x) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------------------
# Fictional master data
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    name_ar: str | None
    tax_label: str
    tax_label_ar: str | None
    tax_id: str
    currency: str
    tax_rate: Decimal  # percent
    num_style: str  # "en" -> 1,234.56   "eu" -> 1.234,56
    templates: tuple[str, ...]
    catalog: str
    inv_fmt: str  # uses {year} and {seq}
    address: tuple[str, ...]
    color: str


VENDORS: tuple[Vendor, ...] = (
    Vendor("V01", "Bluefinch Office Supply Co.", None, "EIN", None, "47-2918365", "USD",
           Decimal("8.25"), "en", ("T1_classic", "T3_receipt"), "office", "BOS-{year}-{seq:05d}",
           ("1180 Larkspur Avenue, Suite 4", "Millbrook, OR 97099", "United States"), "#1f4e79"),
    Vendor("V02", "Copperline Freight LLC", None, "EIN", None, "83-5521047", "USD",
           Decimal("6"), "en", ("T2_modern", "T4_two_column"), "logistics", "CF{seq:06d}",
           ("77 Quarry Road, Dock 12", "Ashby Falls, TX 75999", "United States"), "#b85c1e"),
    Vendor("V03", "Harborlight Print Works", None, "EIN", None, "26-7730419", "USD",
           Decimal("7"), "en", ("T3_receipt", "T1_classic"), "print", "HPW-{seq:04d}",
           ("9 Lantern Wharf", "Port Calder, ME 04999", "United States"), "#2e6b5e"),
    Vendor("V04", "Gruenfeld Maschinentechnik GmbH", None, "USt-IdNr.", None, "DE298104473", "EUR",
           Decimal("19"), "eu", ("T4_two_column", "T1_classic"), "machinery", "RE-{year}-{seq:04d}",
           ("Industriering 42", "99999 Talheim am Wald", "Germany"), "#4a4a4a"),
    Vendor("V05", "Atelier Solenne SARL", None, "TVA", None, "FR32845219036", "EUR",
           Decimal("20"), "eu", ("T2_modern", "T3_receipt"), "design", "F{year}{seq:04d}",
           ("14 Rue des Glycines", "99100 Saint-Aubrac", "France"), "#7a3b69"),
    Vendor("V06", "Velatra Software B.V.", None, "BTW", None, "NL861204593B01", "EUR",
           Decimal("21"), "eu", ("T4_two_column", "T2_modern"), "software", "VS-INV-{seq:05d}",
           ("Keplerstraat 310", "9999 ZZ Noordveld", "Netherlands"), "#0b6e99"),
    Vendor("V07", "Qasr Al Nakheel Trading Est.", "مؤسسة قصر النخيل التجارية", "VAT No.", "الرقم الضريبي",
           "310245678900003", "SAR", Decimal("15"), "en", ("T5_bilingual_ar_en",), "office",
           "QN-{year}-{seq:04d}", ("Building 18, Al Wurud Street", "Riyadh 99999", "Saudi Arabia"), "#7b5e2a"),
    Vendor("V08", "Rimal Catering Co.", "شركة رمال للإعاشة", "VAT No.", "الرقم الضريبي",
           "311908452100003", "SAR", Decimal("15"), "en", ("T5_bilingual_ar_en", "T1_classic"), "catering",
           "RC{seq:05d}", ("Unit 5, Al Safa District", "Jeddah 99998", "Saudi Arabia"), "#a0522d"),
    Vendor("V09", "Wahat Digital Solutions", "حلول الواحة الرقمية", "VAT No.", "الرقم الضريبي",
           "302117735400003", "SAR", Decimal("15"), "en", ("T5_bilingual_ar_en", "T2_modern"), "it",
           "WDS/{year}/{seq:04d}", ("Office 704, Tower B, King Road", "Dammam 99997", "Saudi Arabia"), "#13715a"),
    Vendor("V10", "Lulua Marine Services LLC", "لؤلؤة للخدمات البحرية ذ.م.م", "TRN", "رقم التسجيل الضريبي",
           "100384920500003", "AED", Decimal("5"), "en", ("T5_bilingual_ar_en", "T4_two_column"), "marine",
           "LMS-{seq:05d}", ("Warehouse 3, Marina Yard Road", "Abu Dhabi", "United Arab Emirates"), "#14508c"),
    Vendor("V11", "Saqr Horizon Facilities", "صقر الأفق لإدارة المرافق", "TRN", "رقم التسجيل الضريبي",
           "100572168300003", "AED", Decimal("5"), "en", ("T5_bilingual_ar_en", "T3_receipt"), "facilities",
           "SHF{year}-{seq:04d}", ("Plot 221, Industrial Area 9", "Sharjah", "United Arab Emirates"), "#5b6b2f"),
    Vendor("V12", "Dunecrest Interiors FZ-LLC", None, "TRN", None, "100649027700003", "AED",
           Decimal("5"), "en", ("T2_modern", "T4_two_column"), "interiors", "DCI-{seq:04d}",
           ("Studio 12, Design Quarter", "Dubai", "United Arab Emirates"), "#8c3b3b"),
    Vendor("V13", "Nile Lotus Printing House", "مطبعة لوتس النيل", "Tax Reg. No.", "رقم التسجيل الضريبي",
           "512-904-337", "EGP", Decimal("14"), "en", ("T5_bilingual_ar_en", "T1_classic"), "print",
           "NLP-{seq:05d}", ("23 El Nakheel Street, Dokki", "Giza", "Egypt"), "#3a5f8a"),
    Vendor("V14", "Delta Papyrus Supplies", None, "Tax Reg. No.", None, "498-226-150", "EGP",
           Decimal("14"), "en", ("T3_receipt", "T4_two_column"), "office", "DPS/{seq:04d}/{year}",
           ("6 Port Said Road", "Alexandria", "Egypt"), "#2d6a4f"),
    Vendor("V15", "Kemet Cables & Wiring", "كيميت للكابلات والأسلاك", "Tax Reg. No.", "رقم التسجيل الضريبي",
           "603-781-492", "EGP", Decimal("14"), "en", ("T5_bilingual_ar_en", "T2_modern"), "cables",
           "KCW-{year}-{seq:03d}", ("Industrial Zone 4, Block 17", "10th of Ramadan City", "Egypt"), "#9a6324"),
)

# One fictional buyer group with a legal entity per currency region.
BUYERS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "USD": ("Orchard Peak Holdings Inc.", "أورتشارد بيك القابضة",
            ("400 Juniper Street, Floor 9", "Riverton, CA 90099", "United States")),
    "EUR": ("Orchard Peak Europe B.V.", "أورتشارد بيك أوروبا",
            ("Lindenlaan 5", "9998 AA Westerhaven", "Netherlands")),
    "SAR": ("Orchard Peak Arabia Co.", "شركة أورتشارد بيك العربية",
            ("Floor 3, Al Yasmin Business Park", "Riyadh 99990", "Saudi Arabia")),
    "AED": ("Orchard Peak Middle East LLC", "أورتشارد بيك الشرق الأوسط ذ.م.م",
            ("Office 1902, Creekview Tower", "Dubai", "United Arab Emirates")),
    "EGP": ("Orchard Peak Egypt S.A.E.", "أورتشارد بيك مصر ش.م.م",
            ("90 Corniche El Nil, 7th Floor", "Cairo", "Egypt")),
}

FX = {"USD": Decimal("1"), "EUR": Decimal("0.92"), "SAR": Decimal("3.75"),
      "AED": Decimal("3.67"), "EGP": Decimal("48.5")}
CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "SAR": "SAR ", "AED": "AED ", "EGP": "EGP "}

# (description_en, description_ar, usd_price_lo, usd_price_hi, qty_min, qty_max, fractional_qty)
Item = tuple[str, str, float, float, int, int, bool]
CATALOGS: dict[str, list[Item]] = {
    "office": [
        ("Copy paper A4 (box of 5 reams)", "ورق تصوير A4 (كرتون 5 رزم)", 18, 32, 1, 40, False),
        ("Toner cartridge, black", "حبر طابعة أسود", 45, 120, 1, 8, False),
        ("Ballpoint pens (box of 50)", "أقلام حبر جاف (علبة 50)", 6, 14, 1, 20, False),
        ("Spiral notebooks A5 (pack of 10)", "دفاتر سلك A5 (10 حبات)", 8, 20, 1, 25, False),
        ("Desk organizer", "منظم مكتب", 9, 25, 1, 15, False),
        ("Heavy-duty stapler", "دباسة كبيرة", 12, 30, 1, 6, False),
        ("Whiteboard markers (set of 12)", "أقلام سبورة (12 قلم)", 7, 15, 1, 20, False),
        ("Archive box files", "ملفات أرشيف", 3, 8, 5, 60, False),
        ("Sticky notes (pack of 24)", "أوراق لاصقة (24 رزمة)", 10, 22, 1, 12, False),
    ],
    "logistics": [
        ("Linehaul freight per pallet", "شحن بري لكل منصة", 80, 220, 1, 24, False),
        ("Customs clearance handling", "تخليص جمركي", 60, 180, 1, 3, False),
        ("Warehouse storage per pallet-day", "تخزين لكل منصة يوميا", 1, 4, 10, 300, False),
        ("Last-mile delivery", "توصيل للميل الأخير", 12, 40, 1, 40, False),
        ("Fuel surcharge", "رسوم وقود إضافية", 15, 60, 1, 4, False),
        ("Shrink wrapping per pallet", "تغليف لكل منصة", 3, 9, 1, 24, False),
    ],
    "print": [
        ("Business cards (box of 250)", "بطاقات أعمال (250 بطاقة)", 25, 60, 1, 12, False),
        ("Tri-fold brochures (100 pcs)", "بروشورات ثلاثية (100 قطعة)", 40, 110, 1, 20, False),
        ("Vinyl banner 2x1 m", "لافتة فينيل 2×1 م", 35, 90, 1, 6, False),
        ("Letterhead paper (500 sheets)", "ورق مراسلات (500 ورقة)", 30, 75, 1, 10, False),
        ("Roll-up stand", "ستاند رول أب", 45, 120, 1, 5, False),
        ("Sticker labels (roll)", "ملصقات (لفة)", 10, 30, 1, 30, False),
        ("Design proof revision (h)", "مراجعة تصميم (ساعة)", 15, 40, 1, 6, True),
    ],
    "machinery": [
        ("Bearing set 6204-2RS", "طقم رولمان 6204", 8, 25, 2, 40, False),
        ("Hydraulic hose assembly", "خرطوم هيدروليك", 30, 140, 1, 10, False),
        ("Preventive maintenance labour (h)", "عمالة صيانة وقائية (ساعة)", 55, 95, 1, 16, True),
        ("Drive belt", "سير نقل حركة", 12, 45, 1, 12, False),
        ("Calibration service", "خدمة معايرة", 150, 400, 1, 2, False),
        ("Gearbox seal kit", "طقم موانع تسرب", 20, 70, 1, 8, False),
    ],
    "design": [
        ("Brand identity workshop (h)", "ورشة هوية بصرية (ساعة)", 90, 160, 2, 12, True),
        ("Logo concept round", "جولة تصميم شعار", 300, 900, 1, 2, False),
        ("Packaging mockups", "نماذج تغليف", 120, 350, 1, 4, False),
        ("Product photography, half day", "تصوير منتجات نصف يوم", 400, 850, 1, 2, False),
        ("Illustration set", "مجموعة رسومات", 200, 600, 1, 3, False),
        ("Revision hours (h)", "ساعات تعديل", 70, 120, 1, 10, True),
    ],
    "software": [
        ("Annual licence per seat", "ترخيص سنوي لكل مستخدم", 120, 480, 1, 50, False),
        ("Premium support (h)", "دعم فني متميز (ساعة)", 80, 150, 1, 20, True),
        ("Cloud hosting, monthly", "استضافة سحابية شهرية", 90, 600, 1, 3, False),
        ("Onboarding and training", "تأهيل وتدريب", 300, 1200, 1, 2, False),
        ("API usage block (1M calls)", "باقة استخدام API", 50, 250, 1, 10, False),
        ("Data migration service", "خدمة ترحيل بيانات", 400, 1500, 1, 1, False),
    ],
    "catering": [
        ("Lunch boxes", "وجبات غداء", 6, 14, 10, 200, False),
        ("Coffee break service per person", "استراحة قهوة للشخص", 3, 8, 10, 150, False),
        ("Event buffet per guest", "بوفيه مناسبات للضيف", 15, 40, 20, 120, False),
        ("Bottled water (carton)", "مياه معبأة (كرتون)", 2, 5, 5, 80, False),
        ("Fresh juice (litre)", "عصير طازج (لتر)", 3, 7, 5, 40, True),
        ("Dessert platter", "طبق حلويات", 20, 55, 1, 10, False),
    ],
    "it": [
        ("Laptop 14in, 16GB RAM", "حاسب محمول 14 بوصة", 850, 1600, 1, 10, False),
        ("27in monitor", "شاشة 27 بوصة", 180, 380, 1, 12, False),
        ("Network switch, 24-port", "محول شبكة 24 منفذ", 220, 650, 1, 4, False),
        ("Installation and setup (h)", "تركيب وإعداد (ساعة)", 40, 90, 1, 24, True),
        ("Wireless keyboard and mouse", "لوحة مفاتيح وفأرة لاسلكية", 25, 70, 1, 20, False),
        ("UPS 1500VA", "مزود طاقة احتياطي 1500VA", 160, 320, 1, 6, False),
        ("Cat6 patch cables (10 pcs)", "كابلات شبكة Cat6 (10 قطع)", 15, 35, 1, 15, False),
    ],
    "marine": [
        ("Hull cleaning", "تنظيف بدن القارب", 250, 700, 1, 3, False),
        ("Engine service", "صيانة المحرك", 400, 1200, 1, 2, False),
        ("Marine diesel (litre)", "ديزل بحري (لتر)", 0.8, 1.5, 100, 2000, False),
        ("Mooring fee per day", "رسوم رسو يومية", 30, 90, 1, 30, False),
        ("Safety equipment inspection", "فحص معدات السلامة", 120, 300, 1, 2, False),
        ("Antifouling paint (litre)", "طلاء مانع للحشف (لتر)", 25, 60, 2, 20, True),
    ],
    "facilities": [
        ("Office cleaning, monthly", "تنظيف المكاتب شهريا", 800, 2500, 1, 3, False),
        ("Pest control visit", "زيارة مكافحة حشرات", 60, 180, 1, 4, False),
        ("AC maintenance per unit", "صيانة مكيف لكل وحدة", 25, 70, 1, 30, False),
        ("Plumbing repair (h)", "إصلاح سباكة (ساعة)", 30, 70, 1, 12, True),
        ("Security guard shift", "وردية حراسة أمن", 60, 140, 1, 60, False),
        ("Waste collection", "جمع النفايات", 80, 250, 1, 4, False),
    ],
    "interiors": [
        ("Ergonomic office chair", "كرسي مكتب مريح", 180, 450, 1, 30, False),
        ("Height-adjustable desk", "مكتب قابل لتعديل الارتفاع", 350, 900, 1, 20, False),
        ("Glass partition panel", "لوح فاصل زجاجي", 220, 600, 1, 12, False),
        ("Carpet tiles (sqm)", "بلاط سجاد (متر مربع)", 18, 45, 10, 200, True),
        ("Meeting table, 8-seat", "طاولة اجتماعات 8 أشخاص", 900, 2400, 1, 2, False),
        ("Installation labour (h)", "عمالة تركيب (ساعة)", 30, 60, 2, 40, True),
    ],
    "cables": [
        ("Copper cable 3x2.5mm (100 m roll)", "كابل نحاس 3×2.5 مم (لفة 100 م)", 60, 150, 1, 30, False),
        ("Armoured cable 4x16mm (m)", "كابل مسلح 4×16 مم (متر)", 8, 20, 10, 300, True),
        ("Cable lugs (pack of 100)", "كوسات كابلات (100 قطعة)", 10, 30, 1, 20, False),
        ("PVC conduit 25mm (3 m)", "ماسورة PVC قطر 25 مم (3 م)", 1.5, 4, 10, 200, False),
        ("Junction box IP65", "علبة توصيل IP65", 5, 15, 2, 60, False),
        ("Circuit breaker 32A", "قاطع كهربائي 32 أمبير", 9, 28, 2, 40, False),
    ],
}
# Generic lines any vendor may add; also lets invoices reach 12 distinct lines.
MISC_ITEMS: list[Item] = [
    ("Delivery charge", "رسوم توصيل", 10, 40, 1, 1, False),
    ("Express handling fee", "رسوم معالجة عاجلة", 15, 50, 1, 1, False),
    ("Packaging and crating", "تغليف وتعبئة", 8, 35, 1, 3, False),
    ("Extended warranty (12 months)", "ضمان ممتد (12 شهر)", 30, 120, 1, 5, False),
    ("Site visit", "زيارة موقع", 40, 110, 1, 3, False),
    ("Administrative fee", "رسوم إدارية", 5, 25, 1, 1, False),
]

TERMS_DAYS = (0, 15, 30, 30, 45, 60)
DATE_START = date(2025, 1, 1)
DATE_END = date(2026, 9, 30)

# --------------------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------------------

AR_DIGITS = str.maketrans("0123456789,.%", "٠١٢٣٤٥٦٧٨٩٬٫٪")
DATE_FORMATS = {
    "iso": "%Y-%m-%d",
    "dmy_slash": "%d/%m/%Y",
    "mdy_slash": "%m/%d/%Y",
    "dmy_dot": "%d.%m.%Y",
    "d_mon_y": "%d %b %Y",
    "mon_d_y": "%B %d, %Y",
}


class Fmt:
    """Renders numbers/dates the way a given document prints them."""

    def __init__(self, num_style: str, digits: str, date_style: str):
        self.num_style, self.digits, self.date_style = num_style, digits, date_style

    def _finish(self, s: str) -> str:
        if self.num_style == "eu":
            s = s.replace(",", "\0").replace(".", ",").replace("\0", ".")
        if self.digits == "arabic_indic":
            s = s.translate(AR_DIGITS)
        return s

    def money(self, d: Decimal) -> str:
        return self._finish(f"{d:,.2f}")

    def qty(self, q: Decimal) -> str:
        return self._finish(f"{q:,.0f}" if q == q.to_integral_value() else f"{q:,.1f}")

    def pct(self, rate: Decimal) -> str:
        return self._finish(f"{rate.normalize():f}%")

    def date(self, d: date) -> str:
        s = d.strftime(DATE_FORMATS[self.date_style])
        return s.translate(AR_DIGITS) if self.digits == "arabic_indic" else s


def date_style_for(template: str, v: Vendor) -> str:
    return {
        "T1_classic": "iso",
        "T2_modern": "d_mon_y",
        "T3_receipt": "mdy_slash" if v.currency == "USD" else "dmy_slash",
        "T4_two_column": "dmy_dot" if v.currency == "EUR" else "mon_d_y",
        "T5_bilingual_ar_en": "dmy_slash",
    }[template]


def ar(text: str) -> str:
    """Shape + reorder Arabic text for left-to-right PDF drawing."""
    return get_display(arabic_reshaper.reshape(text))


def tax_name(v: Vendor) -> str:
    return "Sales Tax" if v.currency == "USD" else "VAT"


def terms_text(days: int) -> str:
    return "Due on receipt" if days == 0 else f"Net {days}"


# --------------------------------------------------------------------------------------
# Invoice data generation
# --------------------------------------------------------------------------------------


def random_price(rng: random.Random, lo: float, hi: float, currency: str) -> Decimal:
    p = Decimal(str(rng.uniform(lo, hi))) * FX[currency]
    if p >= 50 and rng.random() < 0.5:  # many larger prices are round numbers
        return q2(p.to_integral_value(rounding=ROUND_HALF_UP))
    return q2(p)


def random_qty(rng: random.Random, item: Item) -> Decimal:
    _, _, _, _, qmin, qmax, fractional = item
    if fractional and rng.random() < 0.6:
        return Decimal(rng.randint(qmin * 2, qmax * 2)) / 2
    # skew towards small quantities
    return Decimal(min(qmax, qmin + int(rng.expovariate(1 / max(1, (qmax - qmin) / 4)))))


def build_lines(rng: random.Random, v: Vendor) -> list[dict]:
    n = rng.choices(range(1, 13), weights=[8, 10, 10, 9, 8, 7, 6, 5, 4, 3, 2, 2])[0]
    primary = CATALOGS[v.catalog][:]
    misc = MISC_ITEMS[:]
    rng.shuffle(primary)
    rng.shuffle(misc)
    n_primary = min(n, len(primary))
    if n < len(primary) and n >= 3 and rng.random() < 0.3:
        n_primary = n - 1  # occasionally add one misc line
    chosen = primary[:n_primary] + misc[: n - n_primary]
    lines = []
    for item in chosen:
        qty = random_qty(rng, item)
        price = random_price(rng, item[2], item[3], v.currency)
        lines.append({
            "description": item[0],
            "description_ar": item[1],
            "quantity": qty,
            "unit_price": price,
            "line_total": q2(qty * price),
        })
    return lines


def compute_totals(doc: dict) -> None:
    doc["subtotal"] = sum((ln["line_total"] for ln in doc["lines"]), Decimal("0.00"))
    if doc["discount_pct"] is not None:
        doc["discount"] = q2(doc["subtotal"] * doc["discount_pct"] / 100)
    base = doc["subtotal"] - (doc["discount"] or 0)
    doc["tax_amount"] = q2(base * doc["tax_rate"] / 100)
    doc["total"] = base + doc["tax_amount"]


def build_dataset(seed: int, n: int, scan_ratio: float, anomaly_ratio: float) -> list[dict]:
    rng = random.Random(seed)

    # Balanced templates, then a vendor that uses that template.
    templates = [TEMPLATES[i % len(TEMPLATES)] for i in range(n)]
    rng.shuffle(templates)
    span = (DATE_END - DATE_START).days

    docs = []
    for tpl in templates:
        v = rng.choice([x for x in VENDORS if tpl in x.templates])
        inv_date = DATE_START + timedelta(days=rng.randint(0, span))
        terms = rng.choice(TERMS_DAYS)
        doc = {
            "vendor": v,
            "template": tpl,
            "digits": "western",
            "invoice_date": inv_date,
            "printed_invoice_date": inv_date,
            "due_date": inv_date + timedelta(days=terms),
            "terms_days": terms,
            "lines": build_lines(rng, v),
            "tax_rate": v.tax_rate,
            "discount": None,
            "discount_pct": None,
            "anomaly": None,
            "has_scan": False,
        }
        roll = rng.random()
        if roll < 0.15:
            doc["discount_pct"] = rng.choice([Decimal("2.5"), Decimal("5"), Decimal("10"), Decimal("15")])
        elif roll < 0.25:
            doc["discount"] = Decimal("0")  # flat discount, set after subtotal is known
        compute_totals(doc)
        if doc["discount"] == 0 and doc["discount_pct"] is None:
            flat = doc["subtotal"] * Decimal(str(rng.uniform(0.02, 0.08)))
            step = Decimal("5") if doc["subtotal"] > 200 else Decimal("1")
            doc["discount"] = q2(max(step, (flat / step).to_integral_value() * step))
            compute_totals(doc)
        docs.append(doc)

    # Chronological doc ids and per-vendor invoice sequences.
    docs.sort(key=lambda d: (d["invoice_date"], d["vendor"].id))
    # Strictly increasing per vendor, with small gaps (invoices issued to other customers).
    seq = {v.id: rng.randint(10, 900) for v in VENDORS}
    for i, doc in enumerate(docs, start=1):
        v = doc["vendor"]
        seq[v.id] += rng.randint(1, 3)
        doc["doc_id"] = f"doc_{i:04d}"
        doc["invoice_number"] = v.inv_fmt.format(year=doc["invoice_date"].year, seq=seq[v.id])
        doc["buyer"] = BUYERS[v.currency]

    # Arabic-Indic digits on ~half of the bilingual invoices.
    t5 = [d for d in docs if d["template"] == "T5_bilingual_ar_en"]
    for d in rng.sample(t5, len(t5) // 2):
        d["digits"] = "arabic_indic"

    inject_anomalies(rng, docs, round(n * anomaly_ratio))

    for d in rng.sample(docs, round(n * scan_ratio)):
        d["has_scan"] = True
    return docs


def inject_anomalies(rng: random.Random, docs: list[dict], count: int) -> None:
    if count == 0:
        return
    per_type = {t: count // 4 + (1 if i < count % 4 else 0) for i, t in enumerate(ANOMALY_TYPES)}

    # duplicate_invoice_number: needs an earlier invoice from the same vendor.
    seen: dict[str, dict] = {}
    dup_candidates = []
    for d in docs:
        vid = d["vendor"].id
        if vid in seen:
            dup_candidates.append((d, seen[vid]))
        seen.setdefault(vid, d)
    rng.shuffle(dup_candidates)
    used: set[str] = set()
    dups = []
    for d, first in dup_candidates:
        if len(dups) == per_type["duplicate_invoice_number"]:
            break
        if d["vendor"].id in {x[0]["vendor"].id for x in dups}:
            continue
        dups.append((d, first))
        used |= {d["doc_id"], first["doc_id"]}
    for d, first in dups:
        d["invoice_number"] = first["invoice_number"]
        d["anomaly"] = {
            "type": "duplicate_invoice_number",
            "reason": f"Invoice number {first['invoice_number']} was already used by "
                      f"{first['doc_id']} from the same vendor ({d['vendor'].name}).",
            "duplicate_of": first["doc_id"],
        }

    pool = [d for d in docs if d["doc_id"] not in used]
    rng.shuffle(pool)
    for atype in ("total_mismatch", "missing_invoice_date", "tax_amount_mismatch"):
        for _ in range(per_type[atype]):
            d = pool.pop()
            base = d["subtotal"] - (d["discount"] or 0)
            if atype == "total_mismatch":
                correct = d["total"]
                delta = max(Decimal("1.00"), q2(correct * Decimal(str(rng.uniform(0.01, 0.08)))))
                d["total"] = correct + delta * rng.choice([1, -1])
                d["anomaly"] = {
                    "type": atype,
                    "reason": f"Printed total {d['total']} != subtotal - discount + tax = {correct}.",
                }
            elif atype == "missing_invoice_date":
                d["printed_invoice_date"] = None
                d["anomaly"] = {"type": atype, "reason": "Invoice date is not printed on the document."}
            else:
                correct = d["tax_amount"]
                wrong = correct
                while abs(wrong - correct) < Decimal("0.50"):
                    wrong_rate = d["tax_rate"] + Decimal(rng.choice([-5, -2, -1, 1, 2, 3, 5]))
                    if wrong_rate <= 0:
                        wrong_rate = d["tax_rate"] * 2
                    wrong = q2(base * wrong_rate / 100)
                    if abs(wrong - correct) < Decimal("0.50"):
                        wrong = correct + Decimal("0.50") * rng.choice([1, 2, 3])
                d["tax_amount"] = wrong
                d["total"] = base + wrong  # total is consistent with the (wrong) printed tax
                d["anomaly"] = {
                    "type": atype,
                    "reason": f"Printed tax {wrong} != {d['tax_rate'].normalize():f}% of {base} = {correct}.",
                }


# --------------------------------------------------------------------------------------
# PDF templates
# --------------------------------------------------------------------------------------


def register_fonts() -> None:
    if "Amiri" in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont("Amiri", str(FONT_DIR / "Amiri-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Amiri-Bold", str(FONT_DIR / "Amiri-Bold.ttf")))


def totals_rows(doc: dict, f: Fmt, labels: dict) -> list[tuple[str, str, bool]]:
    v = doc["vendor"]
    rows = [(labels["subtotal"], f.money(doc["subtotal"]), False)]
    if doc["discount"] is not None:
        lbl = labels["discount"]
        if doc["discount_pct"] is not None:
            lbl += f" ({f.pct(doc['discount_pct'])})"
        rows.append((lbl, "-" + f.money(doc["discount"]), False))
    rows.append((labels["tax"].format(tax=tax_name(v), TAX=tax_name(v).upper(), pct=f.pct(doc["tax_rate"])),
                 f.money(doc["tax_amount"]), False))
    rows.append((labels["total"].format(cur=v.currency), f.money(doc["total"]), True))
    return rows


def draw_t1_classic(c: canvas.Canvas, doc: dict, f: Fmt) -> None:
    v, (bname, _, baddr) = doc["vendor"], doc["buyer"]
    L, R = 50, W - 50
    y = H - 60
    c.setFont("Times-Bold", 18)
    c.drawString(L, y, v.name)
    c.setFont("Times-Roman", 10)
    for line in (*v.address, f"{v.tax_label}: {v.tax_id}"):
        y -= 13
        c.drawString(L, y, line)
    c.setFont("Times-Bold", 26)
    c.drawRightString(R, H - 62, "INVOICE")

    meta = [("Invoice No.:", doc["invoice_number"])]
    if doc["printed_invoice_date"]:
        meta.append(("Invoice Date:", f.date(doc["printed_invoice_date"])))
    meta += [("Due Date:", f.date(doc["due_date"])), ("Terms:", terms_text(doc["terms_days"]))]
    my = H - 92
    for k, val in meta:
        c.setFont("Times-Bold", 10)
        c.drawString(R - 200, my, k)
        c.setFont("Times-Roman", 10)
        c.drawRightString(R, my, val)
        my -= 14

    y = H - 180
    c.setFont("Times-Bold", 11)
    c.drawString(L, y, "Bill To:")
    c.setFont("Times-Roman", 10)
    for line in (bname, *baddr):
        y -= 13
        c.drawString(L, y, line)

    cols = [L, 330, 385, 465, R]
    y = H - 270
    c.setLineWidth(0.8)
    c.line(L, y + 14, R, y + 14)
    c.setFont("Times-Bold", 10)
    c.drawString(L + 4, y, "Description")
    c.drawRightString(cols[2] - 4, y, "Qty")
    c.drawRightString(cols[3] - 4, y, "Unit Price")
    c.drawRightString(R - 4, y, "Amount")
    c.line(L, y - 6, R, y - 6)
    c.setFont("Times-Roman", 10)
    for ln in doc["lines"]:
        y -= 20
        c.drawString(L + 4, y, ln["description"])
        c.drawRightString(cols[2] - 4, y, f.qty(ln["quantity"]))
        c.drawRightString(cols[3] - 4, y, f.money(ln["unit_price"]))
        c.drawRightString(R - 4, y, f.money(ln["line_total"]))
    c.line(L, y - 8, R, y - 8)
    for x in cols:
        c.line(x, H - 270 + 14, x, y - 8)

    y -= 28
    for label, val, bold in totals_rows(doc, f, {"subtotal": "Subtotal", "discount": "Discount",
                                                  "tax": "{tax} ({pct})", "total": "Total ({cur})"}):
        c.setFont("Times-Bold" if bold else "Times-Roman", 11 if bold else 10)
        c.drawString(cols[2], y, label)
        c.drawRightString(R - 4, y, val)
        y -= 16
    c.setFont("Times-Italic", 9)
    c.drawString(L, 60, "Thank you for your business. Please quote the invoice number with your payment.")


def draw_t2_modern(c: canvas.Canvas, doc: dict, f: Fmt) -> None:
    v, (bname, _, baddr) = doc["vendor"], doc["buyer"]
    accent = HexColor(v.color)
    L, R = 40, W - 40
    c.setFillColor(accent)
    c.rect(0, H - 100, W, 100, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(L, H - 55, v.name)
    c.setFont("Helvetica", 9)
    c.drawString(L, H - 72, " | ".join(v.address))
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(R, H - 55, "TAX INVOICE")

    cells = [("INVOICE #", doc["invoice_number"])]
    if doc["printed_invoice_date"]:
        cells.append(("ISSUED", f.date(doc["printed_invoice_date"])))
    cells += [("DUE", f.date(doc["due_date"])), ("CURRENCY", v.currency)]
    cw = (R - L) / len(cells)
    for i, (k, val) in enumerate(cells):
        x = L + i * cw
        c.setFillColor(HexColor("#888888"))
        c.setFont("Helvetica", 8)
        c.drawString(x, H - 130, k)
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, H - 145, val)

    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(HexColor("#888888"))
    c.drawString(L, H - 180, "BILLED TO")
    c.drawString(W / 2 + 20, H - 180, "FROM")
    c.setFillColor(black)
    c.setFont("Helvetica", 9.5)
    y = H - 194
    for line in (bname, *baddr):
        c.drawString(L, y, line)
        y -= 12
    c.drawString(W / 2 + 20, H - 194, v.name)
    c.drawString(W / 2 + 20, H - 206, f"{v.tax_label} {v.tax_id}")

    cols = {"#": L + 6, "item": L + 30, "qty": 360, "rate": 450, "total": R - 8}
    y = H - 270
    c.setFillColor(accent)
    c.rect(L, y - 6, R - L, 20, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(cols["#"], y, "#")
    c.drawString(cols["item"], y, "ITEM")
    c.drawRightString(cols["qty"], y, "QTY")
    c.drawRightString(cols["rate"], y, "RATE")
    c.drawRightString(cols["total"], y, "TOTAL")
    c.setFont("Helvetica", 9.5)
    for i, ln in enumerate(doc["lines"], start=1):
        y -= 20
        if i % 2 == 0:
            c.setFillColor(HexColor("#f1f1f1"))
            c.rect(L, y - 6, R - L, 20, stroke=0, fill=1)
        c.setFillColor(black)
        c.drawString(cols["#"], y, str(i))
        c.drawString(cols["item"], y, ln["description"])
        c.drawRightString(cols["qty"], y, f.qty(ln["quantity"]))
        c.drawRightString(cols["rate"], y, f.money(ln["unit_price"]))
        c.drawRightString(cols["total"], y, f.money(ln["line_total"]))

    y -= 36
    sym = CURRENCY_SYMBOL[v.currency]
    rows = totals_rows(doc, f, {"subtotal": "Subtotal", "discount": "Discount",
                                "tax": "{tax} {pct}", "total": "Amount Due"})
    for label, val, bold in rows[:-1]:
        c.setFont("Helvetica", 9.5)
        c.drawString(340, y, label)
        c.drawRightString(cols["total"], y, val)
        y -= 16
    y -= 10  # breathing room above the highlighted total
    label, val, _ = rows[-1]
    c.setFillColor(accent)
    c.rect(330, y - 12, R - 330, 28, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(340, y - 2, label)
    c.drawRightString(cols["total"], y - 2, f"{sym}{val}")
    c.setFillColor(HexColor("#888888"))
    c.setFont("Helvetica", 8)
    c.drawString(L, 50, f"Payment terms: {terms_text(doc['terms_days'])}.  Questions? Reply to this invoice.")


def draw_t3_receipt(c: canvas.Canvas, doc: dict, f: Fmt) -> None:
    v, (bname, _, _) = doc["vendor"], doc["buyer"]
    L, R = 150, W - 150
    cx = W / 2
    y = H - 60
    c.setFont("Courier-Bold", 12)
    c.drawCentredString(cx, y, v.name.upper())
    c.setFont("Courier", 9)
    for line in (*v.address, f"{v.tax_label} {v.tax_id}"):
        y -= 11
        c.drawCentredString(cx, y, line)
    dash = "-" * 49

    def kv(k: str, val: str) -> None:
        nonlocal y
        y -= 12
        c.drawString(L, y, f"{k:<9}: {val}")

    y -= 16
    c.drawCentredString(cx, y, dash)
    kv("BILL NO", doc["invoice_number"])
    if doc["printed_invoice_date"]:
        kv("DATED", f.date(doc["printed_invoice_date"]))
    kv("PAY BY", f.date(doc["due_date"]))
    kv("CUSTOMER", bname)
    y -= 8
    c.drawCentredString(cx, y - 4, dash)
    y -= 6
    for ln in doc["lines"]:
        y -= 12
        c.drawString(L, y, ln["description"])
        y -= 11
        c.drawString(L + 12, y, f"{f.qty(ln['quantity'])} x {f.money(ln['unit_price'])}")
        c.drawRightString(R, y, f.money(ln["line_total"]))
    y -= 8
    c.drawCentredString(cx, y - 4, dash)
    y -= 6
    for label, val, bold in totals_rows(doc, f, {"subtotal": "SUBTOTAL", "discount": "DISCOUNT",
                                                  "tax": "{TAX} {pct}", "total": "TOTAL {cur}"}):
        y -= 13
        c.setFont("Courier-Bold" if bold else "Courier", 10 if bold else 9)
        c.drawString(L, y, label)
        c.drawRightString(R, y, val)
    c.setFont("Courier", 9)
    y -= 10
    c.drawCentredString(cx, y - 4, dash)
    c.drawCentredString(cx, y - 20, f"TERMS: {terms_text(doc['terms_days']).upper()}")
    c.drawCentredString(cx, y - 34, "*** THANK YOU ***")


def draw_t4_two_column(c: canvas.Canvas, doc: dict, f: Fmt) -> None:
    v, (bname, _, baddr) = doc["vendor"], doc["buyer"]
    L, R = 45, W - 45
    mid = W / 2
    c.setFont("Helvetica-Bold", 16)
    c.drawString(L, H - 55, v.name)
    c.setFillColor(HexColor("#999999"))
    c.setFont("Helvetica", 24)
    c.drawRightString(R, H - 58, "Invoice")
    c.setFillColor(black)

    top, bottom = H - 80, H - 175
    c.setLineWidth(0.6)
    c.rect(L, bottom, mid - 8 - L, top - bottom)
    c.rect(mid + 8, bottom, R - mid - 8, top - bottom)
    for x0, title, lines in ((L, "SUPPLIER", (v.name, *v.address, f"{v.tax_label}: {v.tax_id}")),
                             (mid + 8, "CUSTOMER", (bname, *baddr))):
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x0 + 8, top - 14, title)
        c.setFont("Helvetica", 9)
        yy = top - 28
        for line in lines:
            c.drawString(x0 + 8, yy, line)
            yy -= 12

    hdr = [("Inv #", doc["invoice_number"])]
    if doc["printed_invoice_date"]:
        hdr.append(("Issue Date", f.date(doc["printed_invoice_date"])))
    hdr += [("Payment Due", f.date(doc["due_date"])), ("Terms", terms_text(doc["terms_days"]))]
    cw = (R - L) / len(hdr)
    yt = H - 195
    c.setFillColor(HexColor("#e8e8e8"))
    c.rect(L, yt - 4, R - L, 16, stroke=0, fill=1)
    c.setFillColor(black)
    c.rect(L, yt - 22, R - L, 34)
    for i, (k, val) in enumerate(hdr):
        x = L + i * cw
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(x + 6, yt, k)
        c.setFont("Helvetica", 9.5)
        c.drawString(x + 6, yt - 16, val)

    cols = {"qty": L + 40, "desc": L + 52, "unit": 450, "ext": R - 6}
    y = H - 260
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(cols["qty"], y, "Qty")
    c.drawString(cols["desc"], y, "Description")
    c.drawRightString(cols["unit"], y, "Unit")
    c.drawRightString(cols["ext"], y, "Ext.")
    c.line(L, y - 5, R, y - 5)
    c.setFont("Helvetica", 9.5)
    for ln in doc["lines"]:
        y -= 18
        c.drawRightString(cols["qty"], y, f.qty(ln["quantity"]))
        c.drawString(cols["desc"], y, ln["description"])
        c.drawRightString(cols["unit"], y, f.money(ln["unit_price"]))
        c.drawRightString(cols["ext"], y, f.money(ln["line_total"]))
        c.setStrokeColor(HexColor("#dddddd"))
        c.line(L, y - 6, R, y - 6)
        c.setStrokeColor(black)
    y -= 30
    for label, val, bold in totals_rows(doc, f, {"subtotal": "Net", "discount": "Less discount",
                                                  "tax": "{tax} @ {pct}", "total": "Amount Due ({cur})"}):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10.5 if bold else 9.5)
        if bold:
            c.line(360, y + 13, R, y + 13)
        c.drawString(360, y, label)
        c.drawRightString(cols["ext"], y, val)
        y -= 16
    c.setFont("Helvetica", 8)
    c.drawString(L, 50, "Bank details available on request. Late payments may incur interest as per agreed terms.")


def draw_t5_bilingual(c: canvas.Canvas, doc: dict, f: Fmt) -> None:
    v, (bname, bname_ar, baddr) = doc["vendor"], doc["buyer"]
    L, R = 40, W - 40
    cx = W / 2
    c.setFont("Amiri-Bold", 18)
    c.drawCentredString(cx, H - 50, ar("فاتورة ضريبية"))
    c.setFont("Amiri-Bold", 13)
    c.drawCentredString(cx, H - 68, "TAX INVOICE")

    # Vendor: English on the left, Arabic on the right.
    c.setFont("Amiri-Bold", 12)
    c.drawString(L, H - 100, v.name)
    c.drawRightString(R, H - 100, ar(v.name_ar or v.name))
    c.setFont("Amiri", 9.5)
    y = H - 114
    for line in v.address:
        c.drawString(L, y, line)
        y -= 11

    def row(y: float, en: str, value: str, ar_label: str) -> None:
        c.setFont("Amiri-Bold", 10)
        c.drawString(L, y, en)
        c.drawRightString(R, y, ar(ar_label))
        c.setFont("Amiri", 10.5)
        c.drawCentredString(cx, y, value)

    y = H - 162
    c.setLineWidth(0.5)
    c.line(L, y + 14, R, y + 14)
    rows = [("Invoice No.", doc["invoice_number"], "رقم الفاتورة")]
    if doc["printed_invoice_date"]:
        rows.append(("Invoice Date", f.date(doc["printed_invoice_date"]), "تاريخ الفاتورة"))
    rows += [
        ("Due Date", f.date(doc["due_date"]), "تاريخ الاستحقاق"),
        ("Currency", v.currency, "العملة"),
        (f"Supplier {v.tax_label}", v.tax_id, f"{v.tax_label_ar} للمورد"),
        ("Customer", bname, "العميل"),
    ]
    for en, val, arl in rows:
        row(y, en, val, arl)
        y -= 15
    c.setFont("Amiri", 10)
    c.drawCentredString(cx, y, ar(bname_ar))
    y -= 12
    c.setFont("Amiri", 9)
    c.drawCentredString(cx, y, ", ".join(baddr))
    c.line(L, y - 8, R, y - 8)

    # Line items table.
    cols = [L, 330, 385, 465, R]
    heads = [("Description", "الوصف"), ("Qty", "الكمية"), ("Unit Price", "سعر الوحدة"), ("Total", "الإجمالي")]
    y -= 30
    c.setFillColor(HexColor("#efe6d2"))
    c.rect(L, y - 12, R - L, 28, stroke=0, fill=1)
    c.setFillColor(black)
    for i, (en, arl) in enumerate(heads):
        x0, x1 = cols[i], cols[i + 1]
        c.setFont("Amiri-Bold", 9)
        c.drawCentredString((x0 + x1) / 2, y + 4, en)
        c.drawCentredString((x0 + x1) / 2, y - 8, ar(arl))
    table_top = y + 16
    y -= 12
    rh = 25
    for ln in doc["lines"]:
        y -= rh
        c.setFont("Amiri", 9.5)
        c.drawString(L + 4, y + 13, ln["description"])
        c.setFont("Amiri", 9)
        c.drawRightString(cols[1] - 4, y + 3, ar(ln["description_ar"]))
        c.setFont("Amiri", 10)
        c.drawRightString(cols[2] - 6, y + 8, f.qty(ln["quantity"]))
        c.drawRightString(cols[3] - 6, y + 8, f.money(ln["unit_price"]))
        c.drawRightString(R - 6, y + 8, f.money(ln["line_total"]))
        c.line(L, y, R, y)
    for x in cols:
        c.line(x, table_top, x, y)
    c.line(L, table_top, R, table_top)

    ar_labels = {"subtotal": "المجموع الفرعي", "discount": "الخصم",
                 "tax": "ضريبة القيمة المضافة", "total": "الإجمالي المستحق"}
    tot = totals_rows(doc, f, {"subtotal": "Subtotal", "discount": "Discount",
                               "tax": "VAT {pct}", "total": "Total Due ({cur})"})
    keys = ["subtotal"] + (["discount"] if doc["discount"] is not None else []) + ["tax", "total"]
    y -= 18
    for key, (label, val, bold) in zip(keys, tot):
        c.setFont("Amiri-Bold" if bold else "Amiri", 11 if bold else 10)
        c.drawString(250, y, label)
        c.drawRightString(440, y, val)
        c.drawRightString(R, y, ar(ar_labels[key]))
        y -= 16
    c.setFont("Amiri", 9)
    c.drawCentredString(cx, 45, ar("شكرا لتعاملكم معنا") + "  —  Thank you for your business")


DRAWERS = {
    "T1_classic": draw_t1_classic,
    "T2_modern": draw_t2_modern,
    "T3_receipt": draw_t3_receipt,
    "T4_two_column": draw_t4_two_column,
    "T5_bilingual_ar_en": draw_t5_bilingual,
}


def render_pdf(doc: dict, path: Path) -> None:
    v = doc["vendor"]
    num_style = "en" if doc["template"] == "T5_bilingual_ar_en" else v.num_style
    f = Fmt(num_style, doc["digits"], date_style_for(doc["template"], v))
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)  # invariant -> byte-reproducible
    c.setTitle(f"Invoice {doc['invoice_number']}")
    c.setAuthor(v.name)
    DRAWERS[doc["template"]](c, doc, f)
    c.showPage()
    c.save()


# --------------------------------------------------------------------------------------
# Scanned-looking PNGs
# --------------------------------------------------------------------------------------


def render_scan(pdf_path: Path, png_path: Path, rng: np.random.Generator) -> dict:
    dpi = int(rng.integers(100, 151))
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        img = pdf[0].render(scale=dpi / 72).to_pil().convert("L")
    finally:
        pdf.close()

    arr = np.asarray(img, dtype=np.float32)
    ink_fade = rng.uniform(0.80, 0.95)
    paper = rng.uniform(232, 248)
    arr = paper - (255 - arr) * ink_fade * (paper / 255)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    angle = float(rng.uniform(-2.5, 2.5))
    img = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=int(paper))
    blur = float(rng.uniform(0.4, 1.1))
    img = img.filter(ImageFilter.GaussianBlur(radius=blur))

    arr = np.asarray(img, dtype=np.float32)
    sigma = float(rng.uniform(5, 12))
    arr = arr + rng.normal(0, sigma, size=arr.shape)
    speckles = rng.random(arr.shape) < 0.0008
    arr[speckles] = rng.uniform(40, 120)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    quality = int(rng.integers(45, 76))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    buf.seek(0)
    Image.open(buf).convert("L").save(png_path, optimize=True)
    return {"dpi": dpi, "rotation_deg": round(angle, 2), "blur_radius": round(blur, 2),
            "noise_sigma": round(sigma, 2), "jpeg_quality": quality}


# --------------------------------------------------------------------------------------
# Ground truth, manifest, samples
# --------------------------------------------------------------------------------------


def money_str(d: Decimal | None) -> str | None:
    return None if d is None else f"{d:.2f}"


def ground_truth(doc: dict, seed: int) -> dict:
    v = doc["vendor"]
    is_t5 = doc["template"] == "T5_bilingual_ar_en"
    # Ground truth is "as printed": the T3 receipt prints the vendor name in upper case (draw_t3_receipt).
    printed_name = v.name.upper() if doc["template"] == "T3_receipt" else v.name
    return {
        "doc_id": doc["doc_id"],
        "invoice": {
            "vendor_name": printed_name,
            "vendor_name_ar": v.name_ar if is_t5 else None,
            "vendor_tax_id": v.tax_id,
            "buyer_name": doc["buyer"][0],
            "invoice_number": doc["invoice_number"],
            "invoice_date": doc["printed_invoice_date"].isoformat() if doc["printed_invoice_date"] else None,
            "due_date": doc["due_date"].isoformat(),
            "currency": v.currency,
            "line_items": [
                {
                    "description": ln["description"],
                    "description_ar": ln["description_ar"] if is_t5 else None,
                    "quantity": f"{ln['quantity'].normalize():f}",
                    "unit_price": money_str(ln["unit_price"]),
                    "line_total": money_str(ln["line_total"]),
                }
                for ln in doc["lines"]
            ],
            "subtotal": money_str(doc["subtotal"]),
            "discount": money_str(doc["discount"]),
            "tax_rate": f"{doc['tax_rate'].normalize():f}",
            "tax_amount": money_str(doc["tax_amount"]),
            "total": money_str(doc["total"]),
        },
        "is_anomaly": doc["anomaly"] is not None,
        "anomaly": doc["anomaly"],
        "meta": {
            "vendor_id": v.id,
            "template": doc["template"],
            "language": "ar-en" if is_t5 else "en",
            "digits": doc["digits"],
            "payment_terms_days": doc["terms_days"],
            "files": {"pdf": f"pdf/{doc['doc_id']}.pdf",
                      "png": f"scans/{doc['doc_id']}.png" if doc["has_scan"] else None},
            "scan": doc.get("scan_params"),
            "generator_seed": seed,
        },
    }


MANIFEST_FIELDS = ["doc_id", "vendor_id", "vendor_name", "template", "language", "digits", "currency",
                   "invoice_number", "invoice_date", "total", "n_lines", "has_discount", "has_scan",
                   "is_anomaly", "anomaly_type", "pdf_path", "png_path"]


def manifest_row(gt: dict) -> dict:
    inv, meta = gt["invoice"], gt["meta"]
    return {
        "doc_id": gt["doc_id"], "vendor_id": meta["vendor_id"], "vendor_name": inv["vendor_name"],
        "template": meta["template"], "language": meta["language"], "digits": meta["digits"],
        "currency": inv["currency"], "invoice_number": inv["invoice_number"],
        "invoice_date": inv["invoice_date"] or "", "total": inv["total"], "n_lines": len(inv["line_items"]),
        "has_discount": inv["discount"] is not None, "has_scan": meta["files"]["png"] is not None,
        "is_anomaly": gt["is_anomaly"], "anomaly_type": (gt["anomaly"] or {}).get("type", ""),
        "pdf_path": meta["files"]["pdf"], "png_path": meta["files"]["png"] or "",
    }


def clean_outputs(paths: list[Path]) -> None:
    """Remove previously generated doc_* files only (never anything else)."""
    for d in paths:
        if d.exists():
            for p in d.glob("doc_*"):
                p.unlink()


def write_samples(out: Path, samples_dir: Path, gts: list[dict]) -> list[dict]:
    """Copy a curated set (1 PDF per template + 2 scans) into data/samples/."""
    samples_dir.mkdir(parents=True, exist_ok=True)
    clean_outputs([samples_dir])
    picks: list[tuple[dict, str, str]] = []  # (gt, file kind, note)
    used_currencies: set[str] = set()
    for tpl in TEMPLATES:
        cands = [g for g in gts if g["meta"]["template"] == tpl and not g["is_anomaly"]
                 and len(g["invoice"]["line_items"]) >= 3]
        if tpl == "T5_bilingual_ar_en":
            cands = [g for g in cands if g["meta"]["digits"] == "arabic_indic"] or cands
        if cands:  # prefer a currency not shown yet, so the samples cover more variety
            g = next((x for x in cands if x["invoice"]["currency"] not in used_currencies), cands[0])
            used_currencies.add(g["invoice"]["currency"])
            note = "Arabic-Indic digits" if g["meta"]["digits"] == "arabic_indic" else ""
            picks.append((g, "pdf", note))
    scanned = [g for g in gts if g["meta"]["files"]["png"]]
    normal_scan = next((g for g in scanned if not g["is_anomaly"]), None)
    anomaly_scan = next((g for g in scanned if g["is_anomaly"]), None)
    for g, note in ((normal_scan, "scan"), (anomaly_scan, "scan, anomaly")):
        if g:
            picks.append((g, "png", note))

    rows = []
    for g, kind, note in picks:
        src = out / g["meta"]["files"][kind]
        shutil.copy2(src, samples_dir / src.name)
        (samples_dir / f"{g['doc_id']}.json").write_text(json.dumps(g, indent=2, ensure_ascii=False) + "\n",
                                                         encoding="utf-8")
        a = g["anomaly"]
        rows.append({"doc_id": g["doc_id"], "file": src.name, "template": g["meta"]["template"],
                     "vendor": g["invoice"]["vendor_name"], "currency": g["invoice"]["currency"],
                     "note": "; ".join(x for x in (note, a and f"{a['type']}: {a['reason']}") if x)})

    md = ["# Sample invoices", "",
          "A curated subset of the synthetic dataset, committed so the examples can be viewed without "
          "running the generator. Regenerated automatically by `python src/generate_invoices.py`.", "",
          "| Document | Ground truth | Template | Vendor (fictional) | Currency | Notes |",
          "|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| [{r['file']}]({r['file']}) | [{r['doc_id']}.json]({r['doc_id']}.json) | {r['template']} "
                  f"| {r['vendor']} | {r['currency']} | {r['note']} |")
    (samples_dir / "README.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return rows


def generate(out: Path = DEFAULT_OUT, seed: int = DEFAULT_SEED, n: int = DEFAULT_N,
             scan_ratio: float = SCAN_RATIO, anomaly_ratio: float = ANOMALY_RATIO,
             samples_dir: Path | None = DEFAULT_SAMPLES) -> list[dict]:
    register_fonts()
    pdf_dir, scan_dir, gt_dir = out / "pdf", out / "scans", out / "ground_truth"
    for d in (pdf_dir, scan_dir, gt_dir):
        d.mkdir(parents=True, exist_ok=True)
    clean_outputs([pdf_dir, scan_dir, gt_dir])

    docs = build_dataset(seed, n, scan_ratio, anomaly_ratio)
    np_rng = np.random.default_rng(seed)
    gts = []
    for doc in docs:
        pdf_path = pdf_dir / f"{doc['doc_id']}.pdf"
        render_pdf(doc, pdf_path)
        if doc["has_scan"]:
            doc["scan_params"] = render_scan(pdf_path, scan_dir / f"{doc['doc_id']}.png", np_rng)
        gt = ground_truth(doc, seed)
        (gt_dir / f"{doc['doc_id']}.json").write_text(json.dumps(gt, indent=2, ensure_ascii=False) + "\n",
                                                      encoding="utf-8")
        gts.append(gt)

    with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        w.writerows(manifest_row(g) for g in gts)

    if samples_dir is not None:
        write_samples(out, samples_dir, gts)
    return gts


def print_summary(gts: list[dict]) -> None:
    from collections import Counter

    def show(title: str, counter: Counter) -> None:
        print(f"{title}: " + ", ".join(f"{k}={v}" for k, v in sorted(counter.items())))

    print(f"invoices: {len(gts)}")
    print(f"scanned PNGs: {sum(1 for g in gts if g['meta']['files']['png'])}")
    print(f"anomalies: {sum(g['is_anomaly'] for g in gts)}")
    show("anomalies by type", Counter(g["anomaly"]["type"] for g in gts if g["anomaly"]))
    show("per template", Counter(g["meta"]["template"] for g in gts))
    show("per currency", Counter(g["invoice"]["currency"] for g in gts))
    show("digits", Counter(g["meta"]["digits"] for g in gts))
    print(f"vendors: {len({g['meta']['vendor_id'] for g in gts})}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--n", type=int, default=DEFAULT_N, help="number of invoices")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--scan-ratio", type=float, default=SCAN_RATIO)
    p.add_argument("--anomaly-ratio", type=float, default=ANOMALY_RATIO)
    p.add_argument("--no-samples", action="store_true", help="do not refresh data/samples/")
    a = p.parse_args(argv)
    gts = generate(a.out, a.seed, a.n, a.scan_ratio, a.anomaly_ratio,
                   None if a.no_samples else DEFAULT_SAMPLES)
    print_summary(gts)


if __name__ == "__main__":
    main()
