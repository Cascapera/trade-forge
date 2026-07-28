# ADR-0018 — Mover o stop de uma posição aberta (`modify_stop`)

- **Status**: aceito
- **Data**: 2026-07-27
- **Contexto do PR**: PR-210

## Contexto

A engine sabe abrir e fechar posição, e sabe colocar e retirar ordem pendente (ADR-0014, ADR-0016). O que ela **não** sabe é mexer numa posição que já existe.

Isso não é um parâmetro que faltou — é um verbo que não existe. O `Broker` tem quatro: `submit`, `cancel`, `positions`, `trades`, e nenhum alcança uma posição aberta. Do lado do broker de backtest, o `_Protection` é um dataclass **frozen** armado no fill (`_arm_protection`) e nunca mais tocado até a saída.

Dois métodos independentes pedem a mesma peça, e os dois estão registrados no backlog desde 21/07/2026:

- **A condução do swing.** A regra do autor para o rompimento da MME9: ao **tocar** 2x1 do risco, o stop vai para 0x0 (o preço de entrada); e toda barra que **fecha** do outro lado da MME9 traz o stop para o extremo dela. Fechar do outro lado **não é a saída** — é o gatilho que aperta o stop. Quem encerra o trade é sempre o stop sendo atingido, o que também significa que o setup roda **sem alvo** (`take_profit_rr=None`).
- **A condução do SMC.** Breakeven no 1º BOS a favor, depois stop atrás dos topos/fundos válidos.

Sem essa peça, os dois setups saem por múltiplo de R — que é um trade diferente do que o método toma.

## Decisão

O protocolo `Broker` ganha um quinto verbo:

```python
def modify_stop(self, symbol: str, stop_loss: Money, decided_at: dt.datetime) -> bool: ...
```

A estratégia o alcança por um intent novo, **`SignalKind.MODIFY_STOP`**, roteado pelo loop — simetria exata com o `CANCEL` do ADR-0014. Como o cancel, este intent **nunca vira `OrderRequest`**: não tem volume, não tem fill e não entra na fila que o broker preenche.

### O `decided_at` é carimbado pelo loop, e é o núcleo desta decisão

O loop passa `decided_at=candle.time` — a estratégia não tem como mentir sobre quando decidiu, exatamente como já acontece na entrada (`_to_order` carimba a partir do contexto).

A ordem do loop **já** garante o comportamento certo por construção: `broker.on_bar(N)` roda no passo 1 e `strategy.on_bar(N)` no passo 2, então uma modificação decidida na barra N só pode agir a partir da N+1.

Então por que o carimbo importa? **Porque é ele que arma a guarda.** O `loop._reject_lookahead` recusa qualquer fill cujo `decided_at >= candle.time`. Se um stop modificado mantivesse o carimbo da **entrada**, a guarda nunca dispararia sobre ele, e a proteção passaria a ser **geográfica** — funciona porque as linhas estão nessa ordem — em vez de **estrutural** — funciona porque a engine verifica. Bastaria um `MT5Broker` que preencha diferente, ou uma reordenação futura do loop, para não sobrar nada checando.

É o mesmo tipo de fragilidade que o backlog já registra sobre o `OrderRequest` não validar limite do lado errado: *"a proteção é geográfica, não estrutural"*.

O erro que isso evita é caro justamente por ser invisível: um trailing que herdasse o carimbo da entrada poderia sair **dentro** da barra que decidiu o nível novo, usando o fechamento de N para sair no meio de N. Nenhum teste de valor quebraria, nenhum número pareceria errado — o backtest só ficaria **sistematicamente melhor**, porque um trailing que "sabe" a barra sempre sai melhor que um que não sabe.

### `_Protection` ganha um `stop_decided_at`

Hoje um único `decided_at` serve stop e alvo, e significa "quando a entrada foi decidida". Depois de uma modificação isso deixa de ser verdade para o stop e continua verdade para o alvo. Um campo só obrigaria a mentir sobre um dos dois; a saída protetiva passa a usar o carimbo **do nível que foi atingido**.

### `Position` ganha um `initial_stop_loss` — pelo mesmo motivo

