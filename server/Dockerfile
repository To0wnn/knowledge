# Versions pinned on purpose: updates are a deliberate bump + rebuild, never a surprise.
FROM python:3.13.16-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
# Embedding model baked into the image (1.2 GB, downloaded once at build): the server never fetches anything at
# runtime (HF_HUB_OFFLINE) and works without internet. Separate layer, so code changes do not re-download it.
ENV KNOWLEDGE_MODELS=/opt/models
RUN mkdir -p /opt/models && /app/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('google/embeddinggemma-300m', cache_dir='/opt/models')" \
    && chmod -R a+rX /opt/models
# All modules (a forgotten module once crash-looped the container; deploy.sh now waits for /health).
COPY *.py ./
COPY static ./static
# The skills that ship with the plugin (deploy.sh copies plugin/skills here): on the VM ~/.claude/skills only links to
# them, which NFS cannot follow, so the server shows this copy read-only.
COPY plugin_skills ./plugin_skills
# Runs as the uid that owns the shared files on the VM (uid 1000), so NFS root_squash is no issue.
USER 1000:1000
ENV HOME=/tmp PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1
EXPOSE 8766
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8766/health',timeout=4)"
CMD ["/app/.venv/bin/python", "server.py"]
