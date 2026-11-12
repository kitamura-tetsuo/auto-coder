FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates cloud-image-utils curl python3 qemu-system-x86 qemu-utils \
    && rm -rf /var/lib/apt/lists/*
ARG CLOUD_IMAGE_URL=https://cloud-images.ubuntu.com/minimal/releases/noble/release-20261105/ubuntu-24.04-minimal-cloudimg-amd64.img
ARG CLOUD_IMAGE_SHA256=406630c08b5e545fcc7209854b129d47e3af822db128c9f19336593b5a2d3fe0
RUN curl --fail --location --retry 3 "$CLOUD_IMAGE_URL" -o /opt/base.img \
    && echo "$CLOUD_IMAGE_SHA256  /opt/base.img" | sha256sum --check --strict
COPY runner.sh /opt/runner.sh
ENTRYPOINT ["/bin/bash", "/opt/runner.sh"]
