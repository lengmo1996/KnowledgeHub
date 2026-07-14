FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs
RUN python -m pip install --no-cache-dir ".[rag]"

RUN useradd --create-home --uid 1000 knowledgehub
USER knowledgehub
ENTRYPOINT ["python", "-m"]
