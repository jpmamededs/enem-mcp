import re
from io import BytesIO
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import MethodReturnValue, XPos, YPos
from PIL import Image as ImagemPIL

REGEX_IMAGEM_MD = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)")

# Fontes TrueType com suporte a Unicode, na ordem de preferência.
CANDIDATAS_FONTE = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]
FONTE_SIMBOLOS = "C:/Windows/Fonts/seguisym.ttf"
COR_CORRETA = (20, 120, 50)
FUNDO_CORRETA = (225, 245, 230)

DISCIPLINAS = {
    "ciencias-humanas": "Ciências Humanas",
    "ciencias-natureza": "Ciências da Natureza",
    "linguagens": "Linguagens",
    "matematica": "Matemática",
}


def urls_de_imagens(q: dict[str, Any]) -> list[str]:
    urls = REGEX_IMAGEM_MD.findall(q.get("context") or "")
    urls += q.get("files") or []
    urls += [a["file"] for a in q["alternatives"] if a.get("file")]
    return list(dict.fromkeys(urls))


class _Pdf(FPDF):
    def __init__(self, rodape: str) -> None:
        super().__init__(format="A4")
        self.rodape = rodape
        self.fonte = "Helvetica"
        self.unicode = False
        for regular, negrito in CANDIDATAS_FONTE:
            if Path(regular).exists() and Path(negrito).exists():
                self.add_font("Principal", "", regular)
                self.add_font("Principal", "B", negrito)
                self.fonte = "Principal"
                self.unicode = True
                if Path(FONTE_SIMBOLOS).exists():
                    self.add_font("Simbolos", "", FONTE_SIMBOLOS)
                    self.set_fallback_fonts(["Simbolos"])
                break
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(True, margin=18)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(self.fonte, "", 8)
        self.set_text_color(130)
        self.cell(0, 6, self.texto(f"{self.rodape} — página {self.page_no()}/{{nb}}"), align="C")
        self.set_text_color(0)

    def texto(self, valor: str) -> str:
        if self.unicode:
            return valor
        trocas = {"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", "•": "-"}
        for de, para in trocas.items():
            valor = valor.replace(de, para)
        return valor.encode("latin-1", "replace").decode("latin-1")

    def altura_paragrafo(
        self, valor: str, tamanho: float = 10, estilo: str = "", altura: float = 5, recuo: float = 0
    ) -> float:
        valor = _limpar_markdown(valor)
        if not valor:
            return 0
        self.set_font(self.fonte, estilo, tamanho)
        linhas = self.multi_cell(
            self.epw - recuo, altura, self.texto(valor), dry_run=True, output=MethodReturnValue.LINES
        )
        return len(linhas) * altura

    def garantir_espaco(self, altura: float) -> None:
        """Quebra a página antes se o bloco não couber (blocos maiores que a página são divididos)."""
        if self.get_y() + min(altura, self.eph * 0.9) > self.page_break_trigger:
            self.add_page()

    def reservar_texto(self, altura: float, inicio: float = 15) -> None:
        """Para texto divisível: só divide se ficarem ao menos ~3 linhas antes e 2 depois da quebra."""
        restante = self.page_break_trigger - self.get_y()
        if altura > restante and (restante < inicio or altura - restante < 10):
            self.add_page()

    def paragrafo(
        self, valor: str, tamanho: float = 10, estilo: str = "", altura: float = 5, recuo: float = 0, fundo: bool = False
    ) -> None:
        valor = _limpar_markdown(valor)
        if not valor:
            return
        self.set_font(self.fonte, estilo, tamanho)
        self.set_x(self.l_margin + recuo)
        self.multi_cell(
            self.epw - recuo, altura, self.texto(valor), fill=fundo, new_x=XPos.LMARGIN, new_y=YPos.NEXT
        )

    def tamanho_imagem(self, dados: bytes | None, recuo: float = 0) -> tuple[float, float] | None:
        if dados is None:
            return None
        try:
            with ImagemPIL.open(BytesIO(dados)) as img:
                largura_px, altura_px = img.size
        except Exception:
            return None
        largura = min(largura_px * 25.4 / 96, self.epw - recuo)
        altura = largura * altura_px / largura_px
        altura_max = self.eph * 0.55
        if altura > altura_max:
            largura *= altura_max / altura
            altura = altura_max
        return largura, altura

    def imagem(self, dados: bytes | None, url: str, recuo: float = 0) -> None:
        tamanho = self.tamanho_imagem(dados, recuo)
        try:
            if tamanho is None or dados is None:
                raise ValueError
            largura, altura = tamanho
            self.garantir_espaco(altura)
            self.image(BytesIO(dados), x=self.l_margin + recuo, w=largura, h=altura)
            self.ln(2)
        except Exception:
            self.set_text_color(150, 0, 0)
            self.paragrafo(f"[imagem indisponível: {url}]", tamanho=8, recuo=recuo)
            self.set_text_color(0)

    def conteudo(self, markdown: str, imagens: dict[str, bytes | None], recuo: float = 0) -> None:
        partes = REGEX_IMAGEM_MD.split(markdown)
        # re.split com grupo alterna: texto, url, texto, url, ...
        for i, parte in enumerate(partes):
            if i % 2 == 0:
                for bloco in re.split(r"\n\s*\n", parte):
                    if bloco.strip():
                        self.reservar_texto(self.altura_paragrafo(bloco, recuo=recuo))
                        self.paragrafo(bloco, recuo=recuo)
                        self.ln(1.5)
            else:
                self.imagem(imagens.get(parte), parte, recuo=recuo)


