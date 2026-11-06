# Fixture excerpt of the production Dockerfile: the Spoolman stage.
    fi

# API-only Spoolman, isolated from Print Handy's Python dependencies.
FROM debian:13-slim AS spoolman-build
ARG SPOOLMAN_REF=bed3f14ee77a3a571d31b14215d54b0b88a7519c
ENV UV_PYTHON_DOWNLOADS=0 UV_NO_DEV=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-dev build-essential libpq-dev libffi-dev git ca-certificates && \
    rm -rf /var/lib/apt/lists/* && python3 -m venv /opt/uv && \
    /opt/uv/bin/pip install --no-cache-dir uv==0.9.5
WORKDIR /opt/spoolman
RUN git init . && git remote add origin https://github.com/Donkie/Spoolman.git && \
    git fetch --depth 1 origin "$SPOOLMAN_REF" && git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = "$SPOOLMAN_REF" && \
    /opt/uv/bin/uv sync --locked --no-dev --python /usr/bin/python3 && \
    rm -rf .git client client_v2 && mkdir -p client_v2/build && \
    printf '%s\n' 'Spoolman is managed by Print Handy; use /api/v1.' > client_v2/build/200.html && \
    printf '%s\n' "$SPOOLMAN_REF" > source-revision
FROM debian:13-slim AS runtime
