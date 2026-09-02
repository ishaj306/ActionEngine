# Two stages: build the wheel with a toolchain, run it without one.
#
# Tesseract is deliberately included. OCR is an optional Python extra, but the
# engine's honesty about scans depends on the binary being present -- without
# it every scanned page is refused with a reason, which is correct behaviour
# and a poor experience when a container could simply have it.

FROM python:3.12-slim AS build

WORKDIR /src
RUN python -m pip install --no-cache-dir --upgrade pip build

COPY pyproject.toml ./
COPY app ./app
RUN python -m build --wheel --outdir /dist


FROM python:3.12-slim

# tesseract-ocr: reads scanned pages. libgl1/libglib2.0-0: Pillow's runtime
# dependencies for the image handling behind it.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Never root. A document parser is exactly the process you do not want owning
# the filesystem when a malformed PDF finds something in pypdf.
RUN useradd --create-home --shell /usr/sbin/nologin engine
WORKDIR /home/engine
USER engine

ENV PATH="/home/engine/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build /dist/*.whl /tmp/wheels/
# Extras applied to the built wheel directly. All three are included because a
# container that cannot read a scan, cannot reach Postgres, or cannot run the
# model arm is a container that has to be rebuilt to change its mind.
RUN set -eux; \
    wheel="$(ls /tmp/wheels/*.whl)"; \
    python -m pip install --no-cache-dir --user "${wheel}[ocr,postgres,model]"; \
    rm -rf /tmp/wheels

EXPOSE 8000

# The health endpoint is unauthenticated on purpose, which is what makes it
# usable as a container probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
