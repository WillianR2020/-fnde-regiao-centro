"""Print da página do produto, com o CEP preenchido e o frete visível.

Por que isto não mora no adapter: o print é requisito de TODOS os
registros, inclusive dos sites de plataforma com API (VTEX responde
preço e frete em JSON, mas o solicitante não confere JSON). Sem um
capturador comum, cada adapter teria que subir o seu browser e a regra
de "uma sessão por site" morreria.

O navegador é um só por processo, criado na primeira chamada e
reaproveitado. Como o orquestrador roda um processo por domínio, isso já
significa um navegador por site — que é a regra do projeto.

O preenchimento de CEP é uma heurística: procura o campo pelos nomes
usuais e digita. Site que não responder a ela precisa sobrescrever
`SEL_CAMPO_CEP` no seu adapter — é para isso que o campo existe lá.
"""

from __future__ import annotations

import atexit
from pathlib import Path

from src.core.log import obter

log = obter(__name__)

# Ordem de tentativa. O primeiro que existir e for visível é usado.
SELETORES_CEP = (
    "input[name*='cep' i]",
    "input[id*='cep' i]",
    "input[placeholder*='cep' i]",
    "input[data-testid*='cep' i]",
    "input[name*='postal' i]",
    # Tray: name="number-frete", class="crazy_cep", placeholder="00000-000".
    "input[class*='cep' i]",
    "input[name*='frete' i]",
    "input[placeholder*='00000-000']",
    # Nuvemshop: name="zipcode".
    "input[name*='zip' i]",
)

# Loja que só cota frete no carrinho: a ficha não tem campo de CEP, e
# sem cotação a sede fora de GO não entra. Para estas, cotação e print
# põem o produto num carrinho de visitante (sem conta, sem cadastro) e
# preenchem o CEP lá; o print do carrinho mostra produto, preço e frete
# juntos. Só entra loja conferida à mão -- clicar em comprar continua
# proibido em qualquer outra.
#   dominio -> (botão de adicionar na ficha, caminho do carrinho)
FRETE_NO_CARRINHO = {
    # Magento (Hyvä); o carrinho leva ~10 s para trazer a transportadora.
    "shoppingdaseguranca.com.br": ("#product-addtocart-button", "/checkout/cart/"),
}

ESPERA_MAX_FRETE_MS = 15000
ESPERA_CARRINHO_MS = 40000  # a transportadora do carrinho Magento chegou em ~20 s
ESPERA_ENTER_S = 5
LARGURA, ALTURA = 1366, 900

_navegador = None
_playwright = None


class PrintIndisponivel(RuntimeError):
    """Não deu para tirar o print. O registro fica incompleto de propósito."""


def obter_navegador():
    """Um browser por processo, aberto na primeira necessidade.

    Público porque a varredura genérica (`src/adapters/
    generico_playwright.py`) usa o MESMO navegador: dois navegadores no
    mesmo processo quebrariam a regra de um por domínio, e o print sairia
    de uma sessão diferente da que fez a busca.
    """
    global _navegador, _playwright

    if _navegador is not None:
        return _navegador

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depende do ambiente
        raise PrintIndisponivel(
            "playwright nao esta instalado. Rode: pip install playwright && "
            "playwright install chromium"
        ) from e

    _playwright = sync_playwright().start()
    try:
        _navegador = _playwright.chromium.launch(headless=True)
    except Exception as e:  # pragma: no cover - depende do ambiente
        raise PrintIndisponivel(
            f"nao consegui subir o chromium ({e}). Rode: playwright install chromium"
        ) from e

    atexit.register(fechar)
    return _navegador


def novo_contexto():
    """Contexto do navegador com o user-agent de um Chrome comum.

    O Chromium headless se anuncia como "HeadlessChrome", e há loja que
    responde 403 só por isso -- duas distribuidoras reais (Goiânia e
    Taguatinga) barravam o navegador e atendiam o curl. É o mesmo
    user-agent do cliente HTTP, então a loja vê o mesmo visitante nas
    duas portas.

    Todo contexto do projeto sai daqui: busca, ficha e print.
    """
    from src.core.http import UA

    return obter_navegador().new_context(
        viewport={"width": LARGURA, "height": ALTURA},
        locale="pt-BR",
        user_agent=UA,
    )


def fechar() -> None:
    """Fecha browser e playwright. Chamado no fim do processo."""
    global _navegador, _playwright
    try:
        if _navegador is not None:
            _navegador.close()
    finally:
        _navegador = None
    try:
        if _playwright is not None:
            _playwright.stop()
    finally:
        _playwright = None


