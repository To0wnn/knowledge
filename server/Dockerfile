# Versions pinned on purpose: updates are a deliberate bump + rebuild, never a surprise.
FROM python:3.13.16-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY server.py ./
# Runs as the uid that owns the shared files on the VM (uid 1000), so NFS root_squash is no issue.
USER 1000:1000
ENV HOME=/tmp PYTHONUNBUFFERED=1
EXPOSE 8766
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8766/health',timeout=4)"
CMD ["/app/.venv/bin/python", "server.py"]
