FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
ARG AUTO_CODER_SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=$AUTO_CODER_SOURCE_REVISION
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
WORKDIR /workspace
ENTRYPOINT ["auto-coder"]