def capturar(
    url: str,
    cep: str,
    destino: str | Path,
    seletor_cep: str = "",
    seletor_resultado: str = "",
) -> Path:
    """Abre a página, preenche o CEP, espera o frete e salva o PNG.

    `cep` vazio tira o print da página como ela abre, sem frete: é o caso
    do fornecedor com sede em GO, que não tem frete calculado.

    Devolve o caminho do arquivo. Levanta PrintIndisponivel se não deu —
    nunca devolve um caminho para um arquivo que não existe, porque a
    retomada usa a existência do arquivo como prova de que o registro
    ficou completo.
    """
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)

    contexto = novo_contexto()
    pagina = contexto.new_page()

    try:
        pagina.goto(url, wait_until="domcontentloaded", timeout=45000)
        _aceitar_cookies(pagina)
        escolher_variacoes(pagina)

        if cep:
            carrinho = abrir_carrinho_se_preciso(pagina, url)
            espera = ESPERA_CARRINHO_MS if carrinho else ESPERA_MAX_FRETE_MS
            preenchido, antes, depois = preencher_e_esperar_frete(
                pagina, cep, seletor_cep, seletor_resultado, espera_ms=espera)
            if not preenchido:
                log.warning("nao achei campo de CEP em %s; print sai sem frete", url)
            elif not _frete_apareceu(antes, depois):
                # A cotação da planilha pode ter vindo de outra abertura
                # (ou da API): o print tem que mostrar o frete também. Loja
                # que ainda montava a página apagou o CEP digitado (LF).
                log.info("frete nao apareceu no print de %s; tentando de novo", url)
                pagina.wait_for_timeout(3000)
                _, antes2, depois2 = preencher_e_esperar_frete(
                    pagina, cep, seletor_cep, seletor_resultado, espera_ms=espera)
                if not _frete_apareceu(antes2, depois2) and not _frete_apareceu(antes, depois2):
                    log.warning("print de %s sai sem o frete na tela", url)
        else:
            try:
                pagina.wait_for_load_state("networkidle", timeout=10000)
            except Exception:  # loja com polling eterno: fotografa o que tem
                pass

        limpar_tela(pagina)
        pagina.screenshot(path=str(destino), full_page=True)
    except PrintIndisponivel:
        raise
    except Exception as e:
        raise PrintIndisponivel(f"print falhou em {url}: {type(e).__name__}: {e}") from e
    finally:
        pagina.close()
        contexto.close()

    if not destino.exists() or destino.stat().st_size == 0:
        raise PrintIndisponivel(f"print saiu vazio: {destino}")

    return destino


def _frete_apareceu(antes: str, depois: str) -> bool:
    from src.core.pagina_produto import frete_do_texto

    valor, obs = frete_do_texto(antes, depois)
    return valor is not None or bool(obs)


def abrir_carrinho_se_preciso(pagina, url: str) -> bool:
    """Loja de FRETE_NO_CARRINHO: põe o produto no carrinho e vai para lá.

    Devolve False (e não mexe na página) para qualquer outra loja.
    """
    from urllib.parse import urlparse

    partes = urlparse(url)
    conf = FRETE_NO_CARRINHO.get(partes.netloc.lower().removeprefix("www."))
    if conf is None:
        return False
    botao, caminho = conf
    pagina.locator(botao).first.click(timeout=15000)
    pagina.wait_for_timeout(5000)
    pagina.goto(f"{partes.scheme}://{partes.netloc}{caminho}",
                wait_until="domcontentloaded", timeout=45000)
    try:
        pagina.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    return True


