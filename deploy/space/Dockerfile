# Hugging Face Space (Docker SDK) running the sign-off service, stateless.
# It builds from the GitHub repository, so restarting the Space picks up main.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

ARG REPO=https://github.com/yaseminbridges/mondo-evidence-browser.git
ARG REF=main
RUN useradd --create-home app
USER app
WORKDIR /home/app/browser
RUN git clone --depth 1 --branch "$REF" "$REPO" . && uv sync --frozen --no-dev

# Spaces send traffic to app_port (7860).
EXPOSE 7860
CMD ["uv", "run", "--no-dev", "mondo-evidence-browser", "serve", "--host", "0.0.0.0", "--port", "7860"]
