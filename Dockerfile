FROM python:3.12-slim

# Fonte com acentos e símbolos para os PDFs.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV ENEM_MCP_DIR=/data \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Embute as provas no cache da imagem para não depender do limite de 1 req/s da API em produção.
RUN enem-mcp --baixar-provas || echo "Falha ao baixar provas; serão baixadas sob demanda."

EXPOSE 8000
CMD ["enem-mcp", "--http", "--host", "0.0.0.0"]