def preencher_e_esperar_frete(
    pagina,
    cep: str,
    seletor_cep: str = "",
    seletor_resultado: str = "",
    espera_ms: int = ESPERA_MAX_FRETE_MS,
) -> tuple[bool, str, str]:
    """Preenche o CEP, pede o cálculo e espera a cotação aparecer.

    Devolve (preenchido, texto antes, texto depois). É a mesma rotina
    para a cotação e para o print: se cada um esperasse do seu jeito, o
    print poderia sair antes do frete que a planilha registrou.

    A espera é ativa -- olha a página a cada segundo até surgir uma
    linha nova com R$, "grátis" ou recusa -- porque espera fixa errava
    para os dois lados: numa loja Tray real, 6 s bastaram para GO e não
    para DF na mesma página.
    """
    from src.core.pagina_produto import TEXTO, frete_do_texto

    antes = pagina.evaluate(TEXTO)
    campo = _preencher_cep(pagina, cep, seletor_cep)
    if campo is None:
        # Loja VTEX IO (LF Máquinas) só monta o simulador de frete depois
        # de a página terminar de carregar.
        try:
            pagina.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        pagina.wait_for_timeout(2000)
        antes = pagina.evaluate(TEXTO)
        campo = _preencher_cep(pagina, cep, seletor_cep)
    if campo is None:
        return False, antes, antes

    if seletor_resultado:
        try:
            pagina.wait_for_selector(seletor_resultado, timeout=espera_ms)
        except Exception:
            log.warning("resultado de frete nao apareceu (%s)", seletor_resultado)
        return True, antes, pagina.evaluate(TEXTO)

    depois = antes
    for segundo in range(espera_ms // 1000):
        if segundo == ESPERA_ENTER_S:
            # O botão não trouxe nada: há tema que só calcula no Enter.
            # Fica para depois porque, noutros, o Enter submete o
            # formulário errado e leva a página embora.
            try:
                campo.press("Enter")
            except Exception:
                pass
        pagina.wait_for_timeout(1000)
        depois = pagina.evaluate(TEXTO)
        valor, obs = frete_do_texto(antes, depois)
        if valor is not None or obs:
            # A primeira opção apareceu; as outras costumam vir logo atrás.
            pagina.wait_for_timeout(1500)
            return True, antes, pagina.evaluate(TEXTO)
    return True, antes, depois


# O botão de calcular é procurado SÓ em volta do campo de CEP, subindo
# até três níveis. Procurar na página inteira clicava no "Buscar" do
# cabeçalho, e um Enter mal direcionado numa loja Loja Integrada real
# submeteu o formulário errado e abriu a página de busca.
#
# Botão com rótulo ("Calcular", "OK") ganha de botão só com ícone: numa
# loja Tray real o primeiro botão sem texto do bloco não era o de frete.
#
# Tray antiga ("commercesuite"): o botão é um link `a.botao-simular-frete`
# e o CEP fica dentro do formulário de compra. Sem reconhecer a classe, a
# reserva "submit do mesmo formulário" clicava em Comprar e punha o
# produto no carrinho. Botão de compra nunca é reserva.
BOTAO_DO_CAMPO = """(campo) => {
  const rotulo = /^(calcular|consultar|ok|simular|buscar|calcular frete)$/i;
  const deFrete = /frete|simular|calcul/i;
  const deCompra = /comprar|carrinho|adicionar|sacola|cesta/i;
  const texto = (b) => (b.innerText || b.value || '').trim();
  let no = campo.parentElement;
  for (let i = 0; i < 3 && no; i++, no = no.parentElement) {
    const botoes = [...no.querySelectorAll(
      'button, input[type=submit], input[type=button], a[class*="btn" i], ' +
      'a[class*="botao" i], a[class*="frete" i], a[class*="simular" i], a[class*="calcul" i]')]
      .filter(b => b !== campo && b.offsetParent !== null)
      .filter(b => !deCompra.test(texto(b) + ' ' + (b.className || '') + ' ' + (b.id || '')));
    const botao = botoes.find(b => rotulo.test(texto(b))) ||
                  botoes.find(b => deFrete.test((b.className || '') + ' ' + (b.id || ''))) ||
                  botoes.find(b => !texto(b) && (b.type === 'submit' || b.closest('form') === campo.closest('form') && campo.closest('form')));
    if (botao) { botao.click(); return true; }
  }
  return false;
}"""

# CEP em dois campos (5 + 3 dígitos), lado a lado: devolve o segundo.
SEGUNDO_CAMPO_CEP = """(campo) => {
  if (campo.maxLength !== 5) return null;
  const irmaos = [...campo.parentElement.querySelectorAll('input')];
  const seguinte = irmaos[irmaos.indexOf(campo) + 1];
  return seguinte && seguinte.maxLength === 3 ? true : null;
}"""


def _digitar_conferindo(campo, digitos: str) -> None:
    """Digita o CEP e confere se ele ficou inteiro no campo.

    Campo com máscara que ainda está montando engole as primeiras teclas:
    na Distribuidora Cauê o campo ficava "7493_-___" e o print saía sem
    frete. Tenta de novo, mais devagar, até duas vezes.
    """
    for atraso in (40, 120, 250):
        campo.fill("", timeout=5000)
        campo.click(timeout=3000)
        campo.press_sequentially(digitos, delay=atraso, timeout=15000)
        try:
            lido = "".join(c for c in campo.input_value(timeout=2000) if c.isdigit())
        except Exception:
            return
        if lido == digitos:
            return
        log.info("CEP ficou '%s' no campo; digitando de novo mais devagar", lido)


def _preencher_cep(pagina, cep: str, seletor_cep: str = ""):
    """Preenche o primeiro campo de CEP VISÍVEL e pede o cálculo.

    Visível, e não o primeiro: numa loja Nuvemshop real o primeiro
    `zipcode` da página é o do carrinho lateral, escondido, e o da ficha
    vinha depois. Devolve o campo preenchido, ou None.
    """
    seletores = (seletor_cep,) + SELETORES_CEP if seletor_cep else SELETORES_CEP
    for seletor in seletores:
        try:
            candidatos = pagina.locator(seletor)
            for n in range(min(candidatos.count(), 6)):
                campo = candidatos.nth(n)
                if not campo.is_visible():
                    continue
                campo.scroll_into_view_if_needed(timeout=3000)
                # Tecla a tecla, e não fill(): campo com máscara de CEP
                # (Tray, "crazy_cep") só registra o valor nos eventos de
                # teclado, e o botão calculava com o campo "vazio".
                campo.fill("", timeout=5000)
                digitos = "".join(c for c in cep if c.isdigit())
                if campo.evaluate(SEGUNDO_CAMPO_CEP):
                    segundo = campo.locator("xpath=following-sibling::input[1]")
                    campo.press_sequentially(digitos[:5], delay=40, timeout=10000)
                    segundo.fill("", timeout=5000)
                    segundo.press_sequentially(digitos[5:], delay=40, timeout=10000)
                else:
                    _digitar_conferindo(campo, digitos)
                # Campo com debounce (Alpine/Hyvä) só passa o valor adiante
                # um instante depois da última tecla; o clique imediato
                # calculava com o CEP vazio e só trazia "retirada".
                pagina.wait_for_timeout(800)
                if not campo.evaluate(BOTAO_DO_CAMPO):
                    campo.press("Enter")
                return campo
        except Exception:  # noqa: PERF203 - seletor que não serve, tenta o próximo
            continue
    return None


def escolher_variacoes(pagina) -> int:
    """Escolhe a primeira opção disponível de cada grade de variação (Loja Integrada).

    Produto com grade (cor, espessura, quantidade) não calcula frete
    antes da escolha -- "Antes de calcular o frete é necessário escolher
    as opções do produto" -- e mostra "A partir de R$ ..." no lugar do
    preço. Na Casa do Saco de Lixo, 36 itens aceitos ficavam com o frete
    a coletar à mão por isso. A escolha é a primeira opção disponível: o
    print mostra qual foi, e o preço lido é o dela.

    Devolve quantas grades foram escolhidas (0 = a página não tem grade).
    """
    try:
        grades = pagina.evaluate("""() => [...new Set(
            [...document.querySelectorAll('a.atributo-item[data-grade-id]')]
              .map(a => a.dataset.gradeId))]""")
    except Exception:
        return 0
    escolhidas = 0
    for grade in grades:
        opcoes = pagina.locator(
            f"a.atributo-item[data-grade-id='{grade}']"
            ":not([class*='indisponivel']):not([class*='disabled'])"
        )
        for n in range(opcoes.count()):
            opcao = opcoes.nth(n)
            try:
                if opcao.is_visible():
                    opcao.click(timeout=3000)
                    pagina.wait_for_timeout(800)
                    escolhidas += 1
                    break
            except Exception:  # noqa: PERF203
                continue
    if not escolhidas:
        escolhidas = _escolher_variacoes_tray(pagina)
    if escolhidas:
        log.info("%s: %d grade(s) de variacao escolhida(s)", pagina.url, escolhidas)
    return escolhidas


# Tray: "Escolha Tamanho" em <ul class="lista_cor_variacao"><li
# data-variant-value="43">, esgotado com a classe "sem_estoque". Sem a
# escolha, a calculadora responde "Por favor, selecione as variações antes
# de calcular o frete" (Renascer EPI, calçado do 1107). Grade dupla (cor e
# depois tamanho) monta a segunda lista só depois do clique na primeira.
VARIACAO_TRAY = "ul.lista_cor_variacao"


def _escolher_variacoes_tray(pagina) -> int:
    escolhidas = 0
    for i in range(4):
        listas = pagina.locator(VARIACAO_TRAY)
        try:
            if i >= listas.count():
                break
        except Exception:
            break
        opcoes = listas.nth(i).locator("li[data-variant-value]:not(.sem_estoque)")
        for n in range(opcoes.count()):
            opcao = opcoes.nth(n)
            try:
                if opcao.is_visible():
                    opcao.click(timeout=3000)
                    pagina.wait_for_timeout(1500)
                    escolhidas += 1
                    break
            except Exception:  # noqa: PERF203
                continue
    return escolhidas


BOTOES_DE_FECHAR = ("Aceitar", "Aceito", "Permitir", "Concordo", "Entendi", "Prosseguir",
                    "Mais tarde", "Agora não", "Não, obrigad", "OK")
# Logo antes da foto, sem "OK": seria o botão do CEP, e o frete sumiria
# para ser recalculado bem na hora do print.
BOTOES_ANTES_DO_PRINT = BOTOES_DE_FECHAR[:-1]

# Tudo que fica por cima da página no print: banner de cookies (Gimba,
# embaixo; Goiás Ferramentas, painel à esquerda com fundo escuro sobre o
# preço), janela de cupom, chat, botão de WhatsApp, cabeçalho fixo que
# a rolagem deixou no meio da imagem (Casa do Saco de Lixo). Elemento
# fixo é escondido, menos a barra do topo da página e o que mostra o
# frete -- há loja que cota o CEP numa janela.
ESCONDER_SOBREPOSICOES = r"""() => {
  window.scrollTo(0, 0);
  const vw = window.innerWidth, vh = window.innerHeight;
  const frete = /74933|frete|sedex|\bpac\b|transportadora|prazo de entrega/i;
  let escondidos = 0;
  for (const e of document.querySelectorAll('body *')) {
    const st = getComputedStyle(e);
    if (st.position !== 'fixed' && st.position !== 'sticky') continue;
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const area = (r.width * r.height) / (vw * vh);
    const barraDoTopo = r.top <= 5 && r.height < 220 && area < 0.4;
    if (barraDoTopo && st.position === 'sticky') continue;
    if (barraDoTopo && !/cookie|privacidade|lgpd/i.test(e.innerText || '')) continue;
    if (frete.test(e.innerText || '') && area < 0.6) continue;
    e.style.setProperty('display', 'none', 'important');
    escondidos++;
  }
  for (const e of [document.documentElement, document.body]) {
    if (getComputedStyle(e).overflow === 'hidden') e.style.setProperty('overflow', 'visible', 'important');
  }
  return escondidos;
}"""


def _aceitar_cookies(pagina, botoes=BOTOES_DE_FECHAR) -> None:
    """Banner de cookies costuma cobrir o preço. Tenta fechar, sem insistir."""
    for texto in botoes:
        try:
            botao = pagina.get_by_role("button", name=texto, exact=False).first
            if botao.count() and botao.is_visible():
                botao.click(timeout=2000)
                return
        except Exception:  # noqa: PERF203
            continue
    # Balão que não é <button> ("Mais tarde" da Dutra): só textos que
    # não existem em ficha de produto, clicados pelo texto exato.
    for texto in ("Mais tarde", "Agora não", "Não, obrigado"):
        try:
            alvo = pagina.get_by_text(texto, exact=True).first
            if alvo.count() and alvo.is_visible():
                alvo.click(timeout=2000)
        except Exception:  # noqa: PERF203
            continue


def limpar_tela(pagina) -> None:
    """Tira da frente do print o que cobre produto, preço e frete.

    Banner e janela costumam chegar depois do carregamento, então isto
    roda logo antes da foto: fecha pelo botão quando dá (o site para de
    escurecer a página) e esconde o que continua por cima. Esc não:
    fecharia também a janela de frete.
    Esconder não muda preço nem frete -- só o que aparece na imagem.
    """
    _aceitar_cookies(pagina, BOTOES_ANTES_DO_PRINT)
    try:
        escondidos = pagina.evaluate(ESCONDER_SOBREPOSICOES)
        pagina.wait_for_timeout(500)
    except Exception as e:  # o print sai mesmo assim; só registra
        log.warning("nao consegui limpar a tela de %s: %s", pagina.url, e)
        return
    if escondidos:
        log.info("%s: %d sobreposicao(oes) escondida(s) antes do print", pagina.url, escondidos)