O `_Protection` não é o único lugar onde um campo guardava dois fatos que só coincidiam enquanto o stop não podia andar. A `Position` tinha um `stop_loss`, gravado no fill e nunca mais tocado, e ele respondia a **duas** perguntas ao mesmo tempo: *"onde está meu stop agora?"* e *"contra que distância esse lote foi dimensionado?"*.

No momento em que o stop anda, são duas respostas diferentes — e a `Position` é a **única** coisa que a engine mostra à estratégia (`Context.position`). Mantê-la congelada faria a engine cobrar a regra de apertar contra um estado que ela esconde de quem tem que obedecer:

- compra a 1.10000, stop 1.09000, 1R = 0.01000
- o preço toca +2R → a condução manda o stop para 0x0. Vai para 1.10000 ✔
- barra seguinte: trade vivo e ganhando, o stop nem é ameaçado; a MME9 nomeia **1.09950**
- a estratégia se defende como qualquer autor escreveria: `if novo > position.stop_loss`. Lê **1.09000** (obsoleto), passa, e a engine — que sabe que o stop está em 1.10000 — levanta `EngineError`

O backtest inteiro morre numa sequência banal, e **nenhuma leitura possível teria salvado a estratégia**. A alternativa de mandar cada estratégia espelhar o stop num campo próprio é a duplicação de estado que o invariante "estratégia única" (AGENTS §5.3) existe para impedir, e que quebra no dia em que o `MT5Broker` recusar ou ajustar uma modificação por stop level do servidor.

Então:

- **`Position.stop_loss` passa a ser o stop VIGENTE.** O broker o move junto com o `_Protection`. É também o que uma corretora de verdade reporta: o `POSITION_SL` do MT5 é o stop atual, não o que a entrada carregava. Está escrito no protocolo: um adaptador que move o stop na corretora e continua devolvendo o nível da entrada volta a criar as duas respostas.
- **`initial_stop_loss` é o da entrada, congelado.** É contra ele que o `PercentRiskManager` dimensionou o lote, e por isso é o único denominador honesto do R: dividir pelo stop corrente faz um trade levado a 0x0 dividir por risco **zero**, e um trailing no lucro dividir por risco **negativo**. Nenhum dos dois é um número — e são exatamente os trades que os setups de condução produzem.
- **`ClosedTrade.stop_loss` reporta o `initial_stop_loss`**, porque é o número que o `r_multiple` divide; um registro cujo risco e cujo R viessem de stops diferentes se contradiz. A consequência é explícita: um trade que sai num stop puxado reporta `reason='sl'` com `exit_price` distante desse campo.

Nada mais muda de comportamento — a persistência e as métricas continuam lendo o mesmo número de sempre.

### O stop nunca afrouxa

Uma compra exige `novo >= atual`, uma venda `novo <= atual`. **Igual é permitido** — uma estratégia que recalcula o mesmo nível a cada barra não pode explodir por isso.

Afrouxar levanta `EngineError`. Não é conservadorismo: o lote já foi dimensionado contra o stop original (`PercentRiskManager`), então afastar o stop aumenta o risco de uma posição já dimensionada — martingale com outro nome. E é um **erro de sinal**, a mesma família de bug que o `Signal` já persegue nas validações de lado; erro de sinal aqui não se anuncia, ele só aparece no extrato.

O retorno `False` fica reservado para **"não havia o que modificar"** — nenhuma posição aberta. Em live isso é corrida, não bug, exatamente o argumento que o `Broker.cancel` já usa para devolver `False` em vez de levantar.

### Armar onde não havia proteção é permitido

