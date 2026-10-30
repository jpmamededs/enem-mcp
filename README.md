# enem-mcp

<!-- mcp-name: io.github.jpmamededs/enem-mcp -->

Servidor [MCP](https://modelcontextprotocol.io) para estudar para o ENEM com as **questões oficiais (2009–2023)**, usando a API pública [enem.dev](https://enem.dev).

- Consulte qualquer questão e seu gabarito, com as imagens (gráficos, mapas, tirinhas)
- Busque questões por tema, disciplina e ano
- Crie simulados — inclusive em **PDF** com folha de respostas, página "GABARITO ABAIXO" e questões resolvidas com explicação
- Corrija simulados e acompanhe seu desempenho por disciplina, com revisão das questões erradas

## Instalação

### Claude (site, app desktop e celular) — sem instalar nada

1. No Claude, abra **Configurações → Conectores → Adicionar conector personalizado**.
2. Em **Nome**, escreva `ENEM`.
3. Em **URL do servidor MCP**, cole: `https://enem-mcp.onrender.com/mcp`
4. Clique em **Adicionar**. Pronto: é só pedir no chat, por exemplo *"faz um simulado de 10 questões de geometria"*.

Nesse modo os PDFs vêm como link de download (válido por 7 dias). Histórico de desempenho e simulados de revisão só existem na instalação local, abaixo.

### Claude Desktop (um clique)

1. Baixe o arquivo **`enem-mcp.mcpb`** da [página de releases](https://github.com/jpmamededs/enem-mcp/releases/latest).
2. Dê dois cliques no arquivo (ou arraste para a janela do Claude Desktop) e clique em **Instalar**.

Não é preciso instalar Python: o Claude Desktop cuida de tudo.

### Claude Code

```bash
claude mcp add enem -- uvx enem-mcp
```

### Outros clientes

Requer [uv](https://docs.astral.sh/uv/getting-started/installation/) (ou Python 3.10+ com `pip install enem-mcp`).

#### VS Code

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

#### Cursor / outros clientes

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
uv run enem-mcp            # stdio (local)
uv run enem-mcp --http     # servidor remoto em http://127.0.0.1:8000/mcp
```

### Hospedar o servidor remoto

O repositório tem `Dockerfile` e `render.yaml`. No [Render](https://render.com): **New → Blueprint**, escolha este repositório e confirme. O endereço do conector será `https://<nome-do-serviço>.onrender.com/mcp`. Em outras plataformas, use o `Dockerfile` e defina `ENEM_MCP_URL_PUBLICA` com o endereço público (usado nos links dos PDFs).

## Política de privacidade

- **Dados coletados:** apenas os parâmetros das ferramentas (ex.: ano, tema, respostas de um simulado). Não há cadastro, login, nem coleta de nome, e-mail ou dados do dispositivo.
- **Uso e armazenamento:** os simulados (questões sorteadas, respostas e explicações) são guardados para permitir correção e geração de PDF. Na instalação local, ficam só no seu computador. No servidor remoto, ficam no servidor, identificados apenas por um código aleatório.
- **Compartilhamento com terceiros:** o servidor consulta a API pública [enem.dev](https://enem.dev) para obter questões e imagens; nenhum dado seu é enviado a ela além do ano/número da questão. Nada é vendido ou compartilhado.
- **Retenção:** no servidor remoto, PDFs são apagados após 7 dias; simulados podem ser apagados a qualquer momento em reinicializações. Localmente, os dados ficam até você apagar a pasta `enem-mcp`.
- **Contato:** abra uma issue em [github.com/jpmamededs/enem-mcp/issues](https://github.com/jpmamededs/enem-mcp/issues).

## Créditos

Dados da [enem-api](https://github.com/yunger7/enem-api) (GPL-2.0). Questões do ENEM são de domínio público (INEP). Este projeto não é afiliado ao INEP nem ao MEC.

Licença: MIT.
