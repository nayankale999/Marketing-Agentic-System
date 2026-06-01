"""CSV parsing + per-row validation for the audience-seed upload flow
(E01-S02, E01-S03).

Pure functions — no DB, no HTTP. The API layer wraps `parse_csv()` and
inserts the valid rows. Errors are returned as structured records so the
caller can surface a downloadable report keyed by row number.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

# Header row is row 1 in 1-indexed terms; first data row is 2.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")

REQUIRED_FIELDS: tuple[str, ...] = ("email",)
KNOWN_FIELDS: tuple[str, ...] = (
    "email",
    "first_name",
    "last_name",
    "company",
    "country",
    "tags",
    # W43: outbound-personalisation fields. Optional. If absent, the
    # Apollo enrichment task can fill them later.
    "title",
    "seniority",
    "linkedin_url",
    "phone",
    "industry",
)
_NAME_FIELDS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "company",
    "title",
    "industry",
)
_MAX_NAME_LEN = 200
DEFAULT_MAX_ROWS = 10_000


@dataclass(frozen=True)
class CsvRowError:
    row: int
    reason: str
    field: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    external_id: str  # lowercased email
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CsvParseResult:
    total_rows: int  # rows attempted (header excluded)
    valid: list[ParsedRow]
    errors: list[CsvRowError]


def parse_csv(content: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> CsvParseResult:
    """Parse + validate a CSV string. First-write-wins on duplicate emails.

    A missing `email` column is reported as a single row=0 error; the rest of
    the file is not processed (no way to identify the rows).
    """
    reader = csv.DictReader(io.StringIO(content))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    if "email" not in headers:
        return CsvParseResult(
            total_rows=0,
            valid=[],
            errors=[CsvRowError(row=0, reason="missing_required_header", field="email")],
        )

    valid: list[ParsedRow] = []
    errors: list[CsvRowError] = []
    seen_emails: set[str] = set()
    total = 0

    for row_index, raw in enumerate(reader, start=2):  # row 1 is the header
        total += 1
        if total > max_rows:
            errors.append(
                CsvRowError(
                    row=row_index,
                    reason="file_exceeds_max_rows",
                    value=str(max_rows),
                )
            )
            break

        normalized: dict[str, str] = {
            (k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None
        }

        email = normalized.get("email", "").lower()
        if not email:
            errors.append(
                CsvRowError(row=row_index, reason="missing_required_field", field="email")
            )
            continue
        if not _EMAIL_RE.match(email):
            errors.append(
                CsvRowError(
                    row=row_index, reason="invalid_email_format", field="email", value=email
                )
            )
            continue

        long_field = next(
            (f for f in _NAME_FIELDS if len(normalized.get(f, "")) > _MAX_NAME_LEN),
            None,
        )
        if long_field is not None:
            errors.append(
                CsvRowError(
                    row=row_index,
                    reason="field_too_long",
                    field=long_field,
                    value=f"max={_MAX_NAME_LEN}",
                )
            )
            continue

        country = normalized.get("country", "").upper()
        if country and not _COUNTRY_RE.match(country):
            errors.append(
                CsvRowError(
                    row=row_index,
                    reason="invalid_country_code",
                    field="country",
                    value=country,
                )
            )
            continue

        if email in seen_emails:
            errors.append(
                CsvRowError(row=row_index, reason="duplicate_in_file", field="email", value=email)
            )
            continue
        seen_emails.add(email)

        tags_raw = normalized.get("tags", "")
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        payload: dict[str, Any] = {"email": email}
        for f in _NAME_FIELDS:
            if normalized.get(f):
                payload[f] = normalized[f]
        if country:
            payload["country"] = country
        if tags:
            payload["tags"] = tags
        # W43: pick up the remaining KNOWN_FIELDS (linkedin_url, phone,
        # seniority) verbatim — no validation beyond non-empty.
        for f in ("linkedin_url", "phone", "seniority"):
            v = normalized.get(f)
            if v:
                payload[f] = v

        valid.append(ParsedRow(row_number=row_index, external_id=email, payload=payload))

    return CsvParseResult(total_rows=total, valid=valid, errors=errors)
