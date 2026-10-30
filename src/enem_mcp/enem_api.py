import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import __version__

URL_BASE = "https://api.enem.dev/v1"
# Só baixamos imagens do domínio oficial do projeto.
PREFIXO_IMAGENS = "https://enem.dev/"


def _diretorio_dados() -> Path:
    configurado = os.environ.get("ENEM_MCP_DIR")
    if configurado:
        return Path(configurado).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "enem-mcp"


DIRETORIO_DADOS = _diretorio_dados()
DIRETORIO_CACHE = DIRETORIO_DADOS / "cache"
DIRETORIO_IMAGENS = DIRETORIO_CACHE / "imagens"
DOWNLOADS_SIMULTANEOS = 6
# A API permite 1 requisição/segundo nos endpoints sem cache.
INTERVALO_MINIMO = 1.1
LIMITE_POR_PAGINA = 50
MAX_TENTATIVAS = 4


class ErroApiEnem(Exception):
    pass


class ClienteEnem:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=URL_BASE, timeout=30, headers={"User-Agent": f"enem-mcp/{__version__}"}
        )
        self._trava = asyncio.Lock()
        self._ultima_requisicao = 0.0
        self._provas: list[dict[str, Any]] | None = None
        self._questoes_por_ano: dict[int, list[dict[str, Any]]] = {}
        self._semaforo_imagens = asyncio.Semaphore(DOWNLOADS_SIMULTANEOS)
        DIRETORIO_IMAGENS.mkdir(parents=True, exist_ok=True)

    async def _get(self, caminho: str, params: dict[str, Any] | None = None) -> Any:
        for _ in range(MAX_TENTATIVAS):
            async with self._trava:
                espera = self._ultima_requisicao + INTERVALO_MINIMO - time.monotonic()
                if espera > 0:
                    await asyncio.sleep(espera)
                try:
                    resposta = await self._http.get(caminho, params=params)
                except httpx.HTTPError as erro:
                    raise ErroApiEnem(f"Falha de conexão com a API enem.dev: {erro}") from erro
                finally:
                    self._ultima_requisicao = time.monotonic()

            if resposta.status_code == 429:
                await asyncio.sleep(_tempo_retry(resposta.headers.get("Retry-After")))
                continue
            if resposta.status_code >= 400:
                try:
                    mensagem = resposta.json()["error"]["message"]
                except Exception:
                    mensagem = resposta.text[:200]
                raise ErroApiEnem(f"Erro {resposta.status_code} da API enem.dev: {mensagem}")
            return resposta.json()

        raise ErroApiEnem("Limite de requisições da API enem.dev excedido. Tente novamente em alguns segundos.")

    async def listar_provas(self) -> list[dict[str, Any]]:
        if self._provas is None:
            self._provas = _ler_cache("provas.json")
        if self._provas is None:
            self._provas = await self._get("/exams")
            _gravar_cache("provas.json", self._provas)
        return self._provas

    async def anos_disponiveis(self) -> list[int]:
        return sorted(p["year"] for p in await self.listar_provas())

    def ano_em_cache(self, ano: int) -> bool:
        return ano in self._questoes_por_ano or (DIRETORIO_CACHE / f"questoes_{ano}.json").exists()

    async def questoes_do_ano(self, ano: int) -> list[dict[str, Any]]:
        """Todas as questões de uma prova (inclui as versões de inglês e espanhol)."""
        if ano in self._questoes_por_ano:
            return self._questoes_por_ano[ano]

        questoes = _ler_cache(f"questoes_{ano}.json")
        if questoes is None:
            questoes = []
            offset = 0
            while True:
                pagina = await self._get(
                    f"/exams/{ano}/questions",
                    {"limit": LIMITE_POR_PAGINA, "offset": offset},
                )
                questoes.extend(pagina["questions"])
                if not pagina["metadata"]["hasMore"]:
                    break
                offset += LIMITE_POR_PAGINA
            _gravar_cache(f"questoes_{ano}.json", questoes)

        self._questoes_por_ano[ano] = questoes
        return questoes

    async def questao(self, ano: int, numero: int, idioma: str | None = None) -> dict[str, Any]:
        if self.ano_em_cache(ano):
            candidatas = [q for q in await self.questoes_do_ano(ano) if q["index"] == numero]
            if not candidatas:
                raise ErroApiEnem(f"Questão {numero} não encontrada na prova de {ano}.")
            preferido = idioma or "ingles"
            for q in candidatas:
                if q["language"] in (None, preferido):
                    return q
            return candidatas[0]

        params = {"language": idioma} if idioma else None
        return await self._get(f"/exams/{ano}/questions/{numero}", params)

    async def baixar_imagem(self, url: str) -> bytes | None:
        """Baixa (com cache em disco) uma imagem de questão. Retorna None se não for possível."""
        if not url.startswith(PREFIXO_IMAGENS):
            return None
        extensao = Path(urlparse(url).path).suffix.lower()[:5]
        caminho = DIRETORIO_IMAGENS / (hashlib.sha1(url.encode()).hexdigest() + extensao)
        if caminho.exists():
            return caminho.read_bytes()

        async with self._semaforo_imagens:
            try:
                resposta = await self._http.get(url, follow_redirects=True)
            except httpx.HTTPError:
                return None
        if resposta.status_code != 200 or not resposta.content:
            return None
        caminho.write_bytes(resposta.content)
        return resposta.content

    async def baixar_imagens(self, urls: list[str]) -> dict[str, bytes | None]:
        unicas = list(dict.fromkeys(urls))
        conteudos = await asyncio.gather(*(self.baixar_imagem(u) for u in unicas))
        return dict(zip(unicas, conteudos))


def _tempo_retry(valor: str | None) -> float:
    if not valor:
        return 2.0
    try:
        numero = float(valor)
    except ValueError:
        return 2.0
    # A documentação informa milissegundos, mas aceitamos segundos por segurança.
    segundos = numero / 1000 if numero > 100 else numero
    return min(max(segundos, 1.0), 15.0)


def _ler_cache(nome: str) -> Any:
    caminho = DIRETORIO_CACHE / nome
    if not caminho.exists():
        return None
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _gravar_cache(nome: str, dados: Any) -> None:
    (DIRETORIO_CACHE / nome).write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
