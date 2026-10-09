"""O contrato que todo executor de site implementa.

Quem escreve adapter não precisa saber nada de matching.
Quem escreve matching não precisa saber nada de VTEX.
Ambos conversam pelo ProdutoBruto.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.http import Cliente, ErroHTTP
from src.core.log import obter
from src.models import Fornecedor, ProdutoBruto

log = obter(__name__)


class Executor(ABC):
    """Um executor é instanciado UMA VEZ por site e reutilizado.

    Isso é deliberado: a sessão (HTTP ou navegador) é o recurso caro.
    Nunca instancie um executor por item.
    """

    plataforma: str = "desconhecida"

    def __init__(self, fornecedor: Fornecedor, cliente: Cliente) -> None:
        self.fornecedor = fornecedor
        self.cliente = cliente

    # --- Etapa 2: varredura -------------------------------------------------

    # Endpoint que prova que a loja fala mesmo esta API, com {base}.
    # Vazio significa "não dá para sondar": assume-se que fala.
    SONDA: str = ""

    def api_responde(self) -> bool:
        """A plataforma foi detectada, mas a API dela existe nesta loja?

        Não é a mesma pergunta. A detecção lê marcador no HTML da home;
        a busca precisa do endpoint ligado. Das 20 lojas WooCommerce
        aprovadas, 15 respondem 404 em `/wp-json/wc/store/v1` — o
        plugin da Store API não está ativo, ou o WAF bloqueia a rota.

        Sem esta pergunta, essas 15 entram na varredura, recebem lista
        vazia em todos os 132 itens e saem do relatório como "não vende
        nada" — indistinguíveis de uma loja varrida de verdade. Quem
        responde não, vai para o navegador, que enxerga o que o
        comprador enxerga.
        """
        if not self.SONDA:
            return True

        url = self.SONDA.format(base=self.fornecedor.url_base.rstrip("/"))
        try:
            dados = self.cliente.get_json(url)
        except ErroHTTP as e:
            log.info("%s: %s nao responde (%s)",
                     self.fornecedor.dominio, self.plataforma, type(e).__name__)
            return False
        return isinstance(dados, (list, dict))

    @abstractmethod
    def buscar(self, termo: str) -> list[ProdutoBruto]:
        """Busca um termo na loja e devolve os produtos encontrados.

        Não filtra nem pontua nada: isso é trabalho do matching.
        """

    # --- Etapa 3: coleta ----------------------------------------------------

    def detalhar(self, url_produto: str) -> ProdutoBruto:
        """Preço, desconto e estoque na página do produto.

        O padrão lê a página pelo navegador (`core.pagina_produto`), a
        mesma que o print fotografa. Sobrescreva onde a plataforma tem
        API de ficha, como a VTEX.
        """
        from src.core import pagina_produto

        return pagina_produto.detalhar(url_produto)

    def cotar_frete(
        self,
        url_produto: str,
        cep: str,
        produto: ProdutoBruto | None = None,
    ) -> tuple[float | None, str]:
        """Devolve (valor, observação) para 1 unidade naquele CEP.

        Quantidade é sempre 1, então não é preciso montar carrinho: o
        campo de CEP da própria página do produto basta.

        `produto` é o resultado de `detalhar()`, passado adiante para não
        refazer a mesma requisição uma vez por UF -- e para garantir que
        o frete é do mesmo SKU e do mesmo vendedor que deram o preço.
        Quem não precisar dele pode ignorá-lo.

        O padrão preenche o CEP na página pelo navegador e lê a opção de
        entrega mais barata que aparecer. Página sem campo de CEP vira
        frete sob consulta, com observação.
        """
        from src.core import pagina_produto

        return pagina_produto.cotar_frete(
            url_produto, cep, self.SEL_CAMPO_CEP, self.SEL_RESULTADO_FRETE,
        )

    # Sobrescreva no adapter do site quando a heurística de CEP não
    # pegar. Estes dois são os únicos seletores que o print precisa.
    SEL_CAMPO_CEP: str = ""
    SEL_RESULTADO_FRETE: str = ""

    def capturar_print(self, url_produto: str, cep: str, destino: str) -> str:
        """Print DEPOIS do frete carregar, com preço e frete na mesma imagem.

        Print só do produto não valida entrega, que é o que o
        solicitante quer conferir.

        O padrão serve para qualquer site, inclusive os de plataforma com
        API: o print é prova para uma pessoa ler, e ninguém confere JSON.
        """
        from src.export.captura import capturar

        return str(capturar(
            url_produto,
            cep,
            destino,
            seletor_cep=self.SEL_CAMPO_CEP,
            seletor_resultado=self.SEL_RESULTADO_FRETE,
        ))

    # --- ciclo de vida ------------------------------------------------------

    def abrir(self) -> None:
        """Sessão, CEP, cookies. Chamado uma vez antes do lote."""

    def fechar(self) -> None:
        """Chamado uma vez depois do lote, mesmo se houve erro."""

    def __enter__(self):
        self.abrir()
        return self

    def __exit__(self, *_):
        self.fechar()
