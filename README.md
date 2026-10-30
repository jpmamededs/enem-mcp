# enem-mcp

<!-- mcp-name: io.github.jpmamededs/enem-mcp -->

Servidor [MCP](https://modelcontextprotocol.io) para estudar para o ENEM com as **questões oficiais (2009–2023)**, usando a API pública [enem.dev](https://enem.dev).

- Consulte qualquer questão e seu gabarito, com as imagens (gráficos, mapas, tirinhas)
- Busque questões por tema, disciplina e ano
- Crie simulados — inclusive em **PDF** com folha de respostas, página "GABARITO ABAIXO" e questões resolvidas com explicação
- Corrija simulados e acompanhe seu desempenho por disciplina, com revisão das questões erradas

## Instalação

Requer [uv](https://docs.astral.sh/uv/getting-started/installation/) (ou Python 3.10+ com `pip install enem-mcp`).

### VS Code

Adicione ao `.vscode/mcp.json` (ou às configurações de usuário):

```json
{
  "servers": {
    "enem": {
      "type": "stdio",
      "command": "uvx",
      "args": ["enem-mcp"]
    }
  }
}
```

### Claude Desktop / Cursor / outros clientes

```json
{
  "mcpServers": {
    "enem": {
      "command": "uvx",
      "args": ["enem-mcp"]
    }
  }
}
```

## Ferramentas

| Ferramenta | Descrição |
|---|---|
| `listar_provas` | Anos disponíveis, disciplinas e idiomas |
| `obter_questao` | Enunciado, alternativas e imagens de uma questão |
| `obter_resposta` | Gabarito oficial de uma questão |
| `questao_aleatoria` | Sorteia uma questão para praticar |
| `buscar_questoes` | Busca por tema (palavras-chave), disciplina e ano |
| `criar_simulado` | Simulado por tema/disciplina/ano, opcionalmente em PDF |
| `preparar_explicacoes` / `exportar_simulado_pdf` | Gera o PDF com as questões resolvidas e explicadas |
| `corrigir_simulado` | Corrige e salva o resultado |
| `listar_simulados` / `meu_desempenho` | Histórico e estatísticas |
| `criar_simulado_revisao` | Simulado só com as questões que você errou |

Prompts: `explicar_questao`, `sessao_de_estudo`, `simulado_pdf_explicado`, `plano_de_estudos`.

Exemplos de pedidos: *"faz um simulado em PDF com 15 questões de genética, com explicação no final"*, *"qual a resposta da questão 136 do ENEM 2021?"*.

## Configuração (opcional)

| Variável | Padrão |
|---|---|
| `ENEM_PDF_DIR` | `Downloads/simulados-enem` |
| `ENEM_MCP_DIR` | Windows: `%LOCALAPPDATA%\enem-mcp`; macOS: `~/Library/Application Support/enem-mcp`; Linux: `~/.local/share/enem-mcp` |

A API enem.dev aceita 1 requisição por segundo; as provas baixadas ficam em cache local, então só a primeira consulta de cada ano é lenta.

## Desenvolvimento

```bash
uv venv && uv pip install -e .
uv run enem-mcp
```

## Créditos

Dados da [enem-api](https://github.com/yunger7/enem-api) (GPL-2.0). Questões do ENEM são de domínio público (INEP). Este projeto não é afiliado ao INEP nem ao MEC.

Licença: MIT.
