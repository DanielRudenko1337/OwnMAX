FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.txt README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV db_type=sqlite OWNMAX_DATA_DIR=/app/data
CMD ["python", "-m", "ownmax", "serve"]
