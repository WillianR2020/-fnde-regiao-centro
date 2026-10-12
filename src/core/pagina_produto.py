"""Preço e frete lidos na página do produto, pelo navegador.

É o padrão da etapa 3 para todo executor que não tem API de ficha e de
frete -- hoje, todos menos o VTEX. Lê a mesma página que o print
fotografa, então o preço da planilha e o da imagem são o mesmo.

Duas perguntas, duas funções:

  detalhar()     preço, preço riscado e estoque. JSON-LD primeiro
                 (dado limpo que a própria loja publica), depois as
                 metatags de produto, depois o texto da área de compra.
  cotar_frete()  preenche o CEP, espera, e lê o que apareceu de novo
                 na página com "R$" ou "grátis".

A conversão do que o JavaScript devolve é função pura
(`produto_da_ficha`, `frete_do_texto`) e é o que os testes exercitam.
"""

from __future__ import annotations

import re

from src.core.frete import FRETE_A_COLETAR, SEM_ENTREGA
from src.core.http import FalhaDeRede, RespostaInvalida, SiteBloqueado
from src.core.log import obter
from src.models import ProdutoBruto

log = obter(__name__)

TEMPO_NAVEGACAO_MS = 45000

# Milhar com ponto em grupos de 3, ou sem separador. "R$ 17.9117,91" é
# texto de dois spans colados (Dutra: preço cheio e preço PIX) e não é
# número nenhum -- o padrão antigo lia 179.117,91.
VALOR = r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})"
PRECO = re.compile(r"R\$\s*" + VALOR)
# "De R$ 56,80 / R$ 44,55": o primeiro valor do bloco é o riscado. Loja
# que não usa <del> (PreveOeste, Dutra) só se distingue por esse "De".
# "A partir de R$" é preço de verdade e fica.
DE_RISCADO = re.compile(r"(?<!partir )\bde:?\s*R\$\s*" + VALOR, re.IGNORECASE)
# "economize R$ 1,99" é a diferença, não o preço.
ECONOMIA = re.compile(r"\b(?:economi\w*|poupe)\s*(?:de\s*)?:?\s*R\$\s*" + VALOR, re.IGNORECASE)
# "Por R$ 17,91": quando a loja escreve, é o preço de venda.
POR = re.compile(r"\bpor:?\s*R\$\s*" + VALOR, re.IGNORECASE)

# Frase de loja que respondeu e recusou o CEP. Vira o marcador de
# core.frete, que é o que separa recusa de "não consegui cotar".
FRASES_RECUSA = (
    "nao entregamos", "nao realizamos entrega", "nao ha opcoes de entrega",
    "nenhuma opcao de entrega", "nao atendemos", "fora da area de entrega",
    "indisponivel para este cep", "nao disponivel para este cep",
    "cep nao atendido", "nao foram encontradas formas de envio",
    "nenhuma forma de envio", "nao entrega para", "nao encontramos frete",
    "nao ha frete disponivel",
)

# Uma linha só conta como opção de frete se tiver cara de entrega. Sem
# isto, qualquer "R$" que surgisse depois do CEP valia: numa loja real o
# filtro lateral "de R$ 0,00 até R$ 24,99" virou frete grátis.
RE_ENVIO = re.compile(
    r"frete|entrega|envio|sedex|\bpac\b|correios|jadlog|transportadora|"
    r"expresso|express|motoboy|loggi|braspress|azul cargo|economic|"
    r"previsto|prazo|\bdias?\b",
)
# Não é entrega: filtro de preço, parcela, desconto à vista.
RE_NAO_ENVIO = re.compile(r"filtrar|ate r\$|parcela|\b\d+x\b|juros|desconto|\bpix\b|boleto")
RE_RETIRADA = re.compile(r"retir")

# Preço escondido atrás de login/cadastro. Regra da pesquisa: site que
# exige cadastro para mostrar preço é ignorado -- é obrigatório o preço
# estar aberto. Duas distribuidoras reais de GO/DF mostram exatamente
# "Faça seu login ou cadastre-se para ver preços e comprar".
RE_PRECO_COM_CADASTRO = re.compile(
    r"(login|logar|cadastr\w*|entrar|entre|acesse)\W+(?:\w+\W+){0,6}?"
    r"(ver|veja|visualizar|consultar|exibir)\W+(?:\w+\W+){0,2}?(o |os )?preco"
)


def preco_exige_cadastro(texto: str) -> bool:
    """A página diz que o preço só aparece para quem tem login/cadastro?"""
    return bool(RE_PRECO_COM_CADASTRO.search(" ".join(_sem_acento(texto).split())))

