# syntax=docker/dockerfile:1
# OrcaSlicer is never built from source: both channels install pinned official
# prebuilt releases (docker/orca-prebuilt.sh, SHA256 verified before extraction).
# See docs/DOCKER-117-PROGRESS.md for contributor attribution.
FROM debian:13-slim AS orca-prebuilt
ARG WITH_ORCA=false
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive
COPY docker/orca-prebuilt.sh /usr/local/bin/orca-prebuilt
RUN mkdir -p /out && \
    case "$WITH_ORCA" in true|false) ;; *) echo 'WITH_ORCA must be true or false' >&2; exit 2;; esac && \
    if [ "$WITH_ORCA" = true ]; then \
      apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && \
      sh /usr/local/bin/orca-prebuilt stable "$TARGETARCH" && \
      sh /usr/local/bin/orca-prebuilt preview "$TARGETARCH" && \
      rm -rf /var/lib/apt/lists/*; \
    fi

# go2rtc (#246 stage 1) is a pinned official prebuilt too: one static binary
# per architecture, SHA256 verified before installation
# (docker/go2rtc-prebuilt.sh). WITH_GO2RTC=false produces an empty /out, so
# the runtime COPY below is a no-op there.
FROM debian:13-slim AS go2rtc-prebuilt
ARG WITH_GO2RTC=true
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive
COPY docker/go2rtc-prebuilt.sh /usr/local/bin/go2rtc-prebuilt
RUN mkdir -p /out && \
    case "$WITH_GO2RTC" in true|false) ;; *) echo 'WITH_GO2RTC must be true or false' >&2; exit 2;; esac && \
    if [ "$WITH_GO2RTC" = true ]; then \
      apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && \
      sh /usr/local/bin/go2rtc-prebuilt "$TARGETARCH" && \
      rm -rf /var/lib/apt/lists/*; \
    fi

FROM debian:13-slim AS bambu-build
ARG WITH_BAMBU=false
ARG BAMBU_REF=926a7192574bcb9b3a732e1ec59a46d79cb45466
ARG BAMBU_BUILD_PROFILE=release-static-fhs
ARG ORCA_BUILD_JOBS=auto
ARG ORCA_BUILD_MEM_PER_JOB_MIB=2560
ARG ORCA_CCACHE_MAXSIZE=5G
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive
COPY docker/orca-build-jobs.sh /usr/local/bin/orca-build-jobs
RUN mkdir -p /out && chmod +x /usr/local/bin/orca-build-jobs && \
    case "$WITH_BAMBU" in true|false) ;; *) echo 'WITH_BAMBU must be true or false' >&2; exit 2;; esac
RUN --mount=type=cache,id=print-handy-bambu-${TARGETARCH}-${BAMBU_REF}-${BAMBU_BUILD_PROFILE},target=/cache,sharing=locked \
    if [ "$WITH_BAMBU" = true ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git cmake ninja-build build-essential autoconf automake \
        libtool pkg-config gettext texinfo curl wget file m4 ccache nasm yasm \
        eglexternalplatform-dev extra-cmake-modules libcurl4-openssl-dev \
        libdbus-1-dev libglew-dev libgstreamerd-3-dev libgtk-3-dev libosmesa6-dev \
        libgl1-mesa-dev libxkbcommon-dev libunwind-dev libx264-dev libbz2-dev \
        libmspack-dev libsecret-1-dev libssl-dev libudev-dev \
        libwebkit2gtk-4.1-dev gstreamer1.0-gtk3 && \
      ref="$BAMBU_REF" && \
      jobs="$(orca-build-jobs "$ORCA_BUILD_JOBS")" && \
      export CMAKE_BUILD_PARALLEL_LEVEL="$jobs" CC=/usr/lib/ccache/gcc CXX=/usr/lib/ccache/g++ \
        CCACHE_DIR=/cache/ccache CCACHE_MAXSIZE="$ORCA_CCACHE_MAXSIZE" && \
      toolchain="$(. /etc/os-release && printf '%s\n' "$ID-$VERSION_ID" "$(gcc -dumpfullversion)" \
        "$(cmake --version | head -n 1)" "$CC" "$CXX" "$BAMBU_BUILD_PROFILE" | sha256sum | cut -c1-16)" && \
      if [ "$(cat /cache/toolchain 2>/dev/null)" != "$toolchain" ]; then \
        rm -rf /cache/bambu/deps/build /cache/bambu/build && printf '%s\n' "$toolchain" > /cache/toolchain; \
      fi && \
      if [ "$(git -C /cache/bambu rev-parse HEAD 2>/dev/null)" != "$ref" ]; then \
        rm -rf /cache/bambu && git init /cache/bambu && \
        git -C /cache/bambu remote add origin https://github.com/bambulab/BambuStudio.git && \
        git -C /cache/bambu fetch --depth 1 origin "$ref" && git -C /cache/bambu checkout --detach FETCH_HEAD; \
      fi && \
      cd /cache/bambu && \
      test "$(git rev-parse HEAD)" = "$ref" && \
      test -z "$(git status --porcelain --untracked-files=no)" && \
      cmake -S deps -B deps/build -G Ninja -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DDEP_WX_GTK3=ON && \
      cmake --build deps/build --parallel 1 && \
      cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -DCMAKE_PREFIX_PATH="$PWD/deps/build/destdir/usr/local" \
        -DSLIC3R_STATIC=1 -DSLIC3R_GTK=3 -DSLIC3R_FHS=ON -DSLIC3R_PCH=OFF \
        -DBBL_RELEASE_TO_PUBLIC=1 -DBBL_INTERNAL_TESTING=0 -DCMAKE_INSTALL_PREFIX=/usr && \
      cmake --build build --parallel "$jobs" --target BambuStudio && \
      DESTDIR=/out cmake --install build && \
      test -x /out/usr/bin/bambu-studio && \
      printf '%s\n' "$ref" > /out/bambu-source-revision && \
      printf 'revision=%s\narch=%s\nprofile=%s\ntoolchain=%s\njobs=%s\n' \
        "$ref" "$TARGETARCH" "$BAMBU_BUILD_PROFILE" "$toolchain" "$jobs" > /out/bambu-build-info && \
      ccache --show-stats; \
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
ARG WITH_GO2RTC=true
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ca-certificates curl jq ffmpeg xvfb xauth tini tzdata openscad tesseract-ocr \
    libwebkit2gtk-4.1-0 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
    libgtk-3-0t64 libglew2.2 libsecret-1-0 libspnav0 libgstreamerd-3-0 \
    libosmesa6 libunwind8 libx264-164 libopengl0 libglu1-mesa libgl1 libegl1 \
    fonts-liberation && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv && useradd --uid 1000 --create-home handy
COPY --from=spoolman-build /opt/spoolman /opt/spoolman
COPY --from=orca-prebuilt /out/ /
COPY --from=go2rtc-prebuilt /out/ /
# VAAPI is the hardware-encoder baseline for in-container timelapse work
# (#246): Intel iGPU and AMD via /dev/dri when the operator grants it. It
# ships under the same WITH_GO2RTC toggle as the go2rtc binary; a
# WITH_GO2RTC=false build carries neither. No CUDA toolkit is baked
# in: NVENC/QSV activate only when the host runtime provides the device.
# intel-media-va-driver is amd64-only (no arm64 candidate in Debian trixie),
# so it installs behind a TARGETARCH check; the rest of the stack ships on
# both architectures.
RUN case "$WITH_GO2RTC" in true|false) ;; *) echo 'WITH_GO2RTC must be true or false' >&2; exit 2;; esac && \
    if [ "$WITH_GO2RTC" = true ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        mesa-va-drivers libva-drm2 vainfo && \
      if [ "$TARGETARCH" = amd64 ]; then \
        apt-get install -y --no-install-recommends intel-media-va-driver; \
      fi && \
      rm -rf /var/lib/apt/lists/*; \
    fi
# No global LD_LIBRARY_PATH into Orca's bundled runtime: lib/orca-runtime
# carries FFmpeg 7.0 libav* builds whose sonames shadow the system ffmpeg
# 7.1.5 libraries and kill it before it prints anything. The path is scoped
# to Orca invocations instead: upstream's AppRun env helper
# (libexec/orca-slicer-env) sets it for every wrapped launch, and the
# raw-binary checks (tests/docker/image_probe.py, docker/slicer-smoke.py)
# set it per invocation.
COPY --from=bambu-build /out/ /
RUN if [ -x /usr/local/bin/orca-slicer-stable ]; then \
      ln -s /opt/orca-stable/bin/orca-slicer /usr/bin/orca-slicer && \
      ln -s /orca-stable-build-info /orca-build-info && \
      ln -s /orca-stable-source-revision /orca-source-revision; \
    fi
ARG APP_CHANNEL=development
RUN case "$APP_CHANNEL" in stable|development) ;; *) echo 'APP_CHANNEL must be stable or development' >&2; exit 2;; esac
LABEL org.print-handy.app-channel=$APP_CHANNEL
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TZ=America/New_York
ENV PRINT_HANDY_CONFIG=/app/config/docker.yaml HOME=/data PRINT_HANDY__BOT__MODE=standalone \
    PRINT_HANDY_ORCA_CHANNEL=stable PRINT_HANDY__SERVICES__SPOOLMAN_URL=http://127.0.0.1:8000 \
    SPOOLMAN_DIR_DATA=/data/spoolman SPOOLMAN_DIR_CONFIG=/data/spoolman/config \
    SPOOLMAN_DIR_LOGS=/data/spoolman/logs SPOOLMAN_DIR_BACKUPS=/data/spoolman/backups \
    SPOOLMAN_HOST=127.0.0.1 SPOOLMAN_PORT=8000
# /etc/localtime matches the default zone for consumers that ignore the TZ
# variable; glibc, Python and Go consumers honor a runtime TZ override
# directly, and the entrypoint validates the name against this database.
RUN ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime && printf '%s\n' "$TZ" > /etc/timezone
VOLUME ["/data/hermes", "/data/archive", "/data/records", "/data/cache", "/data/spoolman"]
EXPOSE 8000 8088
COPY docker/supervise.sh /usr/local/bin/print-handy-supervise
COPY docker/healthcheck.py /usr/local/bin/print-handy-healthcheck
RUN mkdir -p /data/hermes/state /data/archive /data/records /data/cache /data/spoolman/config /data/spoolman/logs /data/spoolman/backups && \
    chown -R handy:handy /data && chmod +x /app/docker/entrypoint.sh /usr/local/bin/print-handy-supervise
USER handy
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/docker/entrypoint.sh", "/usr/local/bin/print-handy-supervise"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 CMD ["python3", "/usr/local/bin/print-handy-healthcheck"]
CMD []
