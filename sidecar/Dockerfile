# Media2SIP sidecar: pjsua2 SIP user agent + control API.
#
# Stage 1 builds pjproject with its SWIG Python bindings; stage 2 keeps only the
# shared libraries and the pjsua2 module, so the runtime image carries no compiler.
ARG PJ_VERSION=2.15.1
ARG PY_VERSION=3.12

FROM python:${PY_VERSION}-slim-bookworm AS pjbuild
ARG PJ_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential swig python3-dev pkg-config curl ca-certificates \
        libasound2-dev libssl-dev uuid-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl -fsSL "https://github.com/pjsip/pjproject/archive/refs/tags/${PJ_VERSION}.tar.gz" \
      | tar xz --strip-components=1

# No video, no WebRTC AEC (fails on some arches and buys nothing for one-way paging).
# ALSA stays available so pjmedia has a real audio driver to fall back on; the app
# selects the null device at runtime, since a container has no sound card.
RUN ./configure \
        --enable-shared \
        --disable-video \
        --disable-libwebrtc \
        --disable-libyuv \
        --disable-opencore-amr \
        --disable-speex-codec \
        --disable-speex-aec \
        --disable-ilbc-codec \
        --disable-g7221-codec \
        CFLAGS="-O2 -DNDEBUG -fPIC" CXXFLAGS="-O2 -DNDEBUG -fPIC" \
    && make dep && make -j"$(nproc)" && make install && ldconfig

# pjproject's SWIG setup.py still imports distutils, which Python 3.12 dropped;
# setuptools ships the shim. Its own layer, so the compile above stays cached.
RUN pip install --no-cache-dir "setuptools>=69,<81"

# `make` (not `make install`) — the default target runs SWIG to generate
# pjsua2_wrap.cpp and then builds _pjsua2.so. The bundled `install` target calls
# the long-removed `setup.py install --user`, so the artifacts are placed by hand.
RUN make -C pjsip-apps/src/swig/python

# Collect everything the runtime needs into one tree we can COPY wholesale.
RUN set -eux; \
    mkdir -p /out/lib /out/site; \
    cp -a /usr/local/lib/lib*.so* /out/lib/; \
    cp pjsip-apps/src/swig/python/pjsua2.py /out/site/; \
    find pjsip-apps/src/swig/python/build -name '_pjsua2*.so' -exec cp {} /out/site/ \; ; \
    test -f /out/site/pjsua2.py; \
    ls /out/site/_pjsua2*.so; \
    PYTHONPATH=/out/site LD_LIBRARY_PATH=/usr/local/lib python3 -c \
      "import pjsua2, sys; e=pjsua2.Endpoint(); e.libCreate(); print('pjsua2 built:', e.libVersion().full); e.libDestroy()"


FROM python:${PY_VERSION}-slim-bookworm AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libasound2 libssl3 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pjbuild /out/lib/ /usr/local/lib/
RUN ldconfig
COPY --from=pjbuild /out/site/ /usr/local/lib/python3.12/site-packages/
RUN python -c "import pjsua2; print('pjsua2 ok')"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
# Built-in sounds live outside /data on purpose: a Home Assistant add-on gets
# /data mounted as its persistent volume, which would shadow anything baked in
# there. /data/sounds stays available for the operator's own clips.
COPY sounds/ /opt/media2sip/sounds/

# Stamped by the release workflow; "dev" for any other build, which is the
# truth about a build nobody released.
ARG VERSION=dev
ENV APP_VERSION=${VERSION}

ENV PYTHONUNBUFFERED=1 \
    SIDECAR_PORT=8080 \
    CACHE_DIR=/data/cache \
    SOUNDS_DIR=/data/sounds \
    BUILTIN_SOUNDS_DIR=/opt/media2sip/sounds
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS "http://127.0.0.1:${SIDECAR_PORT}/health" || exit 1
CMD ["python", "-m", "app.main"]
