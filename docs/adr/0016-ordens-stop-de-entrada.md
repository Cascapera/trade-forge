# ADR-0016 — Ordens stop de entrada (fill no rompimento do nível)

- **Status**: aceito
- **Data**: 2026-07-23
- **Contexto do PR**: PR-207

## Contexto

A engine sabe entrar de dois jeitos. A mercado no próximo open — o suficiente para os setups de indicador, onde "IFR cruzou 30, compre" não tem preço de entrada preferido. E por **ordem limite pendente** (ADR-0014), que preenche quando o preço **volta** a um nível: a limite descansa do lado que o mercado tem que reencontrar (compra abaixo, venda acima), servindo os setups de estrutura SMC, que entram no toque de uma zona.

Os setups de swing clássicos (Larry Williams / Stormer — 9.1, 9.2, 9.3, 9.4, Ponto Contínuo) entram no sentido **oposto**: no **rompimento**. *"A MME9 virou para cima; compre quando o preço romper a máxima do candle que virou."* A ordem não espera o preço voltar — espera ele **avançar através** de um nível e entra a favor do movimento. Isso é uma **ordem stop de entrada** (buy-stop acima do mercado, sell-stop abaixo), o espelho geométrico da limite.

A limite não expressa isso: uma compra-limite acima do mercado é recusada no `Signal.__post_init__` (ADR-0014) — justamente o lugar onde uma compra-stop precisa descansar. Sem a ordem stop, nenhum desses cinco setups entra como o método manda. E modelar a entrada como "a mercado no fechamento que rompeu" mede um trade diferente: o rompimento acontece no meio da barra, num preço melhor que o fechamento, e o stop dimensionado no fechamento sai errado.

Adicionar mais um tipo de ordem pendente **estende** o ADR-0014, daí este ADR (`AGENTS.md §8`).

## Decisão

A engine ganha **ordens stop de entrada**: uma ordem submetida com `stop_price` fica pendente e preenche no primeiro candle **posterior** cujo range **alcança** o nível no sentido do rompimento.

- **Compra-stop** descansa **acima** do mercado; preenche quando `high ≥ stop_price`, ao preço `max(open, stop_price)`.
- **Venda-stop** descansa **abaixo**; preenche quando `low ≤ stop_price`, ao preço `min(open, stop_price)`.

É a mesma fórmula-rumo-ao-open da limite — mas como o stop descansa do lado oposto, o resultado é o **pior** preço, não o melhor. A regra que isso fixa:

> **No gap, o stop preenche pior.** Se a barra abre já além do gatilho, o fill é o open. Um stop é uma ordem a mercado *disparada* no nível — "este preço **ou pior**" — o oposto da limite ("este preço ou melhor").

Exemplo: compra-stop em 105. A barra abre em 107 (gapou acima do gatilho) → preenche em **107**, não em 105. `max(open=107, 105) = 107`. A barra abre em 103 e sobe à máxima 106 (cruzou 105 intrabar) → preenche em **105**. `max(open=103, 105) = 105`.

A regra anti-lookahead é herdada inteira: a ordem carrega o `decided_at` da barra que a colocou, e a guarda `fill_bar > decided_at` impede a barra decisora de preencher.

### O teto do fill: metade do risco

O preço "ou pior" não pode ser *qualquer* pior. O lote foi dimensionado no gatilho (`PercentRiskManager`), então um fill longe dele abre uma posição carregando risco que ninguém autorizou. **Um stop cujo fill se afasta do gatilho mais que 50% da distância do stop-loss é descartado** — a ordem some, não fica esperando, exatamente como a limite que gapou além do próprio stop (`_survives_the_gap`).

Dois eventos caem aqui, e o segundo é o que um backtest jamais suspeitaria:

1. **Gap através do gatilho.** Compra-stop em 1.10500, SL 1.10000 (500 pontos = 1R). A barra abre em 1.15000 → o fill carregaria **10R**. Um gap de notícia num índice, e uma conta que prometeu 1% de risco leva 10%.
2. **Elegibilidade adiada, sem gap nenhum.** A ordem descansa enquanto outra posição ocupa o slot (passo 4 do `on_bar`). O preço rompe numa barra em que ela não é elegível; quando o slot libera, o mercado está 1500 pontos adiante e `max(open, level)` entrega esse preço. É artefato da engine, não evento de mercado — e sem o teto vira entrada a mercado num preço que a estratégia nunca escolheu.

Os 50% são **julgamento, não derivação**: é por isso que moram numa constante nomeada (`_MAX_ENTRY_SLIP`) e não embutidos numa comparação. Mudar o número muda todo backtest de rompimento.

### O SL do recém-nascido: ambiguidade continua perdendo

Numa entrada stop preenchida *dentro* da barra, o SL fica **atrás** de onde o preço veio — a mínima da barra pode muito bem ter impresso antes de a posição existir. A engine mantém a leitura da limite: **o alvo exige prova pelo fechamento, o stop basta ser alcançado**.

Consequência que parece bug e não é: um rompimento pode registrar perda numa barra que **fechou acima da entrada** (teste de ouro `test_a_stop_entry_books_a_loss_on_a_bar_that_closed_above_it`). A barra é genuinamente ambígua e ambiguidade resolve contra o trade em toda esta engine. A alternativa — resolver a favor — é uma engine que decide as próprias dúvidas em benefício próprio e reporta rompimento que ninguém consegue operar.

