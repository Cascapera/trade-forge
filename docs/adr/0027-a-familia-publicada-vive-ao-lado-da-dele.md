# ADR-0027 — A família publicada do Larry Williams vive ao lado da dele

- **Status**: aceito
- **Data**: 2026-09-10
- **Contexto do PR**: PR-231

## Contexto

O `mme9_breakout` e o `ponto_continuo` são os setups **dele**, ditados e fechados pergunta a
pergunta. O primeiro chama-se 9.1, e é 9.1 no sentido de que veio da mesma família — mas a regra
que ele ditou não é a que a literatura publica:

- **A dele**: uma barra **fecha através** da MME9. A ordem persegue: enquanto não preencher, cada
  nova barra do mesmo lado vira a referência (*"vale sempre a última barra"*). Um fechamento de
  volta encerra a virada e também **aperta o stop** da posição aberta.
- **A publicada**: a **inclinação** da média muda de sinal. Marca-se a barra que entortou a linha,
  e a ordem espera **ali**, sem perseguir, enquanto a média continuar apontando para o mesmo lado.
  O que anula é a linha entortar de volta. Sobre condução, o texto não diz nada.

Ele pediu (10/09/2026): *"vamos criar um novo 9.1 e chamar ele de 9.1 original, e os demais como
manda o Larry Williams, pode criar todos eles"*. Então 9.2, 9.3 e 9.4 vêm atrás deste, e a decisão
de forma precisa valer para os quatro.

O `ADR-0016` já previa isto: fechou dizendo que os setups *"9.1 / 9.4 / 9.2 / 9.3 / Ponto
Contínuo"* ficavam para PRs seguintes. O que ele não decidiu é o que fazer quando a regra
publicada **discorda** da regra dele com o mesmo nome.

## Decisão

Cada setup publicado é um **tipo próprio na DSL e uma classe própria na engine**, ao lado dos dele,
sem tocar nos existentes. O primeiro é `mme9_turn` / `Mme9TurnStrategy`; `mme9_breakout` continua
exatamente como está.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Tipo próprio por setup** (escolhida) | Cada regra tem um nome chamável; a grade de estudo compara os dois como dois pontos; nenhuma linha do setup dele muda | Cinco setups viram nove; o construtor cresce; duas coisas chamadas 9.1 na mesma lista |
| Um parâmetro `variant: autor \| publicado` no `mme9_breakout` | Uma entrada só na lista; o eixo de estudo sai de graça | Funde duas máquinas de estado num `if`; qualquer regra futura de um lado precisa de guarda no outro; e a imutabilidade por versão fica ambígua para documentos já salvos |
| Só o publicado, aposentando o dele | A lista fica pequena | Joga fora o método **dele**, que é o produto; e as duas leituras não são a mesma aposta |
| Um `average: EMA \| SMA` no publicado, para a inclinação valer por si | A distinção entre fechar e inclinar deixa de ser vazia | A literatura diz **exponencial**; seria eu inventando um grau de liberdade que ninguém pediu |

## Trade-off aceito

**Duplicação de forma em troca de separação de regra.** Os dois setups compartilham a geometria da
ordem (`_breakout_entry`) e nada mais: cada um guarda seu próprio estado, lê seu próprio evento e
conduz do seu jeito. Uma correção de comportamento num deles não chega no outro, e isso é
deliberado — eles não são a mesma aposta.

**Dois "9.1" na tela**, resolvido só por rótulo (`MME9 breakout` e `MME9 turn (9.1 original)`).
Aceito porque o alternativo, renomear o dele, quebraria os documentos salvos, que são imutáveis por
versão.

## Consequências

- ⚠️ **Numa EMA, a armação dos dois é o mesmo evento — e isso é aritmética, não coincidência.**
  `ema = anterior + a(fechamento − anterior)`, então `ema > anterior` e `fechamento > ema` são a
  mesma desigualdade. O que separa os dois setups é o que acontece **depois** da ordem colocada:
  perseguir ou não, e conduzir ou não. Todo teste que tentar separá-los pela barra da armação
  passa nos dois — está pinado em `test_slope_and_close_are_the_same_event_on_an_ema`.
- **`breakeven_at_r` nasce `None` nos publicados.** É a primeira vez que um setup da engine tem
  esse default; nos dele é `2`. O motivo é que a fonte não fala em mover stop, e o parâmetro existe
  só para a pergunta *"quanto isto renderia com o 2x1 dele por cima"* continuar sendo fazível.
- **Nada de `entry_point` nem de filtro de média longa nos publicados.** Os padrões de barra e o
  filtro de direção são enxertos dele nos setups dele.
- Os próximos (`9.2`, `9.3`, `9.4`) seguem esta forma. O 9.2 e o 9.3 diferem só na contagem de
  fechamentos de correção, e se viram um tipo com contador ou dois tipos é decisão do PR deles —
  esta ADR não a antecipa.
- ⚠️ **Uma divergência de fonte continua aberta** e vai bater no PR do 9.2: o que conta como
  "fechamento de correção". Uma fonte diz fechamento **abaixo da mínima da barra anterior**; outra
  diz fechamento abaixo do **candle de referência**, o de maior fechamento da perna. A segunda é a
  que deixa 9.2 e 9.3 coerentes (um fechamento contra dois, contra a mesma referência), e é a que
  eu pretendo usar se ele não disser o contrário.
