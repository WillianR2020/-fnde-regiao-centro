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

ESPERA_MAX_FRETE_MS = 15000
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

        if cep:
            preenchido, _, _ = preencher_e_esperar_frete(
                pagina, cep, seletor_cep, seletor_resultado,
            )
            if not preenchido:
                log.warning("nao achei campo de CEP em %s; print sai sem frete", url)
        else:
            try:
                pagina.wait_for_load_state("networkidle", timeout=10000)
            except Exception:  # loja com polling eterno: fotografa o que tem
                pass

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


def preencher_e_esperar_frete(
    pagina,
    cep: str,
    seletor_cep: str = "",
    seletor_resultado: str = "",
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
        return False, antes, antes

    if seletor_resultado:
        try:
            pagina.wait_for_selector(seletor_resultado, timeout=ESPERA_MAX_FRETE_MS)
        except Exception:
            log.warning("resultado de frete nao apareceu (%s)", seletor_resultado)
        return True, antes, pagina.evaluate(TEXTO)

    depois = antes
    for segundo in range(ESPERA_MAX_FRETE_MS // 1000):
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
BOTAO_DO_CAMPO = """(campo) => {
  const rotulo = /^(calcular|consultar|ok|simular|buscar|calcular frete)$/i;
  const texto = (b) => (b.innerText || b.value || '').trim();
  let no = campo.parentElement;
  for (let i = 0; i < 3 && no; i++, no = no.parentElement) {
    const botoes = [...no.querySelectorAll(
      'button, input[type=submit], input[type=button], a[class*="btn" i]')]
      .filter(b => b !== campo && b.offsetParent !== null);
    const botao = botoes.find(b => rotulo.test(texto(b))) ||
                  botoes.find(b => !texto(b) && (b.type === 'submit' || b.closest('form') === campo.closest('form') && campo.closest('form')));
    if (botao) { botao.click(); return true; }
  }
  return false;
}"""


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
                campo.press_sequentially(cep, delay=40, timeout=10000)
                if not campo.evaluate(BOTAO_DO_CAMPO):
                    campo.press("Enter")
                return campo
        except Exception:  # noqa: PERF203 - seletor que não serve, tenta o próximo
            continue
    return None


def _aceitar_cookies(pagina) -> None:
    """Banner de cookies costuma cobrir o preço. Tenta fechar, sem insistir."""
    for texto in ("Aceitar", "Aceito", "Concordo", "Entendi", "OK"):
        try:
            botao = pagina.get_by_role("button", name=texto, exact=False).first
            if botao.count() and botao.is_visible():
                botao.click(timeout=2000)
                return
        except Exception:  # noqa: PERF203
            continue
