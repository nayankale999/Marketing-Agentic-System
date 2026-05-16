# OTel collector with the config baked in.
#
# We can't bind-mount the config from the host: this repo lives under a path
# containing ':' (the `(21-04-2026 15:16)` part), which the Docker daemon
# rejects when parsing the volume spec. A custom image with COPY sidesteps
# the parser entirely.
FROM otel/opentelemetry-collector-contrib:0.110.0

COPY otel-collector-config.yaml /etc/otelcol-contrib/config.yaml
