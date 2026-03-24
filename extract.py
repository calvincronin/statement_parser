#!/usr/bin/env python3
"""
Credit union statement PDF extractor.
Uses pdfplumber to get raw text, then parses transaction lines using
structural markers specific to the statement format.

Structure:
  - Each page starts with junk, ending with "to thank you for your business."
  - Table sections begin with: MM/DD ID ## ACCOUNT_NAME Previous Balance ##.##
  - Table sections end with: "Annual Percentage Yield Earned..."
  - Transactions can span pages (no repeated account header on continuation pages)
"""

import argparse
import csv
import re
import sys
from pathlib import Path

import pdfplumber
from openpyxl import Workbook

# Matches a dollar amount like 1,234.56 or -1,234.56 or 1234.56 or -1234.56
MONEY_RE = re.compile(r"-?[\d,]+\.\d{2}")

# Matches a date like MM/DD or MM/DD/YY or MM/DD/YYYY
DATE_RE = re.compile(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?")

# Table start: "MM/DD ID ## ACCOUNT_NAME Previous Balance ##.##"
TABLE_START_RE = re.compile(
    r"^(\d{1,2}/\d{1,2})\s+ID\s+\d+\s+(.+?)\s+Previous\s+Balance\s+([\d,]+\.\d{2})",
    re.IGNORECASE,
)

# Table end marker
TABLE_END_RE = re.compile(r"New\s+Balance", re.IGNORECASE)

# Page junk end marker
PAGE_JUNK_END_RE = re.compile(r"to\s+thank\s+you\s+for\s+your\s+business", re.IGNORECASE)

# Summary/metadata lines to skip when inside a table
SKIP_PATTERNS = [
    re.compile(r"Dividends\s+Earned\s+Year\s+to\s+Date", re.IGNORECASE),
    re.compile(r"page\s+\d+", re.IGNORECASE),
    re.compile(r"continued\s+on", re.IGNORECASE),
    re.compile(r"account\s+number", re.IGNORECASE),
    re.compile(r"statement\s+period", re.IGNORECASE),
    re.compile(r"send\s+inquiries", re.IGNORECASE),
    re.compile(r"credit\s+union", re.IGNORECASE),
]

COLUMNS = [
    "Account",
    "Posting Date",
    "Transaction Description",
    "Transaction Amount",
    "Balance",
]


def should_skip(line: str) -> bool:
    """Check if a line is summary/metadata to skip."""
    stripped = line.strip()
    if not stripped:
        return True
    return any(pat.search(stripped) for pat in SKIP_PATTERNS)


def parse_transaction_line(line: str) -> dict | None:
    """
    Parse a transaction line that starts with a date.
    Returns dict with posting_date, description, amounts or None.
    """
    stripped = line.strip()
    # Must start with a date
    date_match = re.match(r"^(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+", stripped)
    if not date_match:
        return None

    posting_date = date_match.group(1)
    rest = stripped[date_match.end():]

    # Find all dollar amounts in the rest of the line
    money_matches = list(MONEY_RE.finditer(rest))

    if not money_matches:
        # Date but no amounts — still a transaction, amounts might be absent
        return {
            "posting_date": posting_date,
            "description": rest.strip(),
            "amounts": [],
        }

    # Everything before the first dollar amount is the description
    first_money_start = money_matches[0].start()
    description = rest[:first_money_start].strip()

    # Collect amounts (preserve negative signs)
    amounts = [m.group().replace(",", "") for m in money_matches]

    return {
        "posting_date": posting_date,
        "description": description,
        "amounts": amounts,
    }


def is_continuation_line(line: str) -> bool:
    """
    A continuation line has text but does NOT start with a date
    and is not a structural marker or junk.
    """
    stripped = line.strip()
    if not stripped:
        return False
    # Starts with a date → not continuation
    if re.match(r"^\d{1,2}/\d{1,2}", stripped):
        return False
    if should_skip(stripped):
        return False
    if TABLE_END_RE.search(stripped):
        return False
    if PAGE_JUNK_END_RE.search(stripped):
        return False
    return True


def extract_transactions(pdf_path: str, pages: set[int] | None = None, debug: bool = False) -> list[dict]:
    """Extract transactions from a credit union statement PDF."""
    transactions = []
    current_account = None
    in_table = False
    past_junk = False  # tracks whether we've passed page junk on current page
    expect_debit_detail = False  # True only on the line immediately after a debit card withdrawal

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            if pages and page_num not in pages:
                continue

            text = page.extract_text()
            if not text:
                if debug:
                    print(f"[DEBUG] Page {page_num}: extract_text() returned empty", file=sys.stderr)
                continue

            lines = text.split("\n")
            past_junk = False

            if debug:
                print(f"\n[DEBUG] === Page {page_num} ({len(lines)} lines) ===", file=sys.stderr)
                for i, ln in enumerate(lines):
                    print(f"[DEBUG]   {i:3d}: {ln!r}", file=sys.stderr)
                print(f"[DEBUG] === End Page {page_num} raw ===\n", file=sys.stderr)

            for line in lines:
                stripped = line.strip()

                # Detect end of page junk
                if not past_junk:
                    if PAGE_JUNK_END_RE.search(stripped):
                        past_junk = True
                        if debug:
                            print(f"[DEBUG] PAGE JUNK ENDED: {stripped!r}", file=sys.stderr)
                        continue

                    # Escape junk when we recognise table data:
                    # - mid-table: a transaction line or table end/start marker
                    # - between tables: a new table start marker
                    if in_table:
                        txn_probe = parse_transaction_line(stripped)
                        if (txn_probe and txn_probe["amounts"]
                                or TABLE_END_RE.search(stripped)
                                or TABLE_START_RE.match(stripped)):
                            past_junk = True
                            if debug:
                                print(f"[DEBUG] PAGE JUNK ENDED (mid-table, matched data): {stripped!r}", file=sys.stderr)
                            # Fall through to process this line normally
                        else:
                            if debug and stripped:
                                print(f"[DEBUG] SKIPPED (page junk): {stripped!r}", file=sys.stderr)
                            continue
                    elif TABLE_START_RE.match(stripped):
                        past_junk = True
                        if debug:
                            print(f"[DEBUG] PAGE JUNK ENDED (table start): {stripped!r}", file=sys.stderr)
                        # Fall through to process this line normally
                    else:
                        if debug and stripped:
                            print(f"[DEBUG] SKIPPED (page junk): {stripped!r}", file=sys.stderr)
                        continue

                # Detect table end
                if TABLE_END_RE.search(stripped):
                    in_table = False
                    if debug:
                        print(f"[DEBUG] TABLE END: {stripped!r} (was account={current_account!r})", file=sys.stderr)
                    continue

                # Detect table start
                table_match = TABLE_START_RE.match(stripped)
                if table_match:
                    current_account = table_match.group(2).strip()
                    in_table = True
                    if debug:
                        print(f"[DEBUG] TABLE START: account={current_account!r} prev_bal={table_match.group(3)}", file=sys.stderr)
                    continue

                # If we're in a table (or continuing from previous page), parse transactions
                if in_table:
                    if should_skip(stripped):
                        if debug:
                            print(f"[DEBUG] SKIPPED (junk): {stripped!r}", file=sys.stderr)
                        continue

                    # Check if this is a debit card detail line:
                    # Only on the very next line after a debit card withdrawal
                    if (expect_debit_detail
                            and re.match(r"^\d{1,2}/\d{1,2}", stripped)):
                        prev = transactions[-1]
                        prev["description"] += " " + stripped
                        expect_debit_detail = False
                        if debug:
                            print(f"[DEBUG] DEBIT CARD DETAIL: {stripped!r}", file=sys.stderr)
                        continue

                    expect_debit_detail = False

                    txn = parse_transaction_line(stripped)
                    if txn:
                        txn["account"] = current_account
                        # Flag if this is a debit card withdrawal so next line is treated as detail
                        if "debit card" in txn["description"].lower():
                            expect_debit_detail = True
                        transactions.append(txn)
                        if debug:
                            print(f"[DEBUG] TRANSACTION: date={txn['posting_date']!r} "
                                  f"desc={txn['description']!r} amounts={txn['amounts']} "
                                  f"account={current_account!r}", file=sys.stderr)
                        continue

                    if transactions and is_continuation_line(stripped):
                        prev = transactions[-1]
                        prev["description"] += " " + stripped
                        if debug:
                            print(f"[DEBUG] CONTINUATION: {stripped!r}", file=sys.stderr)
                        continue

                    if debug:
                        print(f"[DEBUG] UNMATCHED (in table): {stripped!r}", file=sys.stderr)
                else:
                    # Past junk but not in a table — could be between tables or
                    # continuation from previous page
                    if debug and stripped:
                        print(f"[DEBUG] SKIPPED (between tables): {stripped!r}", file=sys.stderr)

    return transactions


def transactions_to_rows(transactions: list[dict]) -> list[list[str]]:
    """Convert transaction dicts to rows matching COLUMNS order."""
    rows = []
    for txn in transactions:
        amounts = txn["amounts"]
        # Typically: last amount = balance, second-to-last = transaction amount
        balance = amounts[-1] if len(amounts) >= 1 else ""
        txn_amount = amounts[-2] if len(amounts) >= 2 else ""

        row = [
            txn.get("account", ""),
            txn["posting_date"],
            txn["description"],
            txn_amount,
            balance,
        ]
        rows.append(row)
    return rows


def write_csv(rows: list[list[str]], output_path: str):
    """Write rows to CSV with header."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow(row)
    print(f"Wrote {len(rows)} transaction(s) to {output_path}")


def write_xlsx(rows: list[list[str]], output_path: str):
    """Write rows to XLSX with header."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(COLUMNS)
    for row in rows:
        ws.append(row)
    wb.save(output_path)
    print(f"Wrote {len(rows)} transaction(s) to {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Extract transactions from credit union statement PDFs"
    )
    parser.add_argument("pdf", help="Path to the input PDF file")
    parser.add_argument(
        "-o", "--output",
        help="Output file path (default: same name as input with .csv extension)",
    )
    parser.add_argument(
        "-f", "--format",
        choices=["csv", "xlsx"],
        default="csv",
        help="Output format (default: csv)",
    )
    parser.add_argument(
        "--pages",
        help="Comma-separated page numbers to extract (e.g. 1,2,5). Default: all pages",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print raw extracted text and parsing decisions for debugging",
    )

    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: {pdf_path} not found", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = args.output
    else:
        output_path = str(pdf_path.with_suffix(f".{args.format}"))

    selected_pages = None
    if args.pages:
        selected_pages = {int(p.strip()) for p in args.pages.split(",")}

    transactions = extract_transactions(str(pdf_path), pages=selected_pages, debug=args.debug)

    if not transactions:
        print("Warning: No transactions found in the PDF.", file=sys.stderr)

    rows = transactions_to_rows(transactions)

    if args.format == "csv":
        write_csv(rows, output_path)
    else:
        write_xlsx(rows, output_path)


if __name__ == "__main__":
    main()
