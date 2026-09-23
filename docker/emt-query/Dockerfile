FROM python:3.10-slim-bookworm@sha256:68d914ec641a0b69267ce65184d000a2bc3a9ee2590ab702b82250ab2385735a

# The base already includes libstdc++6. The build helper downloads Debian's
# small terminal-information package outside the emulated build environment.
COPY lshw.deb /tmp/lshw.deb
RUN echo '6e2481ebc0a740e7cf1f25d39b8e44a97d10fed33b3eeb271a7a57af594669d7  /tmp/lshw.deb' | sha256sum -c - \
    && dpkg -i /tmp/lshw.deb \
    && rm /tmp/lshw.deb
ENV PYTHONPATH=/sdk \
    LD_LIBRARY_PATH=/sdk \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /work
# SDK and query script are mounted read-only at runtime; no credentials in image.
CMD ["python", "/app/probe.py", "--container", "--check"]
