"""
Text extraction from workflow output files.

Supports .pdf (PyMuPDF), .docx (python-docx), .xlsx (openpyxl),
.csv (csv stdlib), .html (existing html_to_text), .zip (recursive).
All imports are guarded so a missing library degrades to '' rather than
raising, consistent with the rest of the RAG subsystem.
"""
from __future__ import annotations

import base64
import csv
import io
import logging
import zipfile
from pathlib import Path

log = logging.getLogger('app.rag.file_extractor')


def extract_text_from_bytes(data: bytes,
                             mime_type: str = '',
                             file_ext: str = '') -> str:
    """Dispatch on file_ext / mime_type and return extracted plain text."""
    ext  = (file_ext or '').lower().lstrip('.')
    mime = (mime_type or '').lower()

    if ext == 'pdf' or 'pdf' in mime:
        return _pdf(data)
    if ext == 'docx' or ('openxmlformats' in mime and 'word' in mime):
        return _docx(data)
    if ext == 'xlsx' or ('openxmlformats' in mime and 'sheet' in mime):
        return _xlsx(data)
    if ext == 'csv' or mime == 'text/csv':
        return _csv(data)
    if ext in ('html', 'htm') or 'html' in mime:
        return _html(data)
    if ext == 'zip' or mime in ('application/zip', 'application/x-zip-compressed'):
        return _zip(data)
    if ext in ('txt', 'md') or mime.startswith('text/'):
        return data.decode('utf-8', errors='replace')
    try:
        return data.decode('utf-8', errors='replace')
    except Exception:
        return ''


def extract_from_base64(data_b64: str,
                        mime_type: str = '',
                        file_ext: str = '') -> str:
    """Convenience: decode base64 then delegate to extract_text_from_bytes."""
    if not data_b64:
        return ''
    try:
        raw = base64.b64decode(data_b64)
    except Exception as exc:
        log.warning('[EXTRACTOR] base64 decode failed: %s', exc)
        return ''
    return extract_text_from_bytes(raw, mime_type=mime_type, file_ext=file_ext)


# ── format handlers ──────────────────────────────────────────────────

def _pdf(data: bytes) -> str:
    try:
        import pymupdf                                      # PyMuPDF >= 1.24
        doc = pymupdf.open(stream=data, filetype='pdf')
        return '\n\n'.join(page.get_text() for page in doc).strip()
    except ImportError:
        pass
    try:
        import fitz                                         # older PyMuPDF
        doc = fitz.open(stream=data, filetype='pdf')
        return '\n\n'.join(page.get_text() for page in doc).strip()
    except ImportError:
        pass
    log.warning('[EXTRACTOR] PDF extraction requires PyMuPDF')
    return ''


def _docx(data: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(data))
        paras = [p.text for p in doc.paragraphs if p.text.strip()]
        return '\n\n'.join(paras)
    except ImportError:
        log.warning('[EXTRACTOR] DOCX extraction requires python-docx')
        return ''


def _xlsx(data: bytes) -> str:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                row_text = '\t'.join('' if v is None else str(v) for v in row)
                if row_text.strip():
                    rows.append(row_text)
            if rows:
                parts.append(f'Sheet: {ws.title}\n' + '\n'.join(rows))
        return '\n\n'.join(parts)
    except ImportError:
        log.warning('[EXTRACTOR] XLSX extraction requires openpyxl')
        return ''


def _csv(data: bytes) -> str:
    try:
        text = data.decode('utf-8', errors='replace')
        reader = csv.reader(io.StringIO(text))
        return '\n'.join('\t'.join(row) for row in reader)
    except Exception as exc:
        log.warning('[EXTRACTOR] CSV extraction failed: %s', exc)
        return ''


def _html(data: bytes) -> str:
    from app.services.rag.chunking import html_to_text
    return html_to_text(data.decode('utf-8', errors='replace'))


def _zip(data: bytes) -> str:
    parts = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                if name.endswith('/'):
                    continue
                ext = Path(name).suffix.lower().lstrip('.')
                try:
                    entry_bytes = zf.read(name)
                    text = extract_text_from_bytes(entry_bytes, file_ext=ext)
                    if text.strip():
                        parts.append(f'--- {name} ---\n{text}')
                except Exception as exc:
                    log.debug('[EXTRACTOR] ZIP entry %s skipped: %s', name, exc)
    except Exception as exc:
        log.warning('[EXTRACTOR] ZIP extraction failed: %s', exc)
    return '\n\n'.join(parts)
