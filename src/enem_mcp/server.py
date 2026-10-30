import argparse
import asyncio
import json
import logging
import os
import random
import re
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

from .enem_api import DIRETORIO_DADOS, ClienteEnem, ErroApiEnem
from .gerador_pdf import DISCIPLINAS, gerar_pdf_simulado, urls_de_imagens

# Limita downloads novos por chamada para não estourar o tempo de resposta (cada prova leva ~5s).
MAX_PROVAS_NOVAS_POR_CHAMADA = 5
ARQUIVO_SIMULADOS = DIRETORIO_DADOS / "simulados.json"
FORMATOS_IMAGEM = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}
REGEX_NOME_PDF = re.compile(r"^simulado-enem-[0-9a-f]{8,32}\.pdf$")
VALIDADE_PDF_REMOTO = 7 * 24 * 3600
# Ferramentas de histórico pessoal: sem login não dá para separar usuários no modo remoto.
FERRAMENTAS_SO_LOCAIS = ("listar_simulados", "meu_desempenho", "criar_simulado_revisao")

LEITURA = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
ESCRITA = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

Disciplina = Literal["ciencias-humanas", "ciencias-natureza", "linguagens", "matematica"]
Idioma = Literal["ingles", "espanhol"]

# Preenchida no modo HTTP: os PDFs passam a ser entregues por link de download.
url_publica: str | None = None

mcp = MCPServer(
    "enem-mcp",
    instructions=(
        "Ferramentas para estudar para o ENEM com questões oficiais (2009-2023) da API enem.dev: "
        "consultar questões e gabaritos, buscar por tema, criar simulados (inclusive em PDF), "
        "corrigir e acompanhar o desempenho. Ao aplicar um simulado, não revele o gabarito antes da correção."
    ),
)
cliente = ClienteEnem()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("fontTools").setLevel(logging.ERROR)


# ---------- utilitários ----------

def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sem_acento if not unicodedata.combining(c)).lower()


def _texto_completo(q: dict[str, Any]) -> str:
    partes = [q.get("context") or "", q.get("alternativesIntroduction") or ""]
    partes += [a.get("text") or "" for a in q["alternatives"]]
    return _normalizar(" ".join(partes))


def _termos(tema: str | None) -> list[str]:
    if not tema:
        return []
    return [_normalizar(t.strip()) for t in re.split(r"[,;|]", tema) if t.strip()]


def _corresponde(q: dict[str, Any], disciplina: str | None, termos: list[str], idioma: str) -> bool:
    if q["language"] not in (None, idioma):
        return False
    if disciplina and q["discipline"] != disciplina:
        return False
    if termos:
        texto = _texto_completo(q)
        return any(t in texto for t in termos)
    return True


def _trecho(q: dict[str, Any], tamanho: int = 220) -> str:
    texto = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[imagem]", q.get("context") or "")
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto[:tamanho] + ("..." if len(texto) > tamanho else "")


def _formatar_questao(q: dict[str, Any], mostrar_resposta: bool, rotulo: str | None = None) -> str:
    disciplina = DISCIPLINAS.get(q["discipline"] or "", q["discipline"] or "")
    idioma = f" ({q['language']})" if q["language"] else ""
    linhas = [f"### {rotulo or q['title']}{idioma}", f"_{q['title']} — {disciplina}_", ""]

    contexto = q.get("context") or ""
    if contexto:
        linhas += [contexto, ""]
    for arquivo in q.get("files") or []:
        if arquivo not in contexto:
            linhas += [f"![]({arquivo})", ""]
    if q.get("alternativesIntroduction"):
        linhas += [q["alternativesIntroduction"], ""]

    for alt in q["alternatives"]:
        texto = alt.get("text") or ""
        if alt.get("file"):
            texto = f"{texto} ![]({alt['file']})".strip()
        linhas.append(f"**{alt['letter']})** {texto}")

    if mostrar_resposta:
        linhas += ["", f"**Gabarito: {q['correctAlternative']}**"]
    return "\n".join(linhas)


