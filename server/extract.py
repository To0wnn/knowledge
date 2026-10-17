"""Turn a fetched or uploaded document into markdown. Runs as a separate process with CPU and memory limits
(see run()), because parsers on hostile files can hang or blow up memory.

  python extract.py <content-type> [url]  < document bytes   ->  {"title": ..., "markdown": ...} on stdout
"""
import io
import json
import resource
import subprocess
import sys

MAX_PDF_PAGES = 300
CPU_SECONDS = 20
MEMORY_BYTES = 768 * 1024 * 1024


def extract(data: bytes, ctype: str, url: str = "") -> dict:
    if ctype == "application/pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        title = (reader.metadata.title if reader.metadata else "") or ""
        pages = [p.extract_text() or "" for p in reader.pages[:MAX_PDF_PAGES]]
        note = f"\n\n[... {len(reader.pages) - MAX_PDF_PAGES} more pages not extracted]" if len(reader.pages) > MAX_PDF_PAGES else ""
        return {"title": title, "markdown": "\n\n".join(f"<!-- page {i} -->\n{t.strip()}" for i, t in enumerate(pages, 1)) + note}
    if ctype.endswith("wordprocessingml.document"):
        import docx
        d = docx.Document(io.BytesIO(data))
        return {"title": d.core_properties.title or "", "markdown": "\n\n".join(p.text for p in d.paragraphs if p.text.strip())}
    text = data.decode("utf-8", errors="replace")
    if ctype in ("text/html", "application/xhtml+xml"):
        import trafilatura
        meta = trafilatura.extract_metadata(text, default_url=url or None)
        md = trafilatura.extract(text, url=url or None, output_format="markdown", include_links=True,
                                 include_tables=True, favor_recall=True) or ""
        return {"title": (meta.title if meta else "") or "", "markdown": md}
    return {"title": "", "markdown": text}  # text/plain, text/markdown


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))


def run(data: bytes, ctype: str, url: str = "") -> dict:
    """Call extract() in a child process under rlimits; raises RuntimeError with a short reason on failure."""
    try:
        out = subprocess.run([sys.executable, __file__, ctype, url], input=data, capture_output=True,
                             timeout=CPU_SECONDS + 10, preexec_fn=_limits)
    except subprocess.TimeoutExpired:
        raise RuntimeError("extraction took too long")
    if out.returncode != 0:
        raise RuntimeError(f"could not extract text: {out.stderr.decode(errors='replace').strip().splitlines()[-1:]}")
    return json.loads(out.stdout)


if __name__ == "__main__":
    print(json.dumps(extract(sys.stdin.buffer.read(), sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")))
