# A fonte da verdade das regiões: o indicador de Order Block do Guilherme

`packages/engine/src/tradeforge_engine/structure.py::OrderBlockDetector` é uma **transcrição**
deste indicador. Ele é a definição autoritativa de região de oferta e demanda neste projeto — e,
principalmente, de **quando uma região morre**.

**Quando os dois divergirem, o indicador ganha.** Este arquivo existe para que a divergência seja
verificável em vez de discutível, exatamente como `indicador-estrutura-profit.md` faz para BOS e
CHoCH.

O código abaixo é o Pine Script v6 que ele opera no TradingView, que por sua vez é o porte do NTSL
`Order_Block_Casca` que ele operava no ProfitChart. As duas versões implementam a mesma regra.

## As quatro regras, e como a engine divergia de cada uma

Medido em 05/08/2026 sobre os mesmos **3480 candles reais de AAPL H1** (2024-08-01 → 2026-07-31)
que serviram à estrutura.

### 1. O gap exige que o candle do meio feche além da origem

```pine
gapAlta  = low  > high[2] and close[1] > high[2]
gapBaixa = high < low[2]  and close[1] < low[2]
```

A engine testava só a primeira metade (`third.low > first.high`), sem a condição do fechamento.

| | gaps de alta | gaps de baixa |
|---|---|---|
| indicador | 428 | 347 |
| engine (antes) | 482 | 388 |
| **marcados a mais** | **54** | **41** |

### 2. A zona é o range puro do candle que originou o gap

```pine
float t = high[2]
float f = low[2]
```

Nada mais: nem meio, nem extensão. A engine esticava a zona pelo pavio do candle de impulso
(`min(marking.low, impulse.low)` na demanda), o que **deslocava a borda em 22% das zonas** — e a
borda é exatamente onde a ordem descansa.

### 3. Mitigação é o primeiro toque na borda de entrada, por pavio

```pine
ob.bull ? low <= ob.topo : high >= ob.fundo
```

Sem fechamento, sem percentual, sem "afastou-se uma largura". A engine tinha uma regra
`driven_off` — *fechou uma largura inteira além da zona, logo ela cumpriu seu papel* — que **não
existe no método dele**: foi invenção da implementação.

⚠️ **A varredura começa na barra do GAP (marcação + 2)**, não na barra seguinte à marcação. No
Pine, `processar()` só roda a partir da barra em que o OB é criado; a barra de marcação e a de
impulso nunca são testadas. Errar isso por uma barra faz o próprio impulso contar como toque e
mata 99% das regiões em vez de 49%.

**Elegância que a engine não tinha percebido:** na barra do gap, `low > high[2]` (alta) garante que
`low <= topo` é falso *por construção*. Uma região não pode se auto-mitigar, então nenhum truque de
"nasce virgem" é necessário — a geometria já resolve.

**O efeito na engine era total**: ela criava a região só quando um rompimento a revelava, ignorando
tudo que o preço fizera desde o gap — mediana de **16 barras**, máximo de **282**. Resultado:
**49% das regiões já estavam mortas** no instante em que eram oferecidas ao setup.

### 4. Um rompimento novo mata as regiões do anterior

Esta regra **não está no Pine** — o indicador desenha regiões sem olhar estrutura. Ela é do método
dele, dita em 05/08/2026:

> *"um choch de baixa cria a zona, se depois ele fizer um bos de baixa, a entrada de choch morre.
> mesma coisa um bos de baixa, se ele faz um segundo bos a entrada do 1 morreu e só pode entrar
> pelo segundo. e assim vai"*

Ou seja: **só as regiões do rompimento mais recente estão vivas.** O `ContinuationQualifier` já
fazia isso (um BOS novo substitui a escada inteira); o `ChochQualifier` **só reagia a outro CHoCH**,
e deixava um BOS passar despercebido.

| | |
|---|---|
| CHoCHs no período | 34 |
| seguidos de ao menos um BOS antes do CHoCH seguinte | 21 (62%) |
| BOS que deveriam ter matado regiões de CHoCH | 53 |
| máximo empilhado num único CHoCH | 7 |

### E o que NÃO é regra dele: o flip

A engine mantinha `departed`, `flipped` e `flippable` no `TrackedZone` para alimentar um setup de
*flip* — negociar a região que o preço atravessou, contra quem ficou preso nela. Perguntado
diretamente em 05/08/2026, ele respondeu: **"ZONA MORTA NÃO SERVE DE FLIP"**.

O setup de flip nunca chegou a ser construído. A máquina que existia para servi-lo foi removida; se
um flip entrar no método, será definido pelo indicador dele, como tudo o mais.