O que **mudou** aqui: um fill *no open* (gap através do nível) não é recém-nascido coisa nenhuma. A ordem executou no primeiro tick e a barra inteira pertence à posição, então vale a leitura ordinária — alvo pela **máxima**. O passo 4 passa `born_this_bar=resting.price != candle.open`, o mesmo idioma dos passos 1 e 3. Fixar `True` negava à posição um alvo que a barra comprovadamente alcançou, e fazia duas rotas para a mesma posição (stop gapada vs a mercado no mesmo open) darem P&L diferente. Corrige também a limite gapada, herdada do ADR-0014.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Ordem stop de entrada (escolhida)** | Mede o rompimento no nível exato; espelha a máquina da limite; serve os cinco setups de swing de uma vez | Peça nova no broker; a semântica de gap (pior) tem que ser pinada por teste |
| **A mercado no fechamento que rompeu** | Zero código novo | O rompimento é intrabar, num preço melhor que o fechamento → mede um trade pior que o real, e o stop dimensionado no fechamento fica errado |
| **A mercado no próximo open** (o que já existe) | Trivial; anti-lookahead grátis | Descola do nível; num gap mede uma entrada fictícia — o caso que o rompimento existe para capturar |

## Trade-off aceito

Aceitamos a semântica de gap **invertida** em relação à limite (o stop preenche no open — pior — quando gapa através do gatilho) como o modelo honesto de uma ordem a mercado disparada, **até o teto de meio-R**. Além dele o trade deixa de ser o que a estratégia precificou e a ordem é descartada; o preço disso é que rompimentos com gap grande simplesmente não são operados no backtest — silêncio no lugar de uma perda gigante. Preferimos o silêncio: uma perda de 10R num backtest contamina drawdown, Sharpe e toda métrica da Fase 3 com um trade que nenhum gestor de risco autorizou.

O otimismo residual é o espelho do da limite: a limite assume que um pavio de um tick te preencheu; o stop assume que o rompimento de um tick te disparou. Ambos são o preço de simular ordem pendente sem dados de tick.

## Simetria com a limite: o que **não** precisou de código novo

- **Anti-lookahead**: a mesma guarda `fill_bar > decided_at` no `_fill_resting`.
- **`_survives_the_gap` é um no-op para stops** — e por isso a chamada dele fica **inalterada**. O stop-loss de um long fica *abaixo* do gatilho e o gatilho só dispara subindo, então o fill (`≥ gatilho`) está sempre acima do stop; nunca gapa através dele. (A limite precisa da guarda porque preenche *descendo*, em direção ao próprio stop.) **Isso vale enquanto o SL estiver além do gatilho** — um SL do lado errado (compra-stop em 1.10500 com SL em 1.10600) faz a ordem ser silenciosamente descartada, mesmo comportamento que a limite já tem hoje. Está no backlog.
- **Mas o no-op é exatamente o buraco**: a limite tem uma rede para o gap e o stop não tinha nenhuma, porque preenche *afastando-se* do próprio stop. Daí `_survives_the_slip` — a guarda espelhada descrita acima, a única peça de fill genuinamente nova deste ADR.
- **Ciclo de vida, cancelamento (`Broker.cancel`), uma-ordem-por-barra, unicidade do `client_id`**: toda a máquina `_resting` já existe. O stop entra como mais um caso em `_resting_price`.

## Consequências

- `Signal` e `OrderRequest` ganham `stop_price: Money | None` (aditivo; irmão do `limit_price`). **No máximo um dos dois é setado** — uma ordem é limite **ou** stop, nunca os dois.
- Validação de lado **espelhada** no `Signal`: uma compra-stop tem que estar **acima** da referência, uma venda-stop **abaixo** — o inverso da limite. O lado errado é erro de sinal: uma compra-stop *abaixo* do mercado dispararia na hora, virando ordem a mercado silenciosa dimensionada contra um nível que o preço nunca teve que romper.
- `_Resting` passa a carregar se é limite ou stop; `_resting_price` ramifica na condição de cruzamento e na direção do gap.
- O `PercentRiskManager` mede a distância do stop a partir do `stop_price` quando ele existe (como já faz com o `limit_price`) — senão o lote sai errado em todo setup de rompimento, porque o preço de entrada não é o fechamento que decidiu.
- **`_survives_the_slip`** descarta o fill que se afastou do gatilho mais que `_MAX_ENTRY_SLIP` (0.5) da distância do stop. Ordem sem stop-loss não foi dimensionada contra distância nenhuma e passa.
- O passo 4 do `on_bar` passa `born_this_bar=resting.price != candle.open`: fill no open teve a barra inteira, leitura ordinária; fill dentro da barra, leitura de recém-nascido.
- Testes de ouro cobrem: fill ao nível quando o pavio rompe; fill ao open (pior) num gap dentro do teto; espelho venda-stop; **nenhum** fill na barra que colocou a ordem (anti-lookahead, por mutação); sizing pelo `stop_price`; gap acima do teto descartado (long e short); elegibilidade adiada descartada; a perda na barra que fechou a favor, com P&L inteiro; a entrada gapada batendo trade a trade com a entrada a mercado equivalente. Uma property (hypothesis) fixa o que exemplo nenhum fixa: **um stop nunca preenche melhor que o gatilho**, e sempre dentro de `[low, high]`.
- O `engine-guardian` revisa antes de qualquer setup consumir a ordem stop — é mudança de corretude no caminho de fill.
- **Fora de escopo (próximos PRs)**: expor "entrar no rompimento" na DSL (operador de inclinação `rising`/`falling` + captura de candle-referência) e os setups 9.1 / 9.4 / 9.2 / 9.3 / Ponto Contínuo que consomem esta ordem.
