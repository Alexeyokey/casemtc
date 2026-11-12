FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM base AS dependencies
COPY requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --only-binary=:all: \
       --require-hashes -r /tmp/requirements.lock

FROM base AS runtime
ARG CASEMTC_BUILD_SHA
ENV PATH="/opt/venv/bin:$PATH" \
    CASEMTC_BUILD_SHA="${CASEMTC_BUILD_SHA}" \
    CASEMTC_DB_PATH=/var/lib/energytool/casemtc.sqlite3 \
    CASEMTC_LEADS_DIR=/var/lib/energytool/leads
WORKDIR /app
RUN groupadd --gid 10001 energytool \
    && useradd --uid 10001 --gid 10001 --no-create-home energytool \
    && install -d -o 10001 -g 10001 /var/lib/energytool
COPY --from=dependencies /opt/venv /opt/venv
COPY consultation.py knowledge_base.py organizer_api.py rag_consultation.py solution.py requirements.txt ./
COPY backend ./backend
COPY core ./core
COPY scripts/build_submission.py ./scripts/build_submission.py

USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
