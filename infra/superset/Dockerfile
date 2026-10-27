FROM apache/superset:6.1.0
USER root
RUN if command -v uv >/dev/null 2>&1; then \
      uv pip install --no-cache --python /app/.venv/bin/python "trino[sqlalchemy]==0.340.0" psycopg2-binary==2.9.11; \
    else \
      /app/.venv/bin/python -m ensurepip >/dev/null 2>&1 || true; \
      /app/.venv/bin/python -m pip install --no-cache-dir "trino[sqlalchemy]==0.340.0" psycopg2-binary==2.9.11; \
    fi
COPY superset_config.py /app/pythonpath/superset_config.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh
USER superset
CMD ["/app/start.sh"]
