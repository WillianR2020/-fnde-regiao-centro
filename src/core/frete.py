"""Classificação e cotação de frete. A parte mais trabalhosa do projeto.

Frete não é um valor, é um comportamento: o mesmo site pode entregar nos
estados da pesquisa com preços diferentes, ter frete grátis nacional, ou grátis
acima de um valor que uma unidade nunca alcança.

Daí a separação em dois momentos:
  classificar_site()   uma vez por fornecedor, grava `modo_frete`
  cotar_produto()      só quando o modo exigir
"""

from __future__ import annotations

from src.core.log import obter
from src.models import CEPS_CENTRO, Fornecedor, ModoFrete

log = obter(__name__)

# Um CEP fora da Região Centro (Av. Paulista). Sem ele não dá para
# distinguir "frete grátis para o Brasil inteiro" de "frete grátis só na
# região": nos dois casos os CEPs de GO e DF dão zero.
CEP_FORA_DO_CENTRO = "01310100"

# Pistas de "grátis acima de X" no texto que o site devolve junto da
# cotação. Não é o valor: é o sinal de que existe um mínimo.
PISTAS_MINIMO = ("acima de", "a partir de", "compras acima", "minimo", "mínimo")

# Marcador que o adapter põe na observação quando a loja respondeu e
# disse que NÃO entrega naquele CEP. É diferente de "não consegui
# cotar": o primeiro reprova o fornecedor, o segundo só deixa a
# pergunta em aberto. Sem essa distinção, um site fora do ar viraria
# "não entrega na região" e sairia da lista por engano.
SEM_ENTREGA = "sem opcao de entrega"


def _sem_acento(texto: str) -> str:
    from unidecode import unidecode

    return unidecode(str(texto or "")).lower()


def houve_recusa(observacao: str) -> bool:
    """A loja respondeu e disse que não entrega ali."""
    return SEM_ENTREGA in _sem_acento(observacao)


def _entrega(valor: float | None, observacao: str) -> bool | None:
    """True entrega, False não entrega, None não deu para saber."""
    if valor is not None:
        return True
    if houve_recusa(observacao):
        return False
    return None


def classificar_site(
    fornecedor: Fornecedor,
    executor,
    url_produto_exemplo: str,
) -> tuple[ModoFrete, dict[str, bool]]:
    """Descobre como este site cobra frete, usando um produto representativo.

    Devolve (modo, entrega_centro). A regra:
      - todos zerados, inclusive fora da região -> GRATIS_NACIONAL
      - zerados só em GO/DF                     -> GRATIS_REGIAO
      - cotação menciona mínimo para grátis   -> GRATIS_ACIMA_DE
      - valores diferentes por CEP            -> TABELA_POR_CEP
      - sem cotação online                    -> SOB_CONSULTA

    Uma unidade só é o que a entrega pede, então GRATIS_ACIMA_DE é, na
    prática, frete cobrado: o mínimo nunca é alcançado por 1 item.

    `entrega_centro` só traz a UF sobre a qual houve resposta. UF que a
    loja recusou entra como False; UF que não deu para cotar fica de
    fora do dicionário, e não vira um False que reprovaria o
    fornecedor por um timeout.
    """
    cotacoes: dict[str, tuple[float | None, str]] = {}
    for uf, cep in CEPS_CENTRO.items():
        cotacoes[uf] = _cotar(executor, url_produto_exemplo, cep)

    entrega_centro = {
        uf: atende
        for uf, (valor, obs) in cotacoes.items()
        if (atende := _entrega(valor, obs)) is not None
    }
    valores = [valor for valor, _ in cotacoes.values() if valor is not None]
    observacoes = " ".join(obs.lower() for _, obs in cotacoes.values())

    if not valores:
        log.info("%s: nenhuma cotacao online -> SOB_CONSULTA", fornecedor.dominio)
        return ModoFrete.SOB_CONSULTA, entrega_centro

    if any(pista in observacoes for pista in PISTAS_MINIMO):
        return ModoFrete.GRATIS_ACIMA_DE, entrega_centro

    if all(v == 0 for v in valores) and len(valores) == len(CEPS_CENTRO):
        fora, _ = _cotar(executor, url_produto_exemplo, CEP_FORA_DO_CENTRO)
        if fora == 0:
            return ModoFrete.GRATIS_NACIONAL, entrega_centro
        return ModoFrete.GRATIS_REGIAO, entrega_centro

    return ModoFrete.TABELA_POR_CEP, entrega_centro


def _cotar(executor, url: str, cep: str) -> tuple[float | None, str]:
    try:
        return executor.cotar_frete(url, cep)
    except Exception as e:
        # Classificar frete é diagnóstico: um site que não coopera vira
        # SOB_CONSULTA, não uma exceção que derruba a validação inteira.
        log.debug("cotacao falhou para %s: %s", cep, type(e).__name__)
        return None, f"cotacao indisponivel ({type(e).__name__})"


def cotar_produto(
    executor,
    url_produto: str,
    uf: str,
    produto=None,
) -> tuple[float | None, str]:
    """Devolve (valor, observação) para 1 unidade naquela UF.

    Quantidade é sempre 1, então não é preciso montar carrinho: o campo
    de CEP da própria página do produto basta — e garante que preço e
    frete vieram da mesma página, que é o que o print precisa provar.
    """
    cep = CEPS_CENTRO[uf]
    return executor.cotar_frete(url_produto, cep, produto)
