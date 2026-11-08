"""Pydantic models for an extracted invoice, aligned with the ground-truth `invoice` object.

Two representations:
- `ExtractedInvoice`: the typed model the rest of the pipeline uses (Decimal money, `date` dates).
- `RESPONSE_JSON_SCHEMA`: what we ask the LLM to emit. Money, rates and dates are *strings* there,
  so the model never round-trips amounts through binary floats. `parse_invoice` converts.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .normalize import normalize_date, normalize_digits, normalize_number

MONEY_FIELDS = ("subtotal", "discount", "tax_rate", "tax_amount", "total")
LINE_NUMBER_FIELDS = ("quantity", "unit_price", "line_total")
DATE_FIELDS = ("invoice_date", "due_date")
TEXT_FIELDS = ("vendor_name", "vendor_name_ar", "vendor_tax_id", "buyer_name", "invoice_number", "currency")
HEADER_FIELDS = (
    "vendor_name", "vendor_name_ar", "vendor_tax_id", "buyer_name", "invoice_number",
    "invoice_date", "due_date", "currency", "subtotal", "discount", "tax_rate", "tax_amount", "total",
)


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None


class ExtractedInvoice(BaseModel):
    """All fields are nullable: null means 'not printed on the document', never a guess."""

    model_config = ConfigDict(extra="forbid")

    vendor_name: str | None = None
    vendor_name_ar: str | None = Field(None, description="Arabic vendor name, only if printed in Arabic.")
    vendor_tax_id: str | None = None
    buyer_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = Field(None, description="ISO 4217 code, e.g. USD, EUR, SAR.")
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Decimal | None = None
    discount: Decimal | None = Field(None, description="Discount amount (not percent); null if none printed.")
    tax_rate: Decimal | None = Field(None, description="Percentage, e.g. 15 for 15%.")
    tax_amount: Decimal | None = None
    total: Decimal | None = None
    uncertain_fields: list[str] = Field(
        default_factory=list,
        description="Field names the model could not read clearly, e.g. 'total' or 'line_items[2].unit_price'.",
    )

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str | None) -> str | None:
        return v.strip().upper() if isinstance(v, str) else v


def _nullable_string(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vendor_name": _nullable_string("Seller / issuer name in Latin script, exactly as printed."),
        "vendor_name_ar": _nullable_string("Seller name in Arabic script, exactly as printed; null if none."),
        "vendor_tax_id": _nullable_string("Seller tax / VAT / TRN number exactly as printed."),
        "buyer_name": _nullable_string("Customer / bill-to name in Latin script, exactly as printed."),
        "invoice_number": _nullable_string("Invoice number exactly as printed."),
        "invoice_date": _nullable_string("Issue date as ISO YYYY-MM-DD; null if not printed."),
        "due_date": _nullable_string("Due date as ISO YYYY-MM-DD; null if not printed."),
        "currency": _nullable_string("ISO 4217 currency code."),
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": _nullable_string("Item description in Latin script, exactly as printed."),
                    "quantity": _nullable_string("Plain decimal string, e.g. 3.5"),
                    "unit_price": _nullable_string("Plain decimal string, e.g. 1234.50"),
                    "line_total": _nullable_string("Plain decimal string."),
                },
                "required": ["description", "quantity", "unit_price", "line_total"],
            },
        },
        "subtotal": _nullable_string("Plain decimal string."),
        "discount": _nullable_string("Discount amount as plain decimal string; null if none printed."),
        "tax_rate": _nullable_string("Tax percentage as plain decimal string, e.g. 15 or 8.25."),
        "tax_amount": _nullable_string("Plain decimal string."),
        "total": _nullable_string("Grand total as plain decimal string."),
        "uncertain_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "vendor_name", "vendor_name_ar", "vendor_tax_id", "buyer_name", "invoice_number",
        "invoice_date", "due_date", "currency", "line_items", "subtotal", "discount",
        "tax_rate", "tax_amount", "total", "uncertain_fields",
    ],
}


def _clean_text(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    v = v.strip()
    return v or None


def normalize_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Apply the digit/number/date safety net to a raw model payload (does not mutate input)."""
    out = dict(data)
    for f in TEXT_FIELDS:
        if f in out:
            out[f] = _clean_text(out[f])
    for f in ("vendor_tax_id", "invoice_number"):  # identifiers may be printed in Arabic-Indic digits
        if isinstance(out.get(f), str):
            out[f] = normalize_digits(out[f])
    for f in MONEY_FIELDS:
        if f in out:
            out[f] = normalize_number(out[f])
    for f in DATE_FIELDS:
        if f in out:
            out[f] = normalize_date(out[f])
    items = []
    for item in out.get("line_items") or []:
        item = dict(item)
        item["description"] = _clean_text(item.get("description"))
        for f in LINE_NUMBER_FIELDS:
            if f in item:
                item[f] = normalize_number(item[f])
        items.append(item)
    out["line_items"] = items
    out["uncertain_fields"] = [str(x) for x in (out.get("uncertain_fields") or [])]
    return out


def parse_invoice(raw: str | dict[str, Any]) -> ExtractedInvoice:
    """Parse a model response (JSON text or dict) into a validated ExtractedInvoice."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return ExtractedInvoice.model_validate(normalize_payload(data))
