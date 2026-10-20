"""Ground-truth helpers for evals: query the simulator DB directly (no HTTP, no auth)."""
from __future__ import annotations

from . import db

BILL_SELECT = (
    "SELECT b.id, v.name AS vendor, v.erp_code AS vendor_code, b.invoice_number, b.amount, b.currency,"
    " b.issue_date, b.due_date, b.notes, b.status, b.created_at"
    " FROM bills b JOIN vendors v ON v.id = b.vendor_id"
)


def get_bills(vendor: str | None = None, invoice_number: str | None = None) -> list[dict]:
    """ERP bills, optionally filtered by exact (case-insensitive) vendor name/code and invoice number."""
    sql, params = BILL_SELECT + " WHERE 1=1", []
    if vendor:
        sql += " AND (LOWER(v.name) = LOWER(?) OR LOWER(v.erp_code) = LOWER(?))"
        params += [vendor.strip(), vendor.strip()]
    if invoice_number:
        sql += " AND LOWER(b.invoice_number) = LOWER(?)"
        params.append(invoice_number.strip())
    return db.query(sql + " ORDER BY b.id", params)


def get_bill(bill_id: int) -> dict | None:
    return db.query_one(BILL_SELECT + " WHERE b.id = ?", (bill_id,))


def get_sent_mail() -> list[dict]:
    """Messages in the Sent folder, oldest first."""
    return db.query("SELECT * FROM emails WHERE folder = 'sent' ORDER BY id")


def get_invoice(number: str) -> dict | None:
    """Invoice by number (includes invoices hidden from the portal), with `items`."""
    inv = db.query_one(
        "SELECT i.*, v.name AS vendor, v.erp_code AS vendor_code FROM invoices i JOIN vendors v ON v.id = i.vendor_id"
        " WHERE LOWER(i.number) = LOWER(?)", (number.strip(),))
    if inv:
        inv["items"] = db.query(
            "SELECT description, quantity, unit_price FROM invoice_items WHERE invoice_id = ? ORDER BY id", (inv["id"],))
    return inv


def list_invoices(vendor_name: str | None = None) -> list[dict]:
    """All invoices (including portal-hidden ones), newest issue date first."""
    sql = "SELECT i.*, v.name AS vendor FROM invoices i JOIN vendors v ON v.id = i.vendor_id"
    params: list = []
    if vendor_name:
        sql += " WHERE LOWER(v.name) = LOWER(?)"
        params.append(vendor_name.strip())
    return db.query(sql + " ORDER BY i.issue_date DESC, i.id DESC", params)


def latest_invoice_for(vendor_name: str) -> dict | None:
    """Most recent invoice (by issue date) for an exact vendor name, even if hidden from the portal."""
    rows = list_invoices(vendor_name)
    return get_invoice(rows[0]["number"]) if rows else None


def invoice_rows() -> list[dict]:
    """Every invoice (including portal-hidden ones) as plain rows, in internal-id order: what the vendor portal serves from."""
    rows = db.query(
        "SELECT i.number, v.name AS vendor, i.issue_date, i.due_date, i.amount, i.currency, i.status, i.portal_visible"
        " FROM invoices i JOIN vendors v ON v.id = i.vendor_id ORDER BY i.id")
    return [{**r, "portal_visible": bool(r["portal_visible"])} for r in rows]


def get_inbox() -> list[dict]:
    """Messages in the Inbox folder, oldest first."""
    return db.query("SELECT id, sender, recipient, subject, body, sent_at, attachment_invoice, is_read"
                    " FROM emails WHERE folder = 'inbox' ORDER BY id")