FICHA = """() => {
  const ler = (sel) => {
    const e = document.querySelector(sel);
    return e ? (e.getAttribute('content') || e.getAttribute('value') ||
                e.textContent || '').trim() : '';
  };
  const jsonld = [];
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { jsonld.push(JSON.parse(s.textContent)); } catch (e) {}
  }
  const h1 = document.querySelector('h1');
  // Cabeçalho, menu e mini-carrinho têm "R$ 0,00" e classe "price"
  // como qualquer ficha. Numa loja Woo real o carrinho vazio do topo
  // virou o preço do produto.
  const fora = 'header,nav,footer,[class*="cart" i],[class*="carrinho" i],' +
               '[class*="menu" i],[class*="related" i],[class*="relacionad" i]';
  let area = document.querySelector(
    '.summary, .product-info, .product-main, .product-essential, ' +
    '[class*="product-info" i], [class*="product__info" i], [class*="detalhe" i]');
  if (!area && h1) {
    area = h1;
    for (let i = 0; i < 3 && area.parentElement; i++) area = area.parentElement;
  }
  // Tema que separa título/descrição do bloco de preço: a área achada
  // não tem preço nenhum e a ficha saía "sem preço" com R$ 149,00 na
  // tela (Casa da Limpeza, Woo). Sobe até 4 níveis atrás do preço.
  const temPreco = (e) => /R\\$\\s*\\d/.test(e.innerText || '');
  if (area && !temPreco(area)) {
    let acima = area;
    for (let i = 0; i < 4 && acima.parentElement && acima.parentElement !== document.body; i++) {
      acima = acima.parentElement;
      if (temPreco(acima)) { area = acima; break; }
    }
  }
  area = area || document.body;
  const texto = (sel) => {
    for (const e of area.querySelectorAll(sel)) {
      if (e.closest(fora)) continue;
      const t = e.innerText.trim();
      if (t) return t;
    }
    return '';
  };
  return {
    jsonld,
    titulo: h1 ? h1.innerText.trim() : document.title,
    meta_preco: ler('meta[property="product:price:amount"]') ||
                ler('meta[itemprop="price"]') || ler('[itemprop="price"]'),
    meta_estoque: ler('meta[property="product:availability"]') ||
                  ler('[itemprop="availability"]'),
    preco_riscado: texto('del'),
    preco_atual: texto('ins') || texto('[class*="price" i], [class*="preco" i]'),
    texto_area: (area.innerText || '').slice(0, 4000),
  };
}"""

TEXTO = "() => document.body ? document.body.innerText : ''"


# ---------------------------------------------------------------------------
# Funções puras
# ---------------------------------------------------------------------------

