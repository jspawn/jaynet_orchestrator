#!/usr/bin/env python3
"""Batch PDF -> Markdown for MASS extraction. Deterministic, NO LLM, NO sub-agents.

Walks <input_dir> recursively for *.pdf and writes one markdown file per PDF (text
layer via pypdfium2, OCR fallback via rapidocr for scanned pages — the same engine as
ocr_pdf.py). Designed to run as ONE job (job.start + job.wait): it loops in-process and
loads the OCR model once for the whole run, so do NOT spawn a sub-agent per PDF.

Usage:
  pdf_batch.py <input_dir> [--out DIR] [--mirror] [--dpi N]
               [--ocr auto|always|never] [--overwrite] [--pattern '*.pdf']

Output location:
  (default)             <name>.md next to each source PDF
  --out DIR             all .md into DIR (flat); name collisions get a parent prefix
  --out DIR --mirror    recreate the source subfolder tree under DIR

Idempotent: an existing .md newer than its PDF is skipped unless --overwrite, so
re-runs are cheap. Per-file errors are reported and do not abort the batch.

Venv: pip install pypdfium2 rapidocr-onnxruntime
"""
import argparse, fnmatch, os, re, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ocr_pdf import extract_pdf


def _slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


def _target(pdf, root, out, mirror, used):
    """Where the .md for `pdf` should go; reserves the name in `used` for --flat."""
    if out is None:
        return pdf.with_suffix(".md")
    out = Path(out)
    if mirror:
        return out / pdf.relative_to(root).with_suffix(".md")
    name = pdf.stem + ".md"                       # flat: disambiguate collisions
    if name in used:
        prefix = _slug(str(pdf.parent.relative_to(root))) or "d"
        name = f"{prefix}__{pdf.stem}.md"
        k = 2
        while name in used:
            name = f"{prefix}__{pdf.stem}_{k}.md"
            k += 1
    used.add(name)
    return out / name


def run_batch(input_dir, out=None, mirror=False, dpi=220, ocr="auto",
              overwrite=False, pattern="*.pdf", log=print):
    root = Path(input_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {input_dir}")
    pdfs = sorted(p for p in root.rglob("*")
                  if p.is_file() and fnmatch.fnmatch(p.name.lower(), pattern.lower()))
    if out is not None:
        Path(out).mkdir(parents=True, exist_ok=True)

    used, failures = set(), []
    done = skipped = failed = ocr_files = ocr_page_total = 0
    for pdf in pdfs:
        dst = _target(pdf, root, out, mirror, used)
        rel = pdf.relative_to(root)
        try:
            if (not overwrite and dst.exists()
                    and dst.stat().st_mtime >= pdf.stat().st_mtime):
                skipped += 1
                continue
            md, ocr_pages = extract_pdf(str(pdf), dpi=dpi, ocr=ocr)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(f"<!-- source: {rel} -->\n\n" + md, encoding="utf-8")
            done += 1
            if ocr_pages:
                ocr_files += 1
                ocr_page_total += len(ocr_pages)
            log(f"[ok]   {rel} -> {dst.name}"
                + (f"  (OCR {len(ocr_pages)}p)" if ocr_pages else ""))
        except Exception as e:                    # report and keep going
            failed += 1
            failures.append((str(rel), f"{type(e).__name__}: {e}"))
            log(f"[FAIL] {rel}: {type(e).__name__}: {e}")

    return dict(total=len(pdfs), done=done, skipped=skipped, failed=failed,
                ocr_files=ocr_files, ocr_pages=ocr_page_total, failures=failures)


def main():
    ap = argparse.ArgumentParser(description="Batch PDF -> Markdown (no LLM, no pandoc).")
    ap.add_argument("input_dir")
    ap.add_argument("--out", default=None, help="output folder (default: beside each PDF)")
    ap.add_argument("--mirror", action="store_true", help="mirror subfolders under --out")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--ocr", choices=["auto", "always", "never"], default="auto")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--pattern", default="*.pdf")
    a = ap.parse_args()
    try:
        s = run_batch(a.input_dir, out=a.out, mirror=a.mirror, dpi=a.dpi, ocr=a.ocr,
                      overwrite=a.overwrite, pattern=a.pattern)
    except ImportError as e:
        sys.exit(f"missing dep: {e}. In a venv: pip install pypdfium2 rapidocr-onnxruntime")
    except OSError as e:
        sys.exit(str(e))

    if s["total"] == 0:
        print(f"No PDFs matched {a.pattern!r} under {a.input_dir}")
        return
    print(f"\nConverted {s['done']}/{s['total']} PDFs "
          f"({s['failed']} failed, {s['skipped']} skipped/up-to-date).")
    if s["ocr_files"]:
        print(f"OCR used on {s['ocr_files']} file(s), {s['ocr_pages']} page(s) total.")
    if s["failures"]:
        print("\nFailures:")
        for name, err in s["failures"]:
            print(f"  - {name}: {err}")
    sys.exit(1 if s["failed"] and not s["done"] else 0)


if __name__ == "__main__":
    main()