async def _questoes_filtradas(
    anos: list[int] | None, disciplina: str | None, tema: str | None, idioma: str, minimo: int
) -> tuple[list[dict[str, Any]], list[int]]:
    """Coleta questões que atendem aos filtros. Retorna (questões, anos não consultados)."""
    disponiveis = await cliente.anos_disponiveis()
    if anos:
        invalidos = [a for a in anos if a not in disponiveis]
        if invalidos:
            raise ErroApiEnem(f"Anos sem prova disponível: {invalidos}. Disponíveis: {disponiveis}.")
        candidatos = list(dict.fromkeys(anos))
    else:
        candidatos = disponiveis[:]

    # Prioriza provas já em cache para responder rápido; depois baixa novas em ordem aleatória.
    em_cache = [a for a in candidatos if cliente.ano_em_cache(a)]
    novos = [a for a in candidatos if not cliente.ano_em_cache(a)]
    random.shuffle(em_cache)
    random.shuffle(novos)

    termos = _termos(tema)
    encontradas: list[dict[str, Any]] = []
    baixadas = 0
    pendentes: list[int] = []
    for ano in em_cache + novos:
        if len(encontradas) >= minimo:
            break
        if not cliente.ano_em_cache(ano):
            if baixadas >= MAX_PROVAS_NOVAS_POR_CHAMADA:
                pendentes.append(ano)
                continue
            baixadas += 1
        questoes = await cliente.questoes_do_ano(ano)
        encontradas += [q for q in questoes if _corresponde(q, disciplina, termos, idioma)]
    return encontradas, pendentes


