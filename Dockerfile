FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY knappy ./knappy
COPY slack ./slack

RUN pip install --no-cache-dir .

# Socket Mode dials out. Run exactly one worker so two processes do not both answer.
CMD ["python", "-m", "knappy.main"]
