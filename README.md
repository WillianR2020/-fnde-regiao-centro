# fnde-regiao-centro

Coleta de preços no **atacado** para **Goiás (GO)**, a partir
da lista de itens da **Pesquisa SEAD-GO de Limpeza Predial**.

Esta é uma versão adaptada, com histórico próprio, do pipeline
[`cosmic-axolotl/sul-scrapers`](https://github.com/cosmic-axolotl/sul-scrapers)
(Região Sul / Lista de Equipamentos FNDE). A arquitetura, as etapas e as
regras de validação são as mesmas. O que mudou foram a região, a lista de
itens e as entradas de fornecedores (ver
[O que mudou em relação à versão Sul](#o-que-mudou-em-relação-à-versão-sul)).

> **Status:** projeto adaptado, sem coleta. Nenhuma etapa foi rodada contra
> sites reais nesta versão. O `fornecedores_master.csv` ainda não existe:
> ele nasce da etapa 1.

## O que o projeto entrega

- **Itens:** 124 da aba "Solicitação itens" da pesquisa SEAD-GO.
  - 120 vão para a coleta automática: 56 de MATERIAIS DE CONSUMO, 48 de
    FERRAMENTAS E EQUIPAMENTOS e 16 de EPIS E UNIFORMES.
  - 4 são coletados à mão, todos da categoria AUXÍLIOS: 1 passagem de
    ônibus de Goiânia e 3 ASOs (exames de saúde ocupacional). Não existe
    loja online com carrinho para eles.
- **UF:** só GO. O DF saiu da pesquisa em 09/10/2026.
- **Por item:** 5 preços de 5 fornecedores distintos que atendem GO.
  - Sede em GO: sem cálculo de frete; PREÇO FINAL = preço do produto.
  - Sede fora de GO: frete para GO cotado e **somado** ao preço; só entra
    se a entrega em GO for confirmada (frete "sob consulta" não serve).
- **Formato:** o template de 15 colunas em `data/raw/FNDE_output.xlsx`,
  mais a 16ª coluna `PRINT`. Há um exemplo preenchido (da rodada Sul) em
  `docs/modelo_entrega/EQUIPAMENTO_exemplo_sul.xlsx`.

### CEPs usados no cálculo de frete

| UF | CEP | Logradouro (ViaCEP) |
|---|---|---|
| GO | 74933-130 | Rua Monte Carmelo, Cardoso, Aparecida de Goiânia |

Ficam em `src/models.py` (`CEPS_CENTRO`). As UFs da pesquisa saem desse
dicionário: para incluir ou retirar uma UF, basta mexer nele.

## Instalação

Requer **Python 3.11+**.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

Com [uv](https://docs.astral.sh/uv/):
`uv venv --python 3.11 .venv && uv pip install -r requirements.txt`.

Nenhuma credencial é necessária. A consulta de CNPJ usa a BrasilAPI
pública, e o arquivo `.env` (se alguém criar um) já está no `.gitignore`.

## Entradas

| Arquivo | O que é |
|---|---|
| `data/raw/Pesquisa_SEAD_GO_Limpeza_Predial.xlsx` | Lista de itens e sugestões de empresas da SEAD-GO, **sem e-mail, telefone e endereço** |
| `data/raw/leads/sugestoes_sead_go.csv` | **Prioridade 1:** as 13 sugestões de fornecedores de produto da pesquisa (GO e DF) |
| `data/raw/leads/lista_sites.xlsx` | **Prioridade 2:** a lista de sites da prospecção anterior, com todas as abas |
| `data/raw/FNDE_output.xlsx` | Template da entrega |

Em `sugestoes_sead_go.csv`, só 4 das 13 empresas têm site, e esse site é o
**domínio do e-mail** informado na pesquisa: confira antes de validar. As
outras 9 estão sem site e precisam que a prospecção ache o domínio, porque
a chave do projeto é o domínio. As 6 sugestões de serviço (ASO e passagem)
não entram como lead: servem para a coleta manual e estão na aba
"Sugestões de empresas" da planilha.

## Os comandos

```bash
# Etapa 0: limpa a lista de itens (já rodada; resultado versionado em data/interim/)
python -m src.runners.etapa0_limpar_itens

# Etapa 1: valida fornecedores (CNPJ, CNAE, sede e entrega em GO)
python -m src.runners.etapa1_validar               # gera data/interim/fornecedores_master.csv
python -m src.runners.etapa1_validar --com-frete   # também testa o CEP de GO para quem é de fora (lento)
python -m src.runners.etapa1_validar --so-busca    # só relê URL/seletor de busca, sem rede

# Etapa 2: varredura, para saber quais sites têm quais itens
python -m src.orquestrador varredura --categoria "MATERIAIS DE CONSUMO" --limite 10   # piloto
python -m src.runners.etapa2_plano

# Etapa 3: coleta de preço, frete e print (uma linha por item × site × UF)
python -m src.orquestrador coleta --categoria "MATERIAIS DE CONSUMO"

# Monta a entrega
python -m src.export.montar_entrega --entrada data/coletas/ --saida data/output/

# Testes
pytest -m "not lento"    # segundos
pytest                   # inclui o fluxo completo com navegador (~2 min)
```

As categorias são `MATERIAIS DE CONSUMO`, `FERRAMENTAS E EQUIPAMENTOS` e
`EPIS E UNIFORMES`. **Sempre rode `--limite 10` antes de disparar uma
categoria inteira.**

## Regras que não se quebram

1. Só entra fornecedor com CNAE principal de atacado (divisão 46) e CNPJ
   ativo. A exceção de varejo (47.53-9 / 47.59-8) foi herdada da versão Sul
   e está descrita em `AGENTS.md`.
2. Atender **GO** é o critério. Sede em GO entra sem frete; sede em outro
   estado entra só com entrega em GO confirmada, e o frete vai somado ao
   preço. Na etapa 1 só a recusa reprova; timeout deixa PENDENTE.
3. Nenhum site é coletado sem estar aprovado em `fornecedores_master.csv`.
4. O **site** é a unidade de execução: um processo por domínio, sempre.
5. Coleta e montagem são programas separados.
6. `id_item` é **texto**, nunca int (`1000`, `G008`, `318N`).
7. No máximo 3 requisições simultâneas por domínio.
8. Os 5 preços vêm de 5 **domínios distintos**, cada um com preço e print
   (com o frete visível quando a sede é de fora de GO).
9. A embalagem faz parte do item: um frasco de 1 L não atende um galão de
   5 L, e um saco avulso não atende um pacote com 100.

## O que mudou em relação à versão Sul

| Ponto | Sul (original) | Centro (esta versão) |
|---|---|---|
| UFs / CEPs | PR, SC, RS (`CEPS_SUL`) | só GO desde 09/10/2026 (`UF_PESQUISA`, `CEPS_CENTRO`) |
| Campo de entrega | `entrega_sul` | `entrega_centro` (modelo e coluna do master) |
| Regra de reprovação | recusa em PR, SC e RS | recusa em GO, para sede fora de GO |
| Frete | sempre cotado e mostrado à parte | sede em GO: não calculado; fora de GO: somado ao PREÇO FINAL |
| CEP de controle (grátis nacional) | Av. Paulista, fora do Sul | o mesmo, que também está fora do Centro |
| Lista de itens | Lista de Equipamentos FNDE (144) | Pesquisa SEAD-GO Limpeza Predial (124) |
| Itens manuais | COMBUSTÍVEL, SAÚDE OCUPACIONAL | + AUXÍLIOS (passagem e ASO) |
| Termos de busca | grupo de insumo = produto | nos 3 grupos genéricos da SEAD, o termo sai do nome do produto (`termo_base`) |
| Tamanho do item | estava na descrição | a coluna Embalagem (e KG/L) é acrescentada à descrição (`com_embalagem`) |
| Bases de fornecedores | duas planilhas do Sul | opcionais (`*_centro.xlsx`); a entrada principal é `data/raw/leads/` |
| Dados gerados | master, achados, plano do Sul | apagados, porque eram da prospecção Sul |
| Coleta manual de uniformes | 22 preços do Sul no script | ferramenta mantida, sem os registros |

A lista FNDE antiga continua passando pela etapa 0 **exatamente** como
antes. Isso foi conferido comparando a saída nova com o `itens.csv` do Sul.

## Pendências

- **Sites das sugestões SEAD:** 9 das 13 empresas sem domínio, e 4 com
  domínio tirado do e-mail, a confirmar.
- **Exceção de varejo e canal `--varejista`:** a decisão de 23/09/2026 foi
  tomada para a categoria EQUIPAMENTO do FNDE Sul. Falta confirmar se vale
  para a SEAD-GO. Ainda não há `itens_varejo.csv` nesta versão.
- **Prazo:** a definir. A regra dos 5 preços (só GO, frete pela sede)
  foi definida em 09/10/2026.
- `docs/GUIA.md` e `docs/ESTRUTURA.md` são da versão Sul e podem estar
  desatualizados (ver `docs/LEIA-ME.md`).

## Como contribuir

Branch por frente (ex.: `etapa1/sugestoes-sead`), PR com uma revisão,
commits em português no imperativo. Assistentes de IA (Codex, Claude Code,
Gemini CLI) leem `AGENTS.md`, que é a fonte única de contexto.