def _limpar_markdown(texto: str) -> str:
    texto = texto.replace("\\", "")
    texto = re.sub(r"\*\*(.+?)\*\*", r"\1", texto, flags=re.S)
    texto = re.sub(r"(?<![\w*])[*_](\S(?:.*?\S)?)[*_](?![\w*])", r"\1", texto, flags=re.S)
    texto = re.sub(r"^#{1,6}\s*", "", texto, flags=re.M)
    texto = re.sub(r"[ \t]+", " ", texto)
    return texto.strip()


def _escrever_questao(
    pdf: _Pdf,
    numero: int,
    q: dict[str, Any],
    imagens: dict[str, bytes | None],
    resolvida: bool = False,
    explicacao: str | None = None,
) -> None:
    disciplina = DISCIPLINAS.get(q["discipline"] or "", "")
    idioma = f" · {q['language'].capitalize()}" if q["language"] else ""
    # Cabeçalho nunca fica sozinho no fim da página.
    pdf.garantir_espaco(40)
    pdf.set_fill_color(235, 240, 250)
    pdf.set_font(pdf.fonte, "B", 11)
    pdf.cell(
        0, 7, pdf.texto(f"Questão {numero}  —  ENEM {q['year']}, nº {q['index']} · {disciplina}{idioma}"),
        fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT,
    )
    pdf.ln(2)

    contexto = q.get("context") or ""
    pdf.conteudo(contexto, imagens)
    for arquivo in q.get("files") or []:
        if arquivo not in contexto:
            pdf.imagem(imagens.get(arquivo), arquivo)
    def altura_alternativa(alt: dict[str, Any]) -> float:
        estilo = "B" if resolvida and alt["letter"] == q["correctAlternative"] else ""
        tamanho_img = pdf.tamanho_imagem(imagens.get(alt["file"]), recuo=7) if alt.get("file") else None
        return (pdf.altura_paragrafo(alt.get("text") or "", estilo=estilo, recuo=7) or 5) + (
            tamanho_img[1] + 2 if tamanho_img else 0
        ) + 0.8

    introducao = q.get("alternativesIntroduction") or ""
    altura_intro = pdf.altura_paragrafo(introducao, estilo="B") + (1 if introducao else 0)
    alturas = [altura_alternativa(alt) for alt in q["alternatives"]]
    # Pergunta + alternativas ficam juntas na mesma página sempre que couberem em meia página.
    if altura_intro + sum(alturas) < pdf.eph * 0.5:
        pdf.garantir_espaco(altura_intro + sum(alturas))
    elif introducao:
        pdf.garantir_espaco(altura_intro + alturas[0])
    if introducao:
        pdf.paragrafo(introducao, estilo="B")
        pdf.ln(1)

    for alt, necessario in zip(q["alternatives"], alturas):
        destacar = resolvida and alt["letter"] == q["correctAlternative"]
        estilo = "B" if destacar else ""
        texto = alt.get("text") or ""
        # A letra só é escrita depois de garantir que a alternativa inteira cabe na página.
        pdf.garantir_espaco(necessario)

        if destacar:
            pdf.set_text_color(*COR_CORRETA)
            pdf.set_fill_color(*FUNDO_CORRETA)
        pdf.set_font(pdf.fonte, "B", 10)
        pdf.cell(7, 5, f"{alt['letter']})", fill=destacar)
        if texto:
            pdf.paragrafo(texto, estilo=estilo, recuo=7, fundo=destacar)
        else:
            pdf.ln(5)
        if alt.get("file"):
            pdf.imagem(imagens.get(alt["file"]), alt["file"], recuo=7)
        pdf.set_text_color(0)
        pdf.ln(0.8)

    if resolvida:
        pdf.ln(2)
        blocos = [b for b in re.split(r"\n\s*\n", explicacao or "") if b.strip()]
        # Caixa da resposta + título ficam sempre junto do início da explicação.
        cabecalho = 7 + (5 if blocos else 0)
        if blocos:
            pdf.reservar_texto(cabecalho + pdf.altura_paragrafo(blocos[0]), inicio=cabecalho + 15)
        else:
            pdf.garantir_espaco(cabecalho)
        pdf.set_fill_color(*FUNDO_CORRETA)
        pdf.set_text_color(*COR_CORRETA)
        pdf.set_font(pdf.fonte, "B", 11)
        pdf.cell(0, 7, pdf.texto(f"Questão {numero} — resposta correta: {q['correctAlternative']}"), fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0)
        if blocos:
            pdf.set_fill_color(245, 245, 245)
            pdf.paragrafo("Por que essa é a resposta:", estilo="B", fundo=True)
            for i, bloco in enumerate(blocos):
                if i:
                    pdf.reservar_texto(pdf.altura_paragrafo(bloco))
                pdf.paragrafo(bloco, fundo=True)
    pdf.ln(5)