def _numero(valor) -> float | None:
    """'1.234,56', 'R$ 10,67', '10.67' e 10.67 -> float."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor) if valor > 0 else None
    texto = str(valor).strip()
    # Preço zero é campo vazio, não produto de graça: é o que a loja
    # mostra no carrinho vazio e no produto "sob consulta".
    for achado in PRECO.findall(texto):
        valor = float(achado.replace(".", "").replace(",", "."))
        if valor > 0:
            return valor
    texto = texto.replace("R$", "").strip()
    try:
        valor = float(texto)
    except ValueError:
        return None
    return valor if valor > 0 else None


def _preco_por(texto) -> float | None:
    achado = POR.search(str(texto or ""))
    return _numero("R$ " + achado.group(1)) if achado else None


def _preco_de_venda(texto) -> float | None:
    """Primeiro preço do texto que não é o "De R$" riscado nem a economia."""
    if texto is None:
        return None
    return _numero(ECONOMIA.sub(" ", DE_RISCADO.sub(" ", str(texto))))


def _riscado_do_texto(texto) -> float | None:
    achado = DE_RISCADO.search(str(texto or ""))
    return _numero("R$ " + achado.group(1)) if achado else None


def _sem_acento(texto: str) -> str:
    from unidecode import unidecode

    return unidecode(str(texto or "")).lower()


def _nos_produto(dados) -> list[dict]:
    """Todos os nós Product do JSON-LD, inclusive dentro de @graph."""
    fila = list(dados) if isinstance(dados, list) else [dados]
    saida = []
    while fila:
        no = fila.pop(0)
        if isinstance(no, list):
            fila.extend(no)
            continue
        if not isinstance(no, dict):
            continue
        if "@graph" in no:
            fila.extend(no["@graph"] if isinstance(no["@graph"], list) else [no["@graph"]])
        tipo = no.get("@type")
        tipos = tipo if isinstance(tipo, list) else [tipo]
        if "Product" in tipos:
            saida.append(no)
    return saida


def _oferta(produto: dict) -> tuple[float | None, bool | None]:
    """(preço, em estoque) da primeira oferta com preço."""
    ofertas = produto.get("offers") or []
    ofertas = ofertas if isinstance(ofertas, list) else [ofertas]
    for oferta in ofertas:
        if not isinstance(oferta, dict):
            continue
        preco = _numero(oferta.get("price"))
        if preco is None:
            preco = _numero(oferta.get("lowPrice"))
        if preco is None:
            especificacao = oferta.get("priceSpecification")
            especificacoes = (especificacao if isinstance(especificacao, list)
                              else [especificacao])
            for e in especificacoes:
                if isinstance(e, dict) and _numero(e.get("price")) is not None:
                    preco = _numero(e.get("price"))
                    break
        if preco is None:
            continue
        estoque = str(oferta.get("availability") or "")
        disponivel = None
        if estoque:
            disponivel = "outofstock" not in estoque.lower().replace("_", "")
        return preco, disponivel
    return None, None


def _estoque_do_texto(texto: str) -> bool | None:
    t = _sem_acento(texto)
    if any(f in t for f in ("fora de estoque", "esgotado", "indisponivel",
                            "sem estoque", "out of stock", "avise-me")):
        return False
    if any(f in t for f in ("em estoque", "comprar", "adicionar ao carrinho")):
        return True
    return None


def produto_da_ficha(ficha: dict, url: str) -> ProdutoBruto:
    """O que o JavaScript `FICHA` devolveu -> ProdutoBruto.

    Levanta RespostaInvalida se a página não mostra preço nenhum: linha
    sem preço não serve para a entrega, e um None calado viraria
    "preço zero" mais adiante.
    """
    titulo = (ficha.get("titulo") or "").strip()
    preco = disponivel = None

    if preco_exige_cadastro(ficha.get("texto_area") or ""):
        raise RespostaInvalida(f"preco so com login/cadastro (site ignorado): {url}", url)

    for no in _nos_produto(ficha.get("jsonld") or []):
        preco, disponivel = _oferta(no)
        titulo = titulo or (no.get("name") or "").strip()
        if preco is not None:
            break

    if preco is None:
        preco = _numero(ficha.get("meta_preco"))
    if disponivel is None and ficha.get("meta_estoque"):
        disponivel = "out" not in _sem_acento(ficha["meta_estoque"]).replace(" ", "")
    if preco is None:
        # "Por R$" vale mais que a ordem dos valores; a área inteira vem
        # antes porque o bloco de preço às vezes cola dois spans.
        preco = _preco_por(ficha.get("texto_area")) or _preco_por(ficha.get("preco_atual"))
    if preco is None:
        preco = _preco_de_venda(ficha.get("preco_atual"))
    if preco is None:
        preco = _preco_de_venda(ficha.get("texto_area"))
    if preco is None:
        raise RespostaInvalida(f"pagina sem preco: {url}", url)

    if disponivel is None:
        disponivel = _estoque_do_texto(ficha.get("texto_area") or "")

    riscado = (_numero(ficha.get("preco_riscado"))
               or _riscado_do_texto(ficha.get("preco_atual"))
               or _riscado_do_texto(ficha.get("texto_area")))
    if riscado is not None and riscado <= preco:
        riscado = None

    return ProdutoBruto(
        titulo=titulo,
        url=url,
        preco=preco,
        disponivel=True if disponivel is None else disponivel,
        preco_lista=riscado,
    )


def frete_do_texto(antes: str, depois: str) -> tuple[float | None, str]:
    """Compara a página antes e depois do CEP e devolve (valor, observação).

    Só conta linha que apareceu depois do CEP: preço do produto, parcela
    e "frete grátis acima de R$ 299" do banner já estavam lá antes.
    Ficam as opções de entrega, e vale a mais barata.
    """
    vistas = {linha.strip() for linha in antes.splitlines()}
    novas = [
        linha.strip() for linha in depois.splitlines()
        if linha.strip() and linha.strip() not in vistas and len(linha.strip()) < 200
    ]
    texto_novo = _sem_acento(" ".join(novas))

    if any(f in texto_novo for f in FRASES_RECUSA):
        return None, f"{SEM_ENTREGA} para este CEP"

    opcoes: list[tuple[float, str]] = []
    so_retirada = False
    for i, linha in enumerate(novas):
        # A linha do nome da modalidade e a do valor costumam vir
        # separadas ("PAC" / "R$ 32,10"); a observação leva as duas.
        # A Gimba quebra em quatro ("Envio:" / "Transportadora" / "Valor:"
        # / "R$ 77,36"): sobe até três linhas curtas, parando na primeira
        # que já tem valor próprio -- essa é outra opção.
        acima = []
        for anterior in reversed(novas[max(0, i - 3):i]):
            if len(anterior) >= 60 or PRECO.search(anterior):
                break
            acima.insert(0, anterior)
        rotulo = " ".join(acima + [linha])
        # A linha decide sozinha quando tem texto; as de cima só entram
        # quando esta é o valor puro. Senão "Retirar na loja" contamina o
        # PAC logo abaixo.
        tem_texto = re.search(r"[a-z]{3}", _sem_acento(linha))
        rotulo = linha if tem_texto else rotulo
        sem = _sem_acento(rotulo)
        valores = PRECO.findall(linha)
        gratis = "gratis" in _sem_acento(linha) and not valores
        if not (valores or gratis):
            continue
        if not RE_ENVIO.search(sem) or RE_NAO_ENVIO.search(sem):
            continue
        # Retirar no balcão em Santo André não leva a caixa a Goiânia.
        if RE_RETIRADA.search(sem):
            so_retirada = True
            continue
        if gratis:
            opcoes.append((0.0, rotulo))
        for valor in valores:
            opcoes.append((float(valor.replace(".", "").replace(",", ".")), rotulo))

    if not opcoes and so_retirada:
        return None, f"{SEM_ENTREGA} para este CEP (so retirada na loja)"
    if not opcoes:
        return None, ""
    valor, rotulo = min(opcoes, key=lambda o: o[0])
    rotulo = re.sub(r"\s+", " ", rotulo)[:150]
    if valor == 0:
        return 0.0, f"frete gratis ({rotulo})"
    return valor, rotulo


# ---------------------------------------------------------------------------
# Navegador
# ---------------------------------------------------------------------------

def _abrir(url: str):
    from src.export.captura import _aceitar_cookies, escolher_variacoes, novo_contexto

    contexto = novo_contexto()
    pagina = contexto.new_page()
    try:
        resposta = pagina.goto(url, wait_until="domcontentloaded",
                               timeout=TEMPO_NAVEGACAO_MS)
    except Exception as e:
        contexto.close()
        raise FalhaDeRede(f"pagina nao abriu: {type(e).__name__}: {e}", url) from e
    status = resposta.status if resposta else 0
    if status in (401, 403, 407, 429):
        contexto.close()
        raise SiteBloqueado(f"status {status} na pagina do produto", url)
    if status >= 400:
        contexto.close()
        raise FalhaDeRede(f"status {status} na pagina do produto", url)
    try:
        pagina.wait_for_load_state("networkidle", timeout=10000)
    except Exception:  # loja com polling eterno: segue com o que tem
        pass
    _aceitar_cookies(pagina)
    escolher_variacoes(pagina)
    return contexto, pagina


def detalhar(url: str) -> ProdutoBruto:
    contexto, pagina = _abrir(url)
    try:
        ficha = pagina.evaluate(FICHA)
    finally:
        contexto.close()
    return produto_da_ficha(ficha, url)


def cotar_frete(url: str, cep: str, seletor_cep: str = "",
                seletor_resultado: str = "") -> tuple[float | None, str]:
    from src.export.captura import (ESPERA_CARRINHO_MS, ESPERA_MAX_FRETE_MS,
                                    abrir_carrinho_se_preciso, preencher_e_esperar_frete)

    contexto, pagina = _abrir(url)
    try:
        try:
            carrinho = abrir_carrinho_se_preciso(pagina, url)
        except Exception as e:
            return None, f"carrinho nao abriu para cotar o frete ({type(e).__name__}); frete sob consulta"
        preenchido, antes, depois = preencher_e_esperar_frete(
            pagina, cep, seletor_cep, seletor_resultado,
            espera_ms=ESPERA_CARRINHO_MS if carrinho else ESPERA_MAX_FRETE_MS,
        )
    finally:
        contexto.close()

    if not preenchido:
        return None, "pagina sem calculo de frete por CEP; frete sob consulta"
    valor, obs = frete_do_texto(antes, depois)
    if valor is None and not obs:
        # A página TEM calculadora (o CEP entrou), só o valor não foi
        # lido: uma pessoa coleta à mão, e a linha fica marcada para isso.
        return None, (f"{FRETE_A_COLETAR}: a pagina tem calculadora de frete, "
                      "mas o valor nao foi lido automaticamente")
    return valor, obs