Uma posição aberta sem `stop_loss` (o `_arm_protection` deixa `_protection` em `None`) pode receber um stop pelo `modify_stop`. Recusar seria a engine se negando a tornar uma posição **mais segura** — e o invariante que este método protege é exatamente esse. O alvo não é afetado: continua `None`.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Intent novo + verbo no `Broker`** (escolhida) | simetria com o `CANCEL`; a estratégia segue sem tocar no broker; o loop carimba o instante | um membro novo no enum e um método novo em 3 implementações |
| Estratégia chamando o broker direto | menos indireção | **impossível hoje e indesejável**: a estratégia recebe `Context` e devolve `Signal`; dar-lhe o broker é dar-lhe sizing, veto e carimbo de decisão |
| `EXIT` com stop novo | nenhum enum novo | um exit **fecha**; sobrecarregar o intent que encerra posição para também não encerrar é o tipo de ambiguidade que vira bug de leitura |
| Reusar o `decided_at` da entrada | um campo a menos | **é o bug** — desarma o `_reject_lookahead` para o nível que passou a mudar no meio do trade |
| Deixar a `Position` congelada e expor o stop vigente num 6º verbo de leitura do `Broker` | a `Position` continua sendo a foto da entrada | mais um verbo que todo adaptador implementa, no mesmo PR que argumenta que `modify_stop` é a fronteira mínima — e `Position.stop_loss` continua sendo o primeiro campo que qualquer um lê por engano |
| Cada estratégia espelhar o próprio stop | zero código na engine | duplicação de estado que o invariante "estratégia única" existe para impedir; e o espelho nunca fica sabendo quando o broker recusa ou ajusta a modificação |
| Deixar o afrouxamento passar | fiel ao MT5, que aceita | um erro de sinal viraria aumento de risco silencioso numa posição já dimensionada |

## Trade-off aceito

**O `Broker` fica maior.** Cada verbo novo é código que todo adaptador futuro — `MT5Broker` à frente — precisa implementar e provar. Aceitamos porque a alternativa é deixar dois métodos do autor fora da engine, e porque `modify_stop` é a fronteira mínima: não redimensiona, não mexe no alvo, não toca em ordem pendente.

**O `stop_decided_at` é um campo cuja razão de existir não é óbvia lendo o dataclass.** Fica documentado aqui e no próprio código, porque o dia em que alguém "simplificar" os dois campos num só é o dia em que o lookahead volta sem quebrar nenhum teste.

## Consequências

- `SignalKind.MODIFY_STOP` novo; `Signal` exige `stop_loss` nesse intent; `OrderRequest` o recusa como recusa o `CANCEL`.
- `Broker.modify_stop` novo no protocolo, implementado em `BacktestBroker` e em `ImmediateFillBroker` (que devolve `False` sempre — não tem maquinaria protetiva, tudo preenche no próximo open).
- `_Protection` passa a ter `stop_decided_at`; a saída protetiva usa o carimbo do nível atingido.
- `Position` passa a ter `initial_stop_loss`; `stop_loss` vira o nível vigente e o `Portfolio` ganha um `amend_stop`. `r_multiple` e `ClosedTrade.stop_loss` passam a medir pelo inicial — mesmo número de antes em todo trade que não move o stop.
- `modify_stop` valida o `decided_at` na chamada (`_require_utc`), como o `OrderRequest` faz na construção: é o único instante da engine que nunca vira ordem, então sem isso um datetime ingênuo só explodiria barras depois, de dentro do caminho de saída.
- O loop registra em `debug` a modificação **recusada**, sem nomear a causa: normalmente é uma condução puxando o stop de uma entrada que ainda não preencheu, mas o `ImmediateFillBroker` recusa *segurando* posição (não guarda nível protetivo nenhum), e daqui as duas são a mesma resposta. Silêncio ali é idêntico a uma regra de trailing que rodou e funcionou; nomear uma causa que o loop não checou é pior ainda.
- `ClosedTrade.stop_loss` muda de significado para quem consome: a coluna em `tradeforge_db.models` e o campo em `tradeforge_api.schemas` ganharam comentário dizendo que é o stop **dimensionado**, não o de saída — senão a tela de trades vira um bug reportado contra a engine.
- **Fora de escopo, deliberadamente:** parciais (estavam na mesma fatia do backlog), mover o `stop_loss` de uma **ordem pendente** (isso é cancelar e recolocar), e a condução da MME9 em si — que é o PR irmão, consumindo esta peça.
- **Dívida registrada:** um stop movido para o lado errado do preço corrente (uma compra com stop acima do mercado) é aceito e sai na barra seguinte. É a mesma lacuna que o backlog já tem para `stop_loss` × (`limit_price` | `stop_price`), e fecha junto com ela.
