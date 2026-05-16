# Mock OIDC provider for local dev. Bakes the JSON config in (same `:` in
# path problem as the OTel collector). Multi-arch image — works on Apple
# Silicon out of the box.
FROM ghcr.io/navikt/mock-oauth2-server:2.1.10

COPY config.json /app/config.json
ENV JSON_CONFIG_PATH=/app/config.json