def _grade(pdf: _Pdf, textos: list[str], colunas: int = 3) -> None:
    pdf.set_font(pdf.fonte, "", 10)
    largura = pdf.epw / colunas
    for i, texto in enumerate(textos, start=1):
        quebra = i % colunas == 0 or i == len(textos)
        pdf.cell(
            largura, 7, pdf.texto(texto),
            new_x=XPos.LMARGIN if quebra else XPos.RIGHT, new_y=YPos.NEXT if quebra else YPos.TOP,
        )


def gerar_pdf_simulado(
    destino: Path,
    titulo: str,
    descricao: list[str],
    questoes: list[dict[str, Any]],
    imagens: dict[str, bytes | None],
    incluir_resolucao: bool,
    explicacoes: list[str | None] | None = None,
) -> Path:
    explicacoes = explicacoes or []
    pdf = _Pdf(rodape=titulo)
    pdf.set_title(titulo)
    pdf.set_author("mcp-server-enem (dados: enem.dev)")
    pdf.add_page()

    pdf.paragrafo(titulo, tamanho=18, estilo="B", altura=9)
    for linha in descricao:
        pdf.paragrafo(linha, tamanho=10, altura=5)
    pdf.ln(2)
    pdf.set_draw_color(180)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)

    for i, q in enumerate(questoes, start=1):
        _escrever_questao(pdf, i, q, imagens)

    pdf.add_page()
    pdf.paragrafo("Folha de respostas", tamanho=14, estilo="B", altura=8)
    pdf.ln(2)
    _grade(pdf, [f"{i:>2}.  (A)  (B)  (C)  (D)  (E)" for i in range(1, len(questoes) + 1)])

    if incluir_resolucao:
        pdf.add_page()
        pdf.set_y(pdf.h / 2 - 25)
        pdf.set_font(pdf.fonte, "B", 44)
        pdf.cell(0, 20, "GABARITO ABAIXO", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font(pdf.fonte, "", 12)
        pdf.set_text_color(110)
        pdf.cell(0, 10, pdf.texto("Termine o simulado antes de virar a página."), align="C")
        pdf.set_text_color(0)

        pdf.add_page()
        pdf.paragrafo("Gabarito rápido", tamanho=14, estilo="B", altura=8)
        pdf.ln(2)
        _grade(pdf, [f"{i:>2}. {q['correctAlternative']}" for i, q in enumerate(questoes, start=1)], colunas=6)
        pdf.ln(6)
        pdf.paragrafo("Questões resolvidas", tamanho=14, estilo="B", altura=8)
        pdf.ln(2)
        for i, q in enumerate(questoes, start=1):
            explicacao = explicacoes[i - 1] if i <= len(explicacoes) else None
            _escrever_questao(pdf, i, q, imagens, resolvida=True, explicacao=explicacao)

    destino.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(destino))
    return destino
