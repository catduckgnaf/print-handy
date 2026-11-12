# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# OrcaSlicer is never built from source: the one bundled build installs the pinned official
# prebuilt release (docker/orca-prebuilt.sh, SHA256 verified before extraction).
# See docs/DOCKER-117-PROGRESS.md for contributor attribution.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS orca-prebuilt
ARG WITH_ORCA=false
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive
COPY docker/orca-prebuilt.sh /usr/local/bin/orca-prebuilt
RUN mkdir -p /out && \
    case "$WITH_ORCA" in true|false) ;; *) echo 'WITH_ORCA must be true or false' >&2; exit 2;; esac && \
    if [ "$WITH_ORCA" = true ]; then \
      apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && \
      sh /usr/local/bin/orca-prebuilt stable "$TARGETARCH" && \
      rm -rf /var/lib/apt/lists/*; \
    fi

# go2rtc (#246 stage 1) is a pinned official prebuilt too: one static binary
# per architecture, SHA256 verified before installation
# (docker/go2rtc-prebuilt.sh). WITH_GO2RTC=false produces an empty /out, so
# the runtime COPY below is a no-op there.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS go2rtc-prebuilt
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

FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS bambu-build
ARG WITH_BAMBU=false
ARG BAMBU_REF=56a57d00a591360cf41bf21383915ee5bf357be5
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
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS spoolman-build
ARG SPOOLMAN_REF=ff8efd517b76a4b2783f29c89e9b9fbcd8cd167b
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
    printf '%s\n' \
        '<!DOCTYPE html>' \
        '<html lang="en">' \
        '<head>' \
        '<meta charset="utf-8">' \
        '<title>Spoolman is managed by Print Handy</title>' \
        '</head>' \
        '<body>' \
        '<h1>Spoolman is managed by Print Handy</h1>' \
        '<p>This address serves the bundled Spoolman API only. The bundled Spoolman has no web UI and is API-only; Print Handy is its interface.</p>' \
        '<p>The Spoolman API is available under <a href="/api/v1">/api/v1</a>.</p>' \
        '<p>To view and manage spools, open the Print Handy Web UI Spools page: <a id="spools-link" href="#">Web UI Spools page</a>.</p>' \
        '<p id="spools-url">The Web UI Spools page is on the same host as this page, on the Web UI port (default 8088), at the path /spools: use the same protocol and host as this page, with port 8088 and path /spools (for example http://HOST:8088/spools, replacing HOST with the host used for this page).</p>' \
        '<p>The Web UI must be enabled for that link to work, and it may use a different port; if it does, use that port instead of 8088.</p>' \
        '<script>' \
        'var spoolsUrl = location.protocol + "//" + location.hostname + ":8088/spools";' \
        'var spoolsLink = document.getElementById("spools-link");' \
        'spoolsLink.href = spoolsUrl;' \
        'spoolsLink.textContent = spoolsUrl;' \
        'document.getElementById("spools-url").textContent = "Web UI Spools page: " + spoolsUrl;' \
        '</script>' \
        '</body>' \
        '</html>' > client_v2/build/200.html && \
    printf '%s\n' "$SPOOLMAN_REF" > source-revision
# Native G-code bounds parser (#952, D1): the Rust/PyO3 crate in
# native/bounds, built as an abi3-py312 wheel so one wheel per arch
# serves the runtime's Python 3.13. Only the built wheel leaves this
# stage; no Rust toolchain reaches the runtime image. The Python parser
# remains the permanent fallback wherever the extension is absent, so
# this stage only accelerates Print Handy in the image; it is never
# load bearing for correctness.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS bounds-build
ENV DEBIAN_FRONTEND=noninteractive CARGO_HOME=/opt/cargo RUSTUP_HOME=/opt/rustup \
    PATH=/opt/cargo/bin:/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv build-essential curl ca-certificates && \
    rm -rf /var/lib/apt/lists/* && python3 -m venv /opt/venv && \
    pip install --no-cache-dir "maturin==1.16.0" && \
    case "$(uname -m)" in \
      x86_64) target=x86_64-unknown-linux-gnu; sum=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c ;; \
      aarch64) target=aarch64-unknown-linux-gnu; sum=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c ;; \
      *) echo "unsupported architecture for the bounds build: $(uname -m)" >&2; exit 1 ;; \
    esac && \
    curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/rustup-init \
      "https://static.rust-lang.org/rustup/archive/1.28.2/$target/rustup-init" && \
    echo "$sum  /tmp/rustup-init" | sha256sum -c - && \
    chmod +x /tmp/rustup-init && \
    /tmp/rustup-init -y --profile minimal --default-toolchain 1.99.1 && rm /tmp/rustup-init
WORKDIR /src
COPY native/bounds/ ./native/bounds/
# maturin places the extension at the module path only when the target
# package's directories exist beside the crate (mixed layout), so the
# real package __init__ files are staged into the crate first; the
# wheel then carries print_handy/core/_bounds_native.abi3.so.
COPY print_handy/__init__.py ./native/bounds/print_handy/__init__.py
COPY print_handy/core/__init__.py ./native/bounds/print_handy/core/__init__.py
COPY README.md ./README.md
RUN mkdir -p /out && cd native/bounds && \
    maturin build --release --locked -i python3 --out /out

FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS runtime
ARG WITH_GO2RTC=true
ARG ORT_FLAVOR=cpu
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH \
    PRINT_HANDY_IN_CONTAINER=1
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
# both architectures. libmfx-gen1.2 (also amd64-only) is the oneVPL GPU
# runtime h264_qsv opens at run time (#455): Debian's ffmpeg links libvpl
# on amd64 only, and without this runtime QSV was listed but never opened.
RUN case "$WITH_GO2RTC" in true|false) ;; *) echo 'WITH_GO2RTC must be true or false' >&2; exit 2;; esac && \
    if [ "$WITH_GO2RTC" = true ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        mesa-va-drivers libva-drm2 vainfo && \
      if [ "$TARGETARCH" = amd64 ]; then \
        apt-get install -y --no-install-recommends intel-media-va-driver libmfx-gen1.2; \
      fi && \
      rm -rf /var/lib/apt/lists/*; \
    fi
# NVENC (#455): the NVIDIA container runtime mounts the driver libraries
# these capabilities name. Its default (compute,utility) omits "video", so
# the NVENC encode library was never mounted and h264_nvenc could not open.
# Inert without --runtime=nvidia / --gpus; no GPU is selected here — the
# operator still picks one with NVIDIA_VISIBLE_DEVICES.
ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
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
LABEL org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="https://github.com/catduckgnaf/print-handy" \
      org.opencontainers.image.description="Print Handy: local-first chat-guided slicing, spool matching and confirmed printing for 3D printers"
WORKDIR /app
COPY pyproject.toml README.md ./
COPY print_handy/__init__.py ./print_handy/__init__.py
RUN pip install --no-cache-dir .
# GPU inference flavors for the local failure detector (#455): the
# install above brings the CPU build of ONNX Runtime (pyproject.toml),
# which is what the published image ships. ORT_FLAVOR swaps that
# distribution for an accelerated one, and the detector's provider
# selection then picks the GPU provider automatically when the host
# offers one (#576); "cpu" (the default) changes nothing at all. Flavor
# pins name the newest build published for each flavor — the OpenVINO
# flavor's newest (1.24.1) trails the CPU/GPU line (1.31.1), and
# onnxruntime-openvino publishes x86_64 wheels only, so that flavor
# refuses other architectures here instead of failing obscurely in pip.
# The cuda flavor carries its CUDA/cuDNN user-space libraries via the
# package extras; the host driver still comes from the NVIDIA runtime.
# Each flavor proves itself at build time by the provider bridge library
# it must ship. GPU inference itself is not verified on real hardware
# (see docs/DOCKER.md, "GPU and the local failure detector").
RUN case "$ORT_FLAVOR" in \
      cpu) ;; \
      openvino) \
        if [ "$TARGETARCH" != "amd64" ]; then \
          echo 'ORT_FLAVOR=openvino is amd64-only: onnxruntime-openvino publishes no arm64 wheel' >&2; exit 2; \
        fi && \
        pip uninstall -y onnxruntime && \
        pip install --no-cache-dir "onnxruntime-openvino==1.24.1" && \
        python -c "import onnxruntime, pathlib; capi = pathlib.Path(onnxruntime.__file__).parent / 'capi'; assert (capi / 'libonnxruntime_providers_openvino.so').is_file(), 'OpenVINO provider bridge missing'" ;; \
      cuda) \
        pip uninstall -y onnxruntime && \
        pip install --no-cache-dir "onnxruntime-gpu[cuda,cudnn]==1.31.1" && \
        python -c "import onnxruntime, pathlib; capi = pathlib.Path(onnxruntime.__file__).parent / 'capi'; assert (capi / 'libonnxruntime_providers_cuda.so').is_file(), 'CUDA provider bridge missing'" ;; \
      *) echo 'ORT_FLAVOR must be cpu, openvino or cuda' >&2; exit 2 ;; \
    esac
# The native bounds wheel (#952) installs the compiled extension into
# the venv's site-packages; only the .so ships, no build toolchain.
COPY --from=bounds-build /out/*.whl /tmp/bounds-wheel/
COPY . .
# The license, the notice and the third-party components table travel with the image (docs/ is otherwise
# dockerignored; .dockerignore re-includes only docs/COMPONENTS.md).
COPY LICENSE NOTICE docs/COMPONENTS.md /usr/share/doc/print-handy/
# Only the compiled extension leaves the wheel: the wheel also carries the
# package skeleton maturin needed (print_handy/__init__.py and
# core/__init__.py), which must never overwrite the installed package's
# own files. The app imports print_handy from both site-packages and /app
# (the working copy), so the one .so goes to both. The site-packages
# directory has to be created first: the pip install above runs against
# a staged skeleton (pyproject.toml + print_handy/__init__.py only, for
# dependency-layer caching), so the installed package has no core/
# directory, and cp will not create it — that is the failure the develop
# publish run 42410316669 hit on both architectures. The closing import
# proves the /app copy loads under the runtime interpreter before the
# stage completes; the dispatcher's version handshake and golden
# self-check still gate its use at runtime.
RUN set -e; cd /tmp/bounds-wheel && python -m zipfile -e *.whl x && \
    so=$(ls x/print_handy/core/_bounds_native*.so) && \
    site=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') && \
    mkdir -p "$site/print_handy/core" && \
    cp "$so" "$site/print_handy/core/" && cp "$so" /app/print_handy/core/ && \
    cd /app && python -c "from print_handy.core import _bounds_native" && \
    cd / && rm -rf /tmp/bounds-wheel
# Bytecode for the working copy (#1153). .dockerignore keeps every
# __pycache__ out of the context, /app is root-owned and the processes run
# as handy, so without this step nothing can write bytecode and every bot
# start and every heavy-worker spawn compiles model_job, prepared_intake
# and the plugin from source again (about 0.4 s of a bot start and 0.75 s
# of each worker spawn's imports, measured). COPY keeps the source mtimes
# the .pyc files record, so they stay valid; a failed compile fails the
# build rather than shipping a module that cannot import.
RUN python -m compileall -q -j 0 /app/print_handy /app/plugins
ENV TZ=America/New_York
ENV PRINT_HANDY_ORCA_CMD=/usr/local/bin/orca-slicer-stable
ENV PRINT_HANDY_CONFIG=/app/config/docker.yaml HOME=/data PRINT_HANDY__BOT__MODE=standalone \
    PRINT_HANDY_ORCA_CHANNEL=stable PRINT_HANDY__SERVICES__SPOOLMAN_URL=http://127.0.0.1:8000 \
    SPOOLMAN_DIR_DATA=/data/spoolman SPOOLMAN_DIR_CONFIG=/data/spoolman/config \
    SPOOLMAN_DIR_LOGS=/data/spoolman/logs SPOOLMAN_DIR_BACKUPS=/data/spoolman/backups \
    SPOOLMAN_HOST=127.0.0.1 SPOOLMAN_PORT=8000
# /etc/localtime matches the default zone for consumers that ignore the TZ
# variable; glibc, Python and Go consumers honor a runtime TZ override
# directly, and the entrypoint validates the name against this database.
RUN ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime && printf '%s\n' "$TZ" > /etc/timezone
# /data/state is the state volume's neutral name since 2.2.5 (#914); /data/hermes
# is its pre-2.2.5 name and stays accepted (the entrypoint links /data/state to it
# when only the legacy folder holds data; removal is at least two minor releases out).
# The image does not create /data/hermes (#1144): a pre-created folder made the
# entrypoint's compat link to /data/state unreachable. The folder exists only where an
# install maps it; everywhere else the entrypoint links it to /data/state.
# There is deliberately no VOLUME declaration: Docker creates an anonymous volume
# for every declared path that a run does not mount exactly, so a template that
# binds only /data would send state, records and the Spoolman database to hidden
# volumes instead of the host folder. Compose and the Unraid templates mount their
# paths explicitly, and the directories below exist and are owned by handy either way.
EXPOSE 8000 8088
COPY docker/supervise.sh /usr/local/bin/print-handy-supervise
COPY docker/healthcheck.py /usr/local/bin/print-handy-healthcheck
RUN mkdir -p /data/state/state /data/state/logs /data/archive /data/records /data/cache /data/spoolman/config /data/spoolman/logs /data/spoolman/backups && \
    chown -R handy:handy /data && chmod +x /app/docker/entrypoint.sh /usr/local/bin/print-handy-supervise
USER handy
# Image identity for the diagnostics bundle (#1332): a process cannot read its
# own image's OCI labels, so the publish, develop and nightly workflows also pass
# the tag and commit as build args. Local builds leave them empty, and the bundle
# then says "not recorded". Declared last so a new commit rebuilds no earlier layer.
ARG IMAGE_TAG=
ARG IMAGE_SHA=
ENV PRINT_HANDY_IMAGE_TAG=$IMAGE_TAG PRINT_HANDY_IMAGE_SHA=$IMAGE_SHA
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/docker/entrypoint.sh", "/usr/local/bin/print-handy-supervise"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 CMD ["python3", "/usr/local/bin/print-handy-healthcheck"]
CMD []
