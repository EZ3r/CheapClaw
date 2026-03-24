FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHEAPCLAW_USER_DATA_ROOT=/data

WORKDIR /app

COPY pyproject.toml README.md README.zh-CN.md ./
COPY __init__.py ./__init__.py
COPY assets ./assets
COPY scripts ./scripts
COPY skills ./skills
COPY tools_library ./tools_library
COPY web ./web
COPY docs ./docs
COPY cheapclaw_service.py ./cheapclaw_service.py
COPY cheapclaw_hooks.py ./cheapclaw_hooks.py
COPY tool_runtime_helpers.py ./tool_runtime_helpers.py
COPY SDK_GUIDE.md ./SDK_GUIDE.md
COPY LICENSE ./LICENSE

RUN python -m pip install --upgrade pip && python -m pip install -e .

EXPOSE 8787
VOLUME ["/data"]

CMD ["cheapclaw", "up", "--web-host", "0.0.0.0", "--web-port", "8787"]
