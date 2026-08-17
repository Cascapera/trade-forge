# ADR-0020 — Indicadores de várias saídas, referenciados por componente

- **Status**: aceito
- **Data**: 2026-08-17
- **Contexto do PR**: PR-201-B

## Contexto

O `sdd.md §3.3.2` declara uma única forma de indicador:

```python
class Indicator(Protocol):          # estado incremental, O(1) por candle
    def update(self, candle: Candle) -> None: ...
    def value(self) -> float | None: ...
```

**Um `update`, um número.** Os seis indicadores que existiam (SMA, EMA, RSI, ATR, HIGHEST, LOWEST)
caem nessa forma sem folga. Bandas de Bollinger e ADX não: a primeira responde com três níveis
(superior, média, inferior) e o segundo com três linhas (`ADX`, `+DI`, `−DI`), e nos dois casos as
saídas vêm de **um estado só** — a mesma janela, os mesmos parâmetros, a mesma suavização.

A gramática de referência da DSL tinha o mesmo formato: `{"ref": "sma_fast"}` resolve um id nu, e
os únicos refs com ponto eram os namespaces `price.*` e `candle[-N].*`. Não havia como escrever
"a banda superior do `bb`".

## Decisão

Um indicador de várias saídas é **um objeto** que satisfaz um segundo protocolo,
`CompositeIndicator`, com `components() -> Mapping[str, Money | None]` no lugar de `value()`; e a
gramática de ref ganha a forma `id.componente`, sem componente padrão.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Três indicadores separados** (`bb_upper`, `bb_lower`, …) | Nenhum protocolo novo, nenhuma mudança na gramática de ref, nenhuma mudança no compilador | ⚠️ **Deixa os parâmetros divergirem sem que nada possa reclamar**: um documento declara `bb_upper` com período 20 e `bb_lower` com 50, e para o schema são dois indicadores independentes e bem-formados. O que sai é uma banda cujas bordas descrevem mercados diferentes — e é visualmente idêntica a uma banda. Ainda calcula o mesmo estado três vezes |
| **Alargar o `Indicator` existente** para todo mundo devolver um mapa | Um protocolo só; o compilador não escolhe caminho | Toca os seis indicadores de saída única e o `Charted.overlays` junto, para dar a cinco deles um mapa de um elemento que nenhum chamador quer. Muda o `sdd.md` de forma bem maior do que o problema pede |
| **Um segundo protocolo, e o compilador expande em canais** (escolhida) | Os parâmetros não podem divergir porque existe um só conjunto deles; os seis existentes não mudam uma linha; `expressions.py` não muda **nada** (o histórico já é indexado por string, então `bb.upper` é só uma chave) | Duas formas de indicador coexistem, e o compilador precisa perguntar qual recebeu. Um `Charted` que devolve `Mapping[str, Indicator]` precisa de um adaptador para publicar componentes |
| **Componente padrão** (`bb` = a média) | Menos verboso | `bb` e `bb.middle` viram duas grafias do mesmo valor — o que o comentário da própria `REF_PATTERN` dá como o jeito de uma DSL apodrecer. E para o ADX **não existe** padrão defensável |

## Trade-off aceito

**Duas formas de indicador em vez de uma**, e com isso um `isinstance` no compilador. Ele é pago
uma vez, na compilação: o `CompiledStrategy` resolve a forma de cada declaração em duas tuplas
planas com os nomes de canal já montados, então uma barra de backtest não paga `isinstance` nem
formatação de string pelo formato dos seus indicadores.

**O `CompositeIndicator.update` passa a ter uma obrigação que o `Indicator` não tem: ignorar uma
barra que já dobrou**, identificada pelo tempo. É consequência direta do `Charted.overlays`
devolver um `Indicator` por rótulo e o leitor dirigir o que recebeu — então a mesma barra chega uma
vez por componente. Sem isso, um indicador de três bandas dobraria toda barra três vezes e
reportaria uma média sobre o triplo das barras que alguém pediu: um número plausível, de uma série
que ninguém tem.

## Consequências

- **`packages/engine`**: `CompositeIndicator` em `protocols.py`; `Bollinger` e `ADX` em
  `indicators.py`; `ComponentView` (adaptador que veste um componente na forma de saída única, para
  o `Charted`); `COMPOSITE_COMPONENTS` no registry. O `CompiledStrategy` passa a indexar histórico,
  overlays e o `context` da entrada por **canal** (`bb.upper`), não por id de indicador.
- **`packages/schema`**: `REF_PATTERN` aceita `id.componente`. ⚠️ **E o aperto da validação
  semântica entra no mesmo commit, porque alargar a pattern sozinho é uma regressão**: o
  `semantic.py` decidia "é ref de indicador" perguntando se a string tinha ponto, então `bb.uppper`
  passaria a camada, o motor resolveria para `None`, e a comparação seria **falsa em toda barra**
  sem uma palavra. A decisão passou a ser por **namespace** (`price`, `candle`), com o componente
  checado contra o tipo do indicador.
- **Os nomes dos componentes existem em dois lugares** — os dois pacotes não se importam — e são
  fixados iguais por `apps/api/tests/test_indicator_contract.py`, que depende dos dois. Deriva aqui
  é da variedade silenciosa.
- **Ordem dos componentes é contrato: o principal primeiro.** É o que permite um desenhador decidir
  qual linha é o sujeito e quais são o envelope sem saber o que é uma banda.
- **`apps/api`**: nada. O endpoint de overlays é indexado por rótulo e serve N séries, então três
  bandas chegam pelo caminho que já existia.
- **`apps/web`**: a paleta de curvas passa a ser gasta **por indicador, não por curva**. Colorir por
  posição daria três matizes às três bandas — o leitor vê três indicadores — e esgotaria a paleta de
  três cores num `bb` só, descartando toda curva declarada depois dele.
- **`schema_version` continua `1.0`** (ADR-0013): membros novos na união discriminada e uma
  alternativa nova na `REF_PATTERN` são aditivos, e todo documento já salvo continua válido.
- **Aberto**: o ADX não pertence ao painel de preço — as três linhas vivem em 0–100 contra um preço
  de 1,10. ⚠️ **Isso não é novo neste PR**: o RSI já tem o mesmo problema desde que existe. Um
  painel separado por escala está no backlog.
