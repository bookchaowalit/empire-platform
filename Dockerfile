FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY packages ./packages
COPY services ./services
COPY contracts ./contracts
COPY db ./db
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN pip install --no-cache-dir . \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && useradd --system --uid 10001 --no-create-home empire
USER empire
# Secrets (database URLs, public key PEM) arrive as environment variables at runtime only.
ENTRYPOINT ["docker-entrypoint.sh"]