## O caso que originou a revisão

AAPL H1. CHoCH de baixa confirmado em **21/08/2025 16:00** no nível 224,76. As quatro regiões que a
perna marcou, com o critério dele aplicado:

| | região | marcada | estado no CHoCH |
|---|---|---|---|
| **primária** | [232,91 .. 235,12] | 14/08 13:00 | **mitigada** — 5 toques, o 1º em 14/08 17:00 |
| secundária | [232,69 .. 234,22] | 14/08 19:00 | mitigada — 3 toques |
| secundária | [231,35 .. 233,12] | 18/08 13:00 | mitigada — 7 toques |
| secundária | [229,36 .. 230,69] | 19/08 19:00 | **aberta** |

A estratégia rodava com `allow_secondary: false`, então só a primária era oferecida — a única que
estava morta havia sete dias. A ordem preencheu em 28/08 e tomou **−1,73R**.

⚠️ **O achado sistemático:** a primária é, por definição, a região **mais antiga** da perna — logo a
que teve mais tempo para ser consumida. Oferecer só a primária significa oferecer preferencialmente
a mais gasta. Não foi azar deste caso; era o desenho.

## Uma diferença de escopo, deliberada

O indicador marca uma região em **todo** gap, sem olhar estrutura. A engine só oferece as regiões
que caem dentro da **perna de impulso** do rompimento (`origin_time → time`), e distingue primária
de secundária pela ordem de formação dentro dela.

Isso não é divergência do indicador: é a camada de *setup* — qual região vale a pena negociar —
que o indicador não tem, porque ele é uma ferramenta de desenho. A regra da perna vem do método
dele e está descrita em `sdd.md`; se algum dia ela divergir do que ele opera, este arquivo é o
lugar de registrar.

## O código, como ele o enviou

