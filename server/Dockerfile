# Versions pinned on purpose: updates are a deliberate bump + rebuild, never a surprise.
# uv as a stage of its own: Dependabot reads FROM lines
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv
FROM python:3.14.10-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
# Embedding model baked into the image (q4, 200 MB, downloaded once at build): the server never fetches anything at
# runtime (HF_HUB_OFFLINE) and works without internet. Separate layer with only vectors.py (which names the model), so
# other code changes do not re-download it.
ENV KNOWLEDGE_MODELS=/opt/models
COPY vectors.py ./
RUN mkdir -p /opt/models && /app/.venv/bin/python -c "import vectors; vectors.text_model()" \
    && chmod -R a+rX /opt/models
# All modules (a forgotten module once crash-looped the container; deploy.sh now waits for /health).
COPY *.py ./
COPY default_house_rules.md ./
COPY static ./static
# The skills that ship with the plugin (deploy.sh copies plugin/skills here): on the VM ~/.claude/skills only links to
# them, which NFS cannot follow, so the server shows this copy read-only.
COPY plugin_skills ./plugin_skills
# Runs as the uid that owns the shared files on the VM (uid 1000), so NFS root_squash is no issue.
USER 1000:1000
ENV HOME=/tmp PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1
EXPOSE 8766
# The MCP registry checks that this image belongs to the server.json name.
LABEL io.modelcontextprotocol.server.name="io.github.To0wnn/knowledge"
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8766/health',timeout=4)"
CMD ["/app/.venv/bin/python", "server.py"]
