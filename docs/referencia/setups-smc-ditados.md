# Os quatro setups SMC, como o Guilherme os ditou

Este arquivo é a **transcrição das regras que ele ditou em voz**, entre 20/07 e 22/07/2026, e é a
definição autoritativa dos setups deste projeto. Ele existe pelo mesmo motivo que
`indicador-estrutura-profit.md` e `indicador-regioes-order-block.md`: o método é dele, e quando o
código e esta página divergirem, **esta página ganha** — ou ele corrige a página.

Estava num handoff pessoal (`docs/aulas/RETOMAR-camada-smc.md`, não versionado) que foi apagado em
12/08/2026 por já estar desatualizado quanto ao andamento. As regras vieram para cá porque **dois
dos quatro setups ainda não existem em código**, e este era o único registro delas.

> ⚠️ **Regra de processo, dele:** confirmar a definição exata com ele — com exemplo concreto —
> **antes** de implementar cada primitiva ou setup. Estas notas são um ponto de partida fiel, não
> uma licença para codar sem perguntar.

## Estado em 12/08/2026

| Setup | Existe no código? | Onde |
|---|---|---|
| **choch** | ✅ | `setup_factory.py::_structure_choch` (`ChochQualifier`) |
| **continuação** | ✅ | `setup_factory.py::_structure_continuation` (`ContinuationQualifier`) |
| **flip** | ❌ **não existe** | — |
| **grab** | ❌ **não existe** | — |

**Sempre pelo nome, nunca por número.** Ele foi explícito: `9.x` é capítulo do livro dele e confunde
com outros autores.

| Setup | Contexto | Zona de entrada |
|---|---|---|
| **flip** | zona flipada | a região que a perna de flip cria |
| **choch** | CHoCH confirmado por fechamento | zona deixada pelo movimento de reação |
| **continuação** | CHoCH **e depois** BOS a favor | zona criada pelo BOS |
| **grab** | BOS + poça + captura | zona perto da origem do BOS |

Os quatro terminam igual: **preço volta e toca a zona → entra → stop fora da zona → alvo na região
ou liquidez oposta.**

**Dois modos para flip, choch e continuação:** *simples* (só o timeframe operacional) e *multi* (só
procura trade quando o preço atinge região de timeframe maior). ⚠️ **A macro é um portão, não uma
reescrita da engine.** Decisão dele: começar pelos três no modo simples; o grab depois.

## Marcação de order block

- **Por ineficiência de preço.** Ele descarta explicitamente o método "Order Flow" (última barra
  contrária) e manda usar **só um**.
- Movimento que gera BOS/CHoCH **e ao mesmo tempo** cria FVG → a zona é o **candle imediatamente
  anterior ao da ineficiência** (confirmado: no FVG de 3 candles, o OB é o **c1**).
- **Regra do pavio:** se a mínima do candle da ineficiência (sombra ou corpo) for menor que a do
  anterior, ela **entra** na marcação. Espelha para a oferta.
- O FVG tem que estar **na perna de impulsão** do BOS/CHoCH.
- **Regra da pausa:** um impulso pode deixar **várias** zonas, mas só quando a formação de gap
  **para** (uma barra sem gap basta) e recomeça. Gaps em barras consecutivas = **um** evento = **uma**
  zona, marcada no primeiro. A primeira é a **primária**.
- Primária vs secundária fica no **pedido do backtest** (`allow_secondary`), não na marcação.

## Ciclo de vida de uma região

- **Toque:** por pavio.
- **Mitigada:** tocou **e depois fechou** 1× o tamanho da zona além dela. Exemplo dele: `[90,100]`,
  tamanho 10, mitiga quando **fecha** acima de 110.
- **Flipada:** o preço atravessou. Por pavio sem fechar além → continua válida. **Fechou além →
  flipada E mitigada** (não nasce zona nova; as duas marcas ficam na zona, e é isso que o flip
  procura).
- **Mitigada nunca mais flipa** — a região deixou de existir.
- **"Sair da zona" é por fechamento, não por pavio:** numa notícia o preço espeta para fora e volta,
  e isso não é o mercado se afastando.

> Ver `indicador-regioes-order-block.md` para a regra como o indicador dele a implementa, com as 4
> divergências medidas e a prova de equivalência de 574/574 regiões.

## Flip — a variação **sem BOS** (ele descartou a "com BOS")

**Ainda não implementado.** As regras, como ele as ditou:

- Mercado sobe deixando demandas válidas → inverte e **flipa** uma → isso já arma a venda.
- A **perna de queda que flipou cria uma região de oferta**, nascida de **gap entre barras**, **sem
  CHoCH**.
- **O flip tem que ser brusco** — romper de uma vez. Duas barras caindo em sequência sem repique
  ainda contam.