```pine
//@version=6
// =============================================================================
//  SMC Order Blocks (Casca)
//  Porte para Pine Script do indicador NTSL "Order_Block_Casca" (ProfitChart).
//
//  Regra original, preservada:
//    - Gap de alta  : low > high[2]  e  close[1] > high[2]
//    - Gap de baixa : high < low[2]  e  close[1] < low[2]
//    - A zona do OB e a barra que originou o gap (a de 2 barras atras),
//      de low[2] ate high[2]. O NTSL guardava o ponto medio + metade do range,
//      o que da exatamente esse retangulo.
//    - Trava anti-duplicidade: enquanto a condicao de gap continuar verdadeira
//      em barras seguidas, so o primeiro toque cria o OB.
//    - Mitigacao no primeiro toque na borda de entrada da zona:
//        OB de alta  -> low  <= topo da zona
//        OB de baixa -> high >= fundo da zona
//      Ao mitigar, a zona para de se estender e vira cinza (ou some, se
//      "Exibir OBs mitigados" estiver desligado).
//
//  Diferenca em relacao ao NTSL: la o desenho era fixo em 49 niveis por lado;
//  aqui o limite e configuravel e os desenhos mais antigos sao descartados.
// =============================================================================
indicator("SMC Order Blocks (Casca)", "SMC OB", overlay = true, max_boxes_count = 500, max_lines_count = 500, max_labels_count = 500)

// ------------------------------- Inputs --------------------------------------
grpG        = "Geral"
maxOB       = input.int(20, "Maximo de OBs por lado", minval = 1, maxval = 200, group = grpG)
exibirMitig = input.bool(true, "Exibir OBs mitigados", group = grpG)
plotarTexto = input.bool(true, "Exibir texto \"OB\"",   group = grpG)
mostrarMeio = input.bool(true, "Linha do meio da zona", group = grpG)

grpV     = "Visual"
corAlta  = input.color(#46c846, "OB de alta",  group = grpV)  // rgb(70,200,70) do NTSL
corBaixa = input.color(#c84646, "OB de baixa", group = grpV)  // rgb(200,70,70) do NTSL
corMitig = input.color(#787878, "OB mitigado", group = grpV)
transp   = input.int(85, "Transparencia do preenchimento", minval = 0, maxval = 100, group = grpV)
espLinha = input.int(1, "Espessura da borda", minval = 1, maxval = 5, group = grpV)

// -------------------------------- Tipo ---------------------------------------
type OrderBlock
    box   caixa
    line  meio
    label texto
    float topo
    float fundo
    bool  bull
    bool  mitigado

var array<OrderBlock> obsAlta  = array.new<OrderBlock>()
var array<OrderBlock> obsBaixa = array.new<OrderBlock>()

// ------------------------------ Utilidades -----------------------------------
apagar(OrderBlock ob) =>
    box.delete(ob.caixa)
    if not na(ob.meio)
        line.delete(ob.meio)
    if not na(ob.texto)
        label.delete(ob.texto)

criar(bool bull) =>
    float t = high[2]
    float f = low[2]
    color c = bull ? corAlta : corBaixa
    int   x = bar_index - 2

    box bx = box.new(x, t, bar_index, f, border_color = c, border_width = espLinha, bgcolor = color.new(c, transp), extend = extend.right)

    line ml = na
    if mostrarMeio
        ml := line.new(x, math.avg(t, f), bar_index, math.avg(t, f), color = c, style = line.style_dotted, extend = extend.right)

    label lb = na
    if plotarTexto
        lb := label.new(x, bull ? f : t, "OB", xloc.bar_index, yloc.price, color.new(color.black, 100), bull ? label.style_label_up : label.style_label_down, c, size.tiny)

    OrderBlock.new(bx, ml, lb, t, f, bull, false)

// Marca mitigacao e limpa o que passou do limite. Percorre de tras pra frente
// porque pode remover elementos durante o laco.
processar(array<OrderBlock> arr) =>
    if array.size(arr) > 0
        for i = array.size(arr) - 1 to 0
            OrderBlock ob = array.get(arr, i)
            if not ob.mitigado and (ob.bull ? low <= ob.topo : high >= ob.fundo)
                ob.mitigado := true
                // Dois `if` separados, sem `else`: o Pine compara o tipo de retorno
                // dos ramos de um if/else, e aqui um ramo terminava em `label` e o
                // outro em `OrderBlock` (array.remove devolve o elemento). Isso dava
                // CE10235. Nenhum dos dois valores e usado; sem `else` nao ha
                // comparacao de tipo.
                if exibirMitig
                    box.set_extend(ob.caixa, extend.none)
                    box.set_right(ob.caixa, bar_index)
                    box.set_border_color(ob.caixa, corMitig)
                    box.set_bgcolor(ob.caixa, color.new(corMitig, math.min(transp + 8, 100)))
                    if not na(ob.meio)
                        line.set_extend(ob.meio, extend.none)
                        line.set_x2(ob.meio, bar_index)
                        line.set_color(ob.meio, corMitig)
                    if not na(ob.texto)
                        label.delete(ob.texto)
                        ob.texto := na
                else
                    apagar(ob)
                    array.remove(arr, i)

// --------------------------- Deteccao dos gaps -------------------------------
gapAlta  = low  > high[2] and close[1] > high[2]
gapBaixa = high < low[2]  and close[1] < low[2]

var bool obAtivoAlta  = false
var bool obAtivoBaixa = false

if not gapAlta
    obAtivoAlta := false
if not gapBaixa
    obAtivoBaixa := false

bool novoAlta  = false
bool novoBaixa = false

if gapAlta and not obAtivoAlta and bar_index >= 2
    obAtivoAlta := true
    novoAlta    := true
    array.push(obsAlta, criar(true))
    if array.size(obsAlta) > maxOB
        apagar(array.shift(obsAlta))

if gapBaixa and not obAtivoBaixa and bar_index >= 2
    obAtivoBaixa := true
    novoBaixa    := true
    array.push(obsBaixa, criar(false))
    if array.size(obsBaixa) > maxOB
        apagar(array.shift(obsBaixa))

processar(obsAlta)
processar(obsBaixa)

// ------------------------------- Alertas -------------------------------------
alertcondition(novoAlta,  "Novo OB de alta",  "SMC: novo Order Block de alta em {{ticker}} ({{interval}})")
alertcondition(novoBaixa, "Novo OB de baixa", "SMC: novo Order Block de baixa em {{ticker}} ({{interval}})")
```

## Mapeamento, linha a linha

| Pine | `OrderBlockDetector` |
|---|---|
| `gapAlta` / `gapBaixa` | `FVGDetector.update` |
| `criar()` → `t = high[2]`, `f = low[2]` | `_zone()` → `top`, `bottom` |
| `bar_index - 2` (x da caixa) | `OrderBlock.time` — a barra que marca |
| `obAtivoAlta` / `obAtivoBaixa` (trava) | `_runs()` — gaps em barras consecutivas são um evento só |
| `processar()` → `low <= topo` / `high >= fundo` | `TrackedZone.mitigated` |
| `ob.mitigado` | `not TrackedZone.usable` |
| caixa parando de estender | a região deixa de ser oferecida ao setup |
| `maxOB` (limite de desenhos) | `_MAX_ZONES` |