def _carregar_simulados() -> dict[str, Any]:
    if not ARQUIVO_SIMULADOS.exists():
        return {}
    try:
        return json.loads(ARQUIVO_SIMULADOS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _salvar_simulados(simulados: dict[str, Any]) -> None:
    ARQUIVO_SIMULADOS.write_text(json.dumps(simulados, ensure_ascii=False, indent=1), encoding="utf-8")


def _chave(ano: int, numero: int, idioma: str | None) -> str:
    return f"{ano}-{numero}-{idioma or ''}"


def _chaves_ja_usadas() -> set[str]:
    return {
        _chave(q["ano"], q["numero"], q["idioma"])
        for s in _carregar_simulados().values()
        for q in s["questoes"]
    }


def _registrar_simulado(questoes: list[dict[str, Any]], filtros: dict[str, Any]) -> str:
    simulado_id = uuid.uuid4().hex[:12]
    simulados = _carregar_simulados()
    simulados[simulado_id] = {
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "filtros": filtros,
        "questoes": [
            {
                "ano": q["year"],
                "numero": q["index"],
                "idioma": q["language"],
                "disciplina": q["discipline"],
                "gabarito": q["correctAlternative"],
            }
            for q in questoes
        ],
        "correcoes": [],
    }
    _salvar_simulados(simulados)
    return simulado_id


def _descrever_filtros(filtros: dict[str, Any]) -> str:
    partes = []
    if filtros.get("tema"):
        partes.append(f"tema: {filtros['tema']}")
    if filtros.get("disciplina"):
        partes.append(f"disciplina: {DISCIPLINAS.get(filtros['disciplina'], filtros['disciplina'])}")
    if filtros.get("anos"):
        partes.append(f"anos: {', '.join(map(str, filtros['anos']))}")
    if filtros.get("revisao"):
        partes.append("revisão de questões erradas")
    return "; ".join(partes) or "questões variadas"


def _diretorio_pdf() -> Path:
    if url_publica:
        return DIRETORIO_DADOS / "pdfs"
    configurado = os.environ.get("ENEM_PDF_DIR")
    if configurado:
        return Path(configurado).expanduser()
    downloads = Path.home() / "Downloads"
    return downloads / "simulados-enem" if downloads.exists() else DIRETORIO_DADOS / "pdfs"


def _link_pdf(caminho: Path) -> str:
    if url_publica:
        return f"**[Baixar o PDF]({url_publica}/pdfs/{caminho.name})** (link válido por 7 dias)"
    return f"[{caminho.name}]({caminho.as_uri()})\n\nCaminho: `{caminho}`"


def _limpar_pdfs_antigos() -> None:
    limite = time.time() - VALIDADE_PDF_REMOTO
    for arquivo in _diretorio_pdf().glob("simulado-enem-*.pdf"):
        if arquivo.stat().st_mtime < limite:
            arquivo.unlink(missing_ok=True)


async def _exportar_pdf(
    simulado_id: str,
    questoes: list[dict[str, Any]],
    filtros: dict[str, Any],
    incluir_resolucao: bool,
    explicacoes: list[str | None] | None = None,
) -> Path:
    urls = [u for q in questoes for u in urls_de_imagens(q)]
    imagens = await cliente.baixar_imagens(urls)
    descricao = [
        f"{len(questoes)} questões — {_descrever_filtros(filtros)}",
        f"Gerado em {datetime.now():%d/%m/%Y %H:%M} · ID do simulado: {simulado_id}",
        "Questões oficiais do ENEM (INEP), obtidas via enem.dev.",
    ]
    destino = _diretorio_pdf() / f"simulado-enem-{simulado_id}.pdf"
    if url_publica:
        _limpar_pdfs_antigos()
    return await asyncio.to_thread(
        gerar_pdf_simulado,
        destino, f"Simulado ENEM · {simulado_id}", descricao, questoes, imagens, incluir_resolucao, explicacoes,
    )


async def _imagens_mcp(q: dict[str, Any]) -> list[Image]:
    """Imagens da questão como conteúdo MCP, para que o modelo consiga "ver" figuras e gráficos."""
    resultado = []
    for url, dados in (await cliente.baixar_imagens(urls_de_imagens(q))).items():
        formato = FORMATOS_IMAGEM.get(Path(url).suffix.lower())
        if dados and formato:
            resultado.append(Image(data=dados, format=formato))
    return resultado


# ---------- ferramentas ----------

@mcp.tool(title="Listar provas do ENEM", annotations=LEITURA, structured_output=False)
async def listar_provas() -> str:
    """Lista as provas do ENEM disponíveis (anos), com disciplinas e idiomas de cada uma."""
    try:
        provas = await cliente.listar_provas()
    except ErroApiEnem as erro:
        return str(erro)
    linhas = []
    for p in sorted(provas, key=lambda p: p["year"]):
        disciplinas = ", ".join(d["value"] for d in p["disciplines"])
        idiomas = ", ".join(i["value"] for i in p["languages"])
        linhas.append(f"- {p['title']} | disciplinas: {disciplinas} | idiomas: {idiomas}")
    return "\n".join(linhas)


@mcp.tool(title="Ver questão", annotations=LEITURA, structured_output=False)
async def obter_questao(
    ano: Annotated[int, Field(description="Ano da prova, ex.: 2022")],
    numero: Annotated[int, Field(ge=1, le=185, description="Número da questão na prova")],
    idioma: Annotated[Idioma | None, Field(description="Idioma para as questões 1-5 de língua estrangeira")] = None,
    mostrar_resposta: Annotated[bool, Field(description="Se true, inclui o gabarito")] = False,
    incluir_imagens: Annotated[bool, Field(description="Anexa as figuras da questão para análise visual")] = True,
) -> list[str | Image]:
    """Retorna o enunciado completo, as alternativas e as imagens (gráficos, tirinhas, mapas) de uma questão do ENEM."""
    try:
        q = await cliente.questao(ano, numero, idioma)
    except ErroApiEnem as erro:
        return [str(erro)]
    imagens = await _imagens_mcp(q) if incluir_imagens else []
    return [_formatar_questao(q, mostrar_resposta), *imagens]


@mcp.tool(title="Sortear questão", annotations=LEITURA, structured_output=False)
async def questao_aleatoria(
    tema: Annotated[str | None, Field(description="Palavras-chave do tema, separadas por vírgula")] = None,
    disciplina: Annotated[Disciplina | None, Field(description="Área do conhecimento")] = None,
    anos: Annotated[list[int] | None, Field(description="Anos das provas. Vazio = todos")] = None,
    idioma: Annotated[Idioma, Field(description="Idioma das questões de língua estrangeira")] = "ingles",
) -> list[str | Image]:
    """Sorteia uma questão (sem gabarito) para praticar. Depois use `obter_resposta` para conferir."""
    try:
        questoes, _ = await _questoes_filtradas(anos, disciplina, tema, idioma, 30)
    except ErroApiEnem as erro:
        return [str(erro)]
    if not questoes:
        return ["Nenhuma questão encontrada com esses filtros."]
    q = random.choice(questoes)
    return [_formatar_questao(q, mostrar_resposta=False), *await _imagens_mcp(q)]


@mcp.tool(title="Ver gabarito", annotations=LEITURA, structured_output=False)
async def obter_resposta(
    ano: Annotated[int, Field(description="Ano da prova, ex.: 2022")],
    numero: Annotated[int, Field(ge=1, le=185, description="Número da questão na prova")],
    idioma: Annotated[Idioma | None, Field(description="Idioma para as questões 1-5 de língua estrangeira")] = None,
) -> str:
    """Retorna o gabarito oficial (alternativa correta) de uma questão do ENEM, junto do enunciado para contexto."""
    try:
        q = await cliente.questao(ano, numero, idioma)
    except ErroApiEnem as erro:
        return str(erro)
    correta = next(a for a in q["alternatives"] if a["letter"] == q["correctAlternative"])
    texto = correta.get("text") or (f"![]({correta['file']})" if correta.get("file") else "")
    return (
        f"Resposta da {q['title']}: **{q['correctAlternative']}** — {texto}\n\n"
        f"---\n{_formatar_questao(q, mostrar_resposta=False)}"
    )


@mcp.tool(title="Buscar questões", annotations=LEITURA, structured_output=False)
async def buscar_questoes(
    tema: Annotated[
        str | None,
        Field(description="Palavras-chave buscadas no texto da questão. Separe alternativas com vírgula, ex.: 'fotossíntese, clorofila'"),
    ] = None,
    disciplina: Annotated[Disciplina | None, Field(description="Área do conhecimento")] = None,
    anos: Annotated[list[int] | None, Field(description="Anos das provas. Vazio = todos")] = None,
    idioma: Annotated[Idioma, Field(description="Idioma das questões de língua estrangeira")] = "ingles",
    limite: Annotated[int, Field(ge=1, le=50)] = 15,
) -> str:
    """Busca questões do ENEM por tema (palavra-chave), disciplina e/ou ano. Retorna uma lista resumida."""
    try:
        questoes, pendentes = await _questoes_filtradas(anos, disciplina, tema, idioma, limite)
    except ErroApiEnem as erro:
        return str(erro)

    if not questoes:
        msg = "Nenhuma questão encontrada com esses filtros."
    else:
        questoes.sort(key=lambda q: (q["year"], q["index"]))
        linhas = [f"{len(questoes[:limite])} questão(ões) encontrada(s):"]
        for q in questoes[:limite]:
            disciplina_q = DISCIPLINAS.get(q["discipline"] or "", "")
            linhas.append(f"- ENEM {q['year']} Q{q['index']} ({disciplina_q}): {_trecho(q)}")
        msg = "\n".join(linhas)
    if pendentes:
        msg += f"\n\n(Provas ainda não consultadas: {sorted(pendentes)}. Chame novamente para buscar nelas.)"
    return msg


async def _apresentar_simulado(
    simulado_id: str,
    escolhidas: list[dict[str, Any]],
    filtros: dict[str, Any],
    gerar_pdf: bool,
    incluir_gabarito_no_pdf: bool,
    avisos: list[str],
) -> str:
    partes = [f"# Simulado `{simulado_id}` — {len(escolhidas)} questão(ões) — {_descrever_filtros(filtros)}"]
    partes += [f"_{a}_" for a in avisos]

    if gerar_pdf:
        try:
            caminho = await _exportar_pdf(simulado_id, escolhidas, filtros, incluir_gabarito_no_pdf)
            partes.append(
                f"**PDF gerado:** {_link_pdf(caminho)}"
                + (
                    "\n\nNo final do PDF há uma página 'GABARITO ABAIXO' seguida das questões resolvidas. "
                    "Para incluir explicações de cada resposta, use `preparar_explicacoes` e depois "
                    "`exportar_simulado_pdf` com o parâmetro `explicacoes`."
                    if incluir_gabarito_no_pdf
                    else ""
                )
            )
        except Exception as erro:
            partes.append(f"Não foi possível gerar o PDF: {erro}")
        resumo = "\n".join(
            f"{i}. ENEM {q['year']} nº {q['index']} ({DISCIPLINAS.get(q['discipline'] or '', '')})"
            for i, q in enumerate(escolhidas, start=1)
        )
        partes.append(f"Questões do simulado:\n{resumo}")
    else:
        partes += [
            _formatar_questao(q, mostrar_resposta=False, rotulo=f"Questão {i}") for i, q in enumerate(escolhidas, start=1)
        ]

    partes.append(f"Para corrigir, use `corrigir_simulado` com simulado_id='{simulado_id}' e as respostas em ordem.")
    return "\n\n---\n\n".join(partes)


@mcp.tool(title="Criar simulado", annotations=ESCRITA, structured_output=False)
async def criar_simulado(
    quantidade: Annotated[int, Field(ge=1, le=90, description="Número de questões")] = 10,
    tema: Annotated[
        str | None,
        Field(description="Palavras-chave do tema. Separe alternativas com vírgula, ex.: 'genética, DNA, hereditariedade'"),
    ] = None,
    disciplina: Annotated[Disciplina | None, Field(description="Área do conhecimento")] = None,
    anos: Annotated[list[int] | None, Field(description="Anos das provas. Vazio = todos")] = None,
    idioma: Annotated[Idioma, Field(description="Idioma das questões de língua estrangeira")] = "ingles",
    gerar_pdf: Annotated[bool, Field(description="Gera um PDF para impressão/download com questões, imagens e folha de respostas")] = False,
    incluir_gabarito_no_pdf: Annotated[
        bool, Field(description="Adiciona ao final do PDF uma página 'GABARITO ABAIXO' e as questões resolvidas")
    ] = True,
    evitar_repetidas: Annotated[bool, Field(description="Evita questões que já saíram em simulados anteriores")] = True,
) -> str:
    """Cria um simulado com questões aleatórias do ENEM por tema/disciplina/ano, opcionalmente em PDF. Use `corrigir_simulado` com o ID retornado para corrigir."""
    try:
        questoes, pendentes = await _questoes_filtradas(anos, disciplina, tema, idioma, quantidade * 3)
    except ErroApiEnem as erro:
        return str(erro)
    if not questoes:
        return "Nenhuma questão encontrada com esses filtros para montar o simulado."

    random.shuffle(questoes)
    if evitar_repetidas and not url_publica:
        usadas = _chaves_ja_usadas()
        # Questões inéditas primeiro; repetidas só completam se faltar.
        questoes.sort(key=lambda q: _chave(q["year"], q["index"], q["language"]) in usadas)
    escolhidas = sorted(questoes[:quantidade], key=lambda q: (q["discipline"] or "", q["year"], q["index"]))

    filtros = {"tema": tema, "disciplina": disciplina, "anos": anos, "idioma": idioma}
    simulado_id = _registrar_simulado(escolhidas, filtros)
    avisos = []
    if len(escolhidas) < quantidade:
        avisos.append(f"Só foram encontradas {len(escolhidas)} questões com esses filtros.")
    if pendentes:
        avisos.append(f"Provas não consultadas nesta busca: {sorted(pendentes)}.")
    return await _apresentar_simulado(simulado_id, escolhidas, filtros, gerar_pdf, incluir_gabarito_no_pdf, avisos)


@mcp.tool(title="Preparar explicações do simulado", annotations=LEITURA, structured_output=False)
async def preparar_explicacoes(
    simulado_id: Annotated[str, Field(description="ID retornado por criar_simulado")],
    incluir_imagens: Annotated[bool, Field(description="Anexa as figuras das questões")] = True,
) -> list[str | Image]:
    """Retorna as questões de um simulado COM gabarito, para a IA escrever a explicação de cada resposta e
    depois chamar `exportar_simulado_pdf` com `explicacoes`. Não mostre o gabarito ao usuário no chat."""
    simulado = _carregar_simulados().get(simulado_id)
    if simulado is None:
        return [f"Simulado '{simulado_id}' não encontrado."]
    try:
        questoes = [await cliente.questao(q["ano"], q["numero"], q["idioma"]) for q in simulado["questoes"]]
    except ErroApiEnem as erro:
        return [str(erro)]

    conteudo: list[str | Image] = [
        f"Simulado `{simulado_id}` com {len(questoes)} questões. Leia CADA questão abaixo (texto e imagens) e escreva "
        "uma explicação ESPECÍFICA para ela — nunca reaproveite texto entre questões. Cada explicação deve:\n"
        "1. citar os dados/elementos concretos do enunciado (números, trechos, conceitos da própria questão);\n"
        "2. mostrar o raciocínio ou cálculo que leva à alternativa correta;\n"
        "3. dizer por que as alternativas incorretas mais tentadoras estão erradas;\n"
        "4. ter de 4 a 10 frases, em texto simples (sem LaTeX, tabelas ou markdown).\n\n"
        f"Depois chame `exportar_simulado_pdf` com simulado_id='{simulado_id}' e `explicacoes` = lista com "
        f"{len(questoes)} objetos {{questao, alternativa_correta, explicacao}} (questao = número 1..{len(questoes)} "
        "no simulado). O servidor confere a alternativa correta e rejeita explicações repetidas ou genéricas."
    ]
    for i, q in enumerate(questoes, start=1):
        conteudo.append(_formatar_questao(q, mostrar_resposta=True, rotulo=f"Questão {i}"))
        if incluir_imagens:
            conteudo += await _imagens_mcp(q)
    return conteudo


class Explicacao(BaseModel):
    questao: Annotated[int, Field(ge=1, description="Número da questão no simulado (1, 2, 3...)")]
    alternativa_correta: Annotated[Literal["A", "B", "C", "D", "E"], Field(description="Letra do gabarito")]
    explicacao: Annotated[str, Field(min_length=150, description="Explicação específica desta questão")]


def _validar_explicacoes(simulado: dict[str, Any], explicacoes: list[Explicacao]) -> list[str]:
    erros = []
    questoes = simulado["questoes"]
    numeros = [e.questao for e in explicacoes]
    faltando = sorted(set(range(1, len(questoes) + 1)) - set(numeros))
    if faltando:
        erros.append(f"Faltam explicações para as questões {faltando}.")
    duplicadas = sorted({n for n in numeros if numeros.count(n) > 1})
    if duplicadas:
        erros.append(f"Questões com mais de uma explicação: {duplicadas}.")

    vistas: dict[str, int] = {}
    for e in explicacoes:
        if e.questao > len(questoes):
            erros.append(f"O simulado só tem {len(questoes)} questões (recebi a questão {e.questao}).")
            continue
        gabarito = questoes[e.questao - 1]["gabarito"]
        if e.alternativa_correta != gabarito:
            erros.append(
                f"Questão {e.questao}: o gabarito oficial é {gabarito}, não {e.alternativa_correta}. "
                "Releia a questão e reescreva a explicação justificando a alternativa correta."
            )
        # Compara sem números para pegar textos-modelo que só trocam o índice.
        assinatura = re.sub(r"[\d\W]+", " ", _normalizar(e.explicacao)).strip()
        if assinatura in vistas:
            erros.append(f"As explicações das questões {vistas[assinatura]} e {e.questao} são iguais.")
        vistas.setdefault(assinatura, e.questao)
    return erros


@mcp.tool(title="Gerar PDF do simulado", annotations=ESCRITA, structured_output=False)
async def exportar_simulado_pdf(
    simulado_id: Annotated[str, Field(description="ID retornado por criar_simulado")],
    incluir_gabarito: Annotated[
        bool, Field(description="Adiciona ao final uma página 'GABARITO ABAIXO' e as questões resolvidas")
    ] = True,
    explicacoes: Annotated[
        list[Explicacao] | None,
        Field(description="Uma explicação específica por questão (obtenha as questões com `preparar_explicacoes`). Ficam salvas para as próximas exportações"),
    ] = None,
) -> str:
    """Gera (ou regera) o PDF de um simulado: questões com imagens, folha de respostas e, ao final, as questões
    resolvidas com o gabarito destacado e a explicação de cada resposta (se fornecida)."""
    simulados = _carregar_simulados()
    simulado = simulados.get(simulado_id)
    if simulado is None:
        return f"Simulado '{simulado_id}' não encontrado."
    if explicacoes is not None:
        erros = _validar_explicacoes(simulado, explicacoes)
        if erros:
            return "Explicações rejeitadas, corrija e envie novamente:\n- " + "\n- ".join(erros)
        simulado["explicacoes"] = [e.explicacao for e in sorted(explicacoes, key=lambda e: e.questao)]
        _salvar_simulados(simulados)
    try:
        questoes = [await cliente.questao(q["ano"], q["numero"], q["idioma"]) for q in simulado["questoes"]]
        caminho = await _exportar_pdf(
            simulado_id, questoes, simulado["filtros"], incluir_gabarito, simulado.get("explicacoes")
        )
    except Exception as erro:
        return f"Não foi possível gerar o PDF: {erro}"
    com_explicacao = " (com explicações)" if incluir_gabarito and simulado.get("explicacoes") else ""
    return f"PDF gerado{com_explicacao}: {_link_pdf(caminho)}"


@mcp.tool(title="Corrigir simulado", annotations=ESCRITA, structured_output=False)
async def corrigir_simulado(
    simulado_id: Annotated[str, Field(description="ID retornado por criar_simulado")],
    respostas: Annotated[
        list[str],
        Field(description="Letras marcadas, na ordem das questões, ex.: ['A','C','E']. Use '' para questão em branco"),
    ],
) -> str:
    """Corrige um simulado: mostra acertos, nota por disciplina e o gabarito. O resultado fica salvo no histórico."""
    simulados = _carregar_simulados()
    simulado = simulados.get(simulado_id)
    if simulado is None:
        return f"Simulado '{simulado_id}' não encontrado."

    questoes = simulado["questoes"]
    acertos = 0
    por_disciplina: dict[str, list[int]] = {}
    linhas = []
    for i, q in enumerate(questoes, start=1):
        marcada = respostas[i - 1].strip().upper() if i <= len(respostas) else ""
        certo = marcada == q["gabarito"]
        acertos += certo
        contagem = por_disciplina.setdefault(DISCIPLINAS.get(q["disciplina"] or "", "Outra"), [0, 0])
        contagem[0] += certo
        contagem[1] += 1
        status = "✔" if certo else "✘"
        linhas.append(
            f"{status} Questão {i} (ENEM {q['ano']} Q{q['numero']}): "
            f"sua resposta {marcada or '—'} | gabarito {q['gabarito']}"
        )

    total = len(questoes)
    simulado.setdefault("correcoes", []).append({
        "data": datetime.now().isoformat(timespec="seconds"),
        "respostas": [r.strip().upper() for r in respostas],
        "acertos": acertos,
        "total": total,
    })
    _salvar_simulados(simulados)

    resumo = [f"## Resultado do simulado `{simulado_id}`: {acertos}/{total} ({acertos / total:.0%})", ""]
    resumo += [f"- {d}: {a}/{t}" for d, (a, t) in por_disciplina.items()]
    if len(respostas) != total:
        resumo.append(f"\n_Atenção: foram enviadas {len(respostas)} respostas para {total} questões._")
    return "\n".join(resumo + [""] + linhas)


def _historico_respostas() -> list[tuple[str, dict[str, Any], bool]]:
    """(data, questão, acertou) da correção mais recente de cada simulado, em ordem cronológica."""
    registros = []
    for s in _carregar_simulados().values():
        if not s.get("correcoes"):
            continue
        correcao = s["correcoes"][-1]
        for i, q in enumerate(s["questoes"]):
            marcada = correcao["respostas"][i] if i < len(correcao["respostas"]) else ""
            registros.append((correcao["data"], q, marcada == q["gabarito"]))
    registros.sort(key=lambda r: r[0])
    return registros


@mcp.tool(title="Listar meus simulados", annotations=LEITURA, structured_output=False)
async def listar_simulados() -> str:
    """Lista os simulados criados, com data, filtros e nota (se já corrigidos)."""
    simulados = _carregar_simulados()
    if not simulados:
        return "Nenhum simulado criado ainda."
    linhas = []
    for sid, s in sorted(simulados.items(), key=lambda item: item[1]["criado_em"], reverse=True):
        nota = "não corrigido"
        if s.get("correcoes"):
            c = s["correcoes"][-1]
            nota = f"{c['acertos']}/{c['total']} ({c['acertos'] / c['total']:.0%})"
        linhas.append(
            f"- `{sid}` — {s['criado_em'][:16].replace('T', ' ')} — {len(s['questoes'])} questões — "
            f"{_descrever_filtros(s['filtros'])} — {nota}"
        )
    return "\n".join(linhas)


@mcp.tool(title="Meu desempenho", annotations=LEITURA, structured_output=False)
async def meu_desempenho() -> str:
    """Mostra estatísticas de desempenho nos simulados corrigidos: aproveitamento geral, por disciplina, por ano da prova e evolução."""
    registros = _historico_respostas()
    if not registros:
        return "Ainda não há simulados corrigidos. Crie um com `criar_simulado` e corrija com `corrigir_simulado`."

    def tabela(chave) -> list[str]:
        grupos: dict[Any, list[int]] = {}
        for _, q, certo in registros:
            g = grupos.setdefault(chave(q), [0, 0])
            g[0] += certo
            g[1] += 1
        return [f"- {k}: {a}/{t} ({a / t:.0%})" for k, (a, t) in sorted(grupos.items(), key=lambda i: str(i[0]))]

    acertos = sum(certo for _, _, certo in registros)
    total = len(registros)
    linhas = [f"## Desempenho geral: {acertos}/{total} ({acertos / total:.0%})", "", "### Por disciplina"]
    linhas += tabela(lambda q: DISCIPLINAS.get(q["disciplina"] or "", "Outra"))
    linhas += ["", "### Por ano da prova"]
    linhas += tabela(lambda q: q["ano"])

    simulados = [s for s in _carregar_simulados().values() if s.get("correcoes")]
    simulados.sort(key=lambda s: s["correcoes"][-1]["data"])
    linhas += ["", "### Evolução (últimos simulados)"]
    for s in simulados[-10:]:
        c = s["correcoes"][-1]
        linhas.append(f"- {c['data'][:10]}: {c['acertos']}/{c['total']} ({c['acertos'] / c['total']:.0%}) — {_descrever_filtros(s['filtros'])}")

    pendentes = len(_questoes_para_revisar())
    if pendentes:
        linhas += ["", f"Você tem {pendentes} questão(ões) errada(s) para revisar — use `criar_simulado_revisao`."]
    return "\n".join(linhas)


def _questoes_para_revisar() -> list[dict[str, Any]]:
    """Questões cuja resposta mais recente do usuário está errada."""
    ultima: dict[str, tuple[dict[str, Any], bool]] = {}
    for _, q, certo in _historico_respostas():
        ultima[_chave(q["ano"], q["numero"], q["idioma"])] = (q, certo)
    return [q for q, certo in ultima.values() if not certo]


@mcp.tool(title="Criar simulado de revisão", annotations=ESCRITA, structured_output=False)
async def criar_simulado_revisao(
    quantidade: Annotated[int, Field(ge=1, le=90, description="Número máximo de questões")] = 10,
    disciplina: Annotated[Disciplina | None, Field(description="Revisar só uma área")] = None,
    gerar_pdf: Annotated[bool, Field(description="Gera também um PDF")] = False,
) -> str:
    """Cria um simulado só com questões que você errou em simulados anteriores (e ainda não acertou depois)."""
    refs = [q for q in _questoes_para_revisar() if not disciplina or q["disciplina"] == disciplina]
    if not refs:
        return "Não há questões erradas para revisar."
    refs = random.sample(refs, min(quantidade, len(refs)))
    try:
        escolhidas = [await cliente.questao(r["ano"], r["numero"], r["idioma"]) for r in refs]
    except ErroApiEnem as erro:
        return str(erro)
    filtros = {"disciplina": disciplina, "revisao": True}
    simulado_id = _registrar_simulado(escolhidas, filtros)
    return await _apresentar_simulado(simulado_id, escolhidas, filtros, gerar_pdf, True, [])


# ---------- prompts ----------

@mcp.prompt()
def explicar_questao(ano: str, numero: str) -> str:
    """Explicação didática e passo a passo de uma questão do ENEM."""
    return (
        f"Use a ferramenta `obter_questao` (com imagens) para buscar a questão {numero} do ENEM {ano} "
        "e `obter_resposta` para o gabarito. Depois, explique de forma didática: "
        "1) o que a questão pede; 2) os conceitos/conteúdos cobrados; 3) a resolução passo a passo; "
        "4) por que cada alternativa incorreta está errada; 5) uma dica para questões parecidas."
    )


@mcp.prompt()
def sessao_de_estudo(tema: str, quantidade: str = "5") -> str:
    """Sessão interativa: revisão teórica de um tema seguida de questões uma a uma."""
    return (
        f"Quero estudar '{tema}' para o ENEM. Faça um resumo teórico curto do tema e, em seguida, "
        f"use `criar_simulado` com tema '{tema}' e {quantidade} questões. Apresente UMA questão por vez, "
        "espere minha resposta, diga se acertei e explique antes de passar para a próxima. "
        "No final, use `corrigir_simulado` com todas as minhas respostas e comente meus pontos fracos."
    )


@mcp.prompt()
def simulado_pdf_explicado(tema: str, quantidade: str = "10") -> str:
    """Gera um simulado em PDF com questões resolvidas e explicadas ao final."""
    return (
        f"Crie um simulado com `criar_simulado` (tema '{tema}', {quantidade} questões, gerar_pdf=false). "
        "Depois chame `preparar_explicacoes` com o ID, escreva uma explicação para cada questão e chame "
        "`exportar_simulado_pdf` com essas `explicacoes`. Não mostre gabarito nem explicações no chat; "
        "apenas me envie o link do PDF."
    )


@mcp.prompt()
def plano_de_estudos() -> str:
    """Monta um plano de estudos com base no desempenho registrado."""
    return (
        "Use `meu_desempenho` e `listar_simulados` para analisar meu histórico. Identifique as áreas e temas "
        "mais fracos e monte um plano de estudos semanal, sugerindo simulados específicos (tema/disciplina) "
        "e uma revisão com `criar_simulado_revisao`."
    )


@mcp.custom_route("/pdfs/{nome}", methods=["GET"])
async def baixar_pdf(request: Request) -> Response:
    nome = request.path_params["nome"]
    caminho = _diretorio_pdf() / nome
    if not REGEX_NOME_PDF.match(nome) or not caminho.is_file():
        return PlainTextResponse("PDF não encontrado ou expirado.", status_code=404)
    return FileResponse(caminho, media_type="application/pdf", filename=nome)


@mcp.custom_route("/health", methods=["GET"])
async def saude(request: Request) -> Response:
    return PlainTextResponse("ok")


async def _baixar_todas_as_provas() -> None:
    for ano in await cliente.anos_disponiveis():
        await cliente.questoes_do_ano(ano)
        print(f"ENEM {ano}: ok", flush=True)


def main() -> None:
    global url_publica
    parser = argparse.ArgumentParser(prog="enem-mcp", description="MCP server com questões do ENEM")
    parser.add_argument("--http", action="store_true", help="servidor remoto (Streamable HTTP) em vez de stdio")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument(
        "--url-publica",
        default=os.environ.get("ENEM_MCP_URL_PUBLICA") or os.environ.get("RENDER_EXTERNAL_URL"),
        help="endereço público do servidor, usado nos links de download dos PDFs",
    )
    parser.add_argument("--baixar-provas", action="store_true", help="baixa todas as provas para o cache e sai")
    args = parser.parse_args()

    if args.baixar_provas:
        asyncio.run(_baixar_todas_as_provas())
        return
    if not args.http:
        mcp.run()
        return

    url_publica = (args.url_publica or f"http://{args.host}:{args.port}").rstrip("/")
    for nome in FERRAMENTAS_SO_LOCAIS:
        mcp.remove_tool(nome)
    mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True)


if __name__ == "__main__":
    main()
