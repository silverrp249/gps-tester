FROM python:3.12-slim

LABEL org.opencontainers.image.title="gps-tester" \
      org.opencontainers.image.description="GPS Telemetry Simulator & Protocol Validator" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY gps_tester ./gps_tester
RUN pip install --no-cache-dir .

# Reports are written here; mount a volume to keep them:
#   docker run --rm -v "$PWD/reports:/reports" gps-tester --host ... --port ...
WORKDIR /reports
ENTRYPOINT ["gps-tester", "--report-dir", "/reports"]
CMD ["--help"]
