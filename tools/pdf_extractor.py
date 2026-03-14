"""
PDF blood report extractor.
Uses pdfplumber to extract raw text from uploaded PDF lab reports.
The raw text is then passed to VisionAgent for structured LLM parsing.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_blood_report(pdf_path: str) -> str | None:
    """
    Extract all text from a PDF blood test report.
    Returns raw text for LLM parsing, or None on failure.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber not installed. Run: pip install pdfplumber")
        return None

    try:
        path = Path(pdf_path)
        if not path.exists():
            logger.warning("PDF not found: %s", pdf_path)
            return None

        text_parts = []
        with pdfplumber.open(path) as pdf:
            for page_num, page in enumerate(pdf.pages[:5]):   # Max 5 pages
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(f"--- Page {page_num + 1} ---\n{page_text}")

                # Also try to extract tables (common in lab reports)
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if any(cell for cell in row if cell):
                            text_parts.append(" | ".join(str(cell or "") for cell in row))

        return "\n".join(text_parts) if text_parts else None

    except Exception as exc:
        logger.error("PDF extraction failed for %s: %s", pdf_path, exc)
        return None