- **Janela da perna de flip** = a mesma máxima que o CHoCH usaria (a máxima desde o último BOS de
  alta), **sem** o CHoCH ter ocorrido.

⚠️ **Duas lacunas técnicas conhecidas, levantadas quando a regra foi ditada:**

1. O `OrderBlockDetector` **não marca essa zona** — ele só marca em BOS/CHoCH. Precisa ser estendido.
2. O `MarketStructure` já rastreia a máxima em `_up_top`/`_up_top_time`, mas **não a expõe**.

⚠️ **Pergunta aberta, no `specs/backlog.md` (origem PR-202): o toque de raspão desarma o flip.** O
toque de uma zona é não estrito (`low <= top` na demanda), então uma barra cuja mínima encosta
exatamente no topo já marca a zona como tocada; ela vira `departed`, perde `flippable` para sempre,
e o rompimento posterior deixa de ser flip. Medido: com `low=100.00` numa zona `[90,100]` o flip
some; com `low=100.01` ele existe. **É coerente com a regra dele** ("não pode tocar nela, subir, e
depois vir flipar"), então não é bug — mas em gráfico real quase toda demanda é raspada e abandonada
antes de ser rompida de verdade, e o flip pode quase nunca armar. **Ação combinada:** medir a
frequência de `flipped` em dados reais e perguntar a ele se "tocar" ali deveria contar.

## Entrada, stop e alvo

- **Entrada = ordem LIMITE na borda próxima da zona** (não a mercado no próximo open). Preenche ao
  preço do nível quando o pavio cruza; gap de abertura além do limite → preenche no open.
  Formalizado no **ADR-0014**.
  - Consequência aceita por ele: um pavio que espeta a borda e volta **já preenche** — você foi
    executado quando o preço chegou lá.
- **Stop na borda distante ± 10% do tamanho da zona.** Números dele: oferta `[90,100]` → vende a
  partir de **90**, stop **101**. Demanda `[90,100]` → compra em **100**, stop **89**.
- **Alvo = múltiplo de risco, teto de X·R. Default 5R, editável.**
  ⚠️ Isto **substituiu** a decisão anterior de "mira na região/liquidez oposta, senão R". Não se
  mira mais em região oposta.
- **Uma ordem limite viva por vez** — a zona qualificada mais recente. Zona nova **substitui** a
  anterior (cancela e rearma). Nada de N ordens simultâneas.
- **Ciclo de vida da ordem = herdado da zona:** vive enquanto a zona é `usable`; morre quando a zona
  mitiga, flipa ou envelhece sem ter preenchido. Sem prazo próprio.
- **Broker agnóstico (invariante 4):** o broker executa ordem limite e expõe `cancel()`; a
  **estratégia** é dona do ciclo de vida. O broker nunca conhece zonas.

### Quando a ordem é armada — regra textual dele (22/07)

> *"A ordem arma quando rompe a estrutura e configura o setup. No caso do choch e bos, quando ele
> confirma o BOS ou o CHoCH já pode pendurar a ordem na região. No caso do flip, quando ele flipa a
> região que arma a possibilidade do trade, a ordem já pode ir para a pedra."*

⚠️ **Registro anterior estava ERRADO e foi corrigido:** houve uma tradução de "esperar o preço se
afastar" para a flag `departed`. Mas `departed` exige um *round trip* — o preço voltar, **encostar**
na zona, e só então fechar fora. Exigir isso significaria não ter ordem na mesa no primeiro retorno,
e **o primeiro retorno é o toque com a zona ainda virgem**.

**Única guarda que sobrou:** se o fechamento ainda estiver **dentro** da zona, a ordem não pode ser
colocada (compra limite acima do mercado = o `Signal` recusa por lado errado). A zona fica armada e
a ordem sai na primeira barra que fecha livre — nada se perde.

## Leitura do choch e da continuação (modo simples)

- **choch:** CHoCH confirma (fecha além da âncora, inverte a tendência) → o movimento de reação
  deixou uma zona → preço volta e toca → entra a favor da nova tendência. **Não espera mais nada**,
  senão vira continuação.
- **continuação:** CHoCH confirma **e depois** um BOS a favor da nova tendência → entra na zona que
  o BOS deixou → pullback → entra. Nas palavras dele: *"aguardar dois elementos: encerramento
  (CHoCH) e confirmação (BOS)"*.

## Condução

- **Breakeven no 1º BOS a favor, depois stop atrás dos topos/fundos válidos** (espelhado na venda).
- Ver também `swing-conducao-mme9` e `swing-ponto-continuo` para as regras da linha de swing, que
  são outra família.

---

**Origem do material:** capítulos úteis do Black Book dele — **6** (order block, pág. 68-70),
**7** (refinamento), **9** (setups, pág. 77-93).
