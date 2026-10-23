# ADR-0033 — Os sinais de um mercado rodam num processo só

- **Status**: aceito (13/10, por ele)
- **Data**: 2026-10-13
- **Contexto do PR**: pergunta dele de 13/10 ("e se a gente escolher o ativo e os setups que o live
  vai olhar nele? ele já tem os dados ali"), mais os dois pedidos que vieram em seguida: um botão
  para reiniciar sem reiniciar o PC, e a tela organizada por ativo → time frame → setups;
  implementação em quatro PRs depois do aceite
- **Relaciona-se com**: ADR-0029 (o contexto do mercado é lido uma vez por lote), ADR-0032 (várias
  corretoras), PR #437 (a sessão percorre o histórico em vez de guardá-lo), AGENTS.md §5.3 e §5.7

## Contexto

O `tradeforge-signals` mantém **um processo por par (setup, mercado)**: para cada linha de
`live_setup_markets` ele sobe um `tradeforge_api.live.process --mode signal`, que aquece o setup
sobre o histórico do disco e depois segue as barras do stream do Redis. Em 13/10 eram 14 sinais em 5
mercados (BIT, WIN, WDO, Usa500 e UsaTec, todos M5), portanto **14 processos**: 4 deles no WIN e 4
no WDO, lendo o mesmo histórico e o mesmo stream.

Cada processo paga duas vezes um custo que não depende do setup:

1. **~80–100 MB só para existir**: o Python e as bibliotecas (numpy, pyarrow, SQLAlchemy, o motor).
2. **O que a leitura do histórico deixa para trás**: depois do PR #437 a sessão percorre o
   histórico em pedaços em vez de guardá-lo inteiro, mas a memória que essa leitura usou não volta
   toda para o Windows. São ~270 MB no WIN (100 mil barras) e ~500 MB no Usa500 (662 mil barras).

O que a sessão precisa de verdade depois do aquecimento, que é o estado do setup, ocupa ~0,3 MB.
Hoje os 14 sinais reservam **5,2 GB**. O plano dele é forward test em centenas de ativos
(`plano-banco-de-combinacoes-para-ml`): com 100 ativos e 3 setups cada seriam 300 processos e
~110 GB, que não cabem nesta máquina.

O motor já resolve esse problema em outro lugar: nas varreduras, `run_batch` (ADR-0029) lê cada
barra uma vez e a entrega a vários runs, cada um passando pelo **mesmo `iter_run`** de um run
sozinho, por meio de um alimentador de uma barra só (`_Feed`).

## Decisão

**Os sinais de um mesmo mercado (instrumento + time frame) rodam num processo só: o processo lê
cada barra uma vez (do disco no aquecimento, do stream ao vivo) e a entrega a cada setup em
sequência. Cada setup continua sendo uma sessão inteira: tem linha própria em `live_sessions`, id
próprio, estratégia própria, corretora própria (`SignalBroker` sobre `BacktestBroker`) e sinais
próprios. Todos rodam no mesmo motor e com o mesmo `iter_run`.**

O que muda é só **quantos processos** existem. O que cada sessão decide, posta e grava continua
igual.

### Escopo

- **Só as sessões de sinal**, isto é, as que o `tradeforge-signals` sobe. Paper e live continuam
  com um processo por sessão (`tradeforge-session`), sem nenhuma mudança de comportamento: o
  caminho que pode tocar numa conta (AGENTS.md §5.7) não ganha nada com isso e só ganharia risco.
- **O agrupamento é por (instrumento, time frame)**, porque é isso que define o stream
  (`Subscription`). Um setup em M15 e outro em M5 no mesmo ativo ficam em processos diferentes.
- **A tela passa a seguir a mesma forma** (pedido dele, 13/10): ativo → time frame → setups. O bloco
  do time frame é exatamente um processo, então é nele que ficam a situação e o botão de reiniciar.
  Ver "O que a tela mostra e controla".

### A vida de um processo de mercado

1. **Aquecimento**: o histórico é lido uma vez (`splice` + `iter_candles`, como hoje). Cada barra
   vai, em sequência, para o `iter_run` de cada membro, cada um sobre a sua `BacktestBroker` de
   aquecimento. A recusa de buraco entre o disco e o stream (`_refuse_a_hole`) vale para o mercado
   inteiro, porque o buraco é do mercado.
2. **Passagem para o ao vivo, membro a membro**: o trade herdado do aquecimento, a linha em
   `live_sessions`, o batimento e o `hand_over` acontecem para cada membro, na mesma ordem e pelas
   mesmas razões de `run_session`. Um membro que ainda está seguindo um trade herdado convive com
   os outros, que já estão ao vivo; o trade herdado é estado do membro, não do processo.
3. **Ao vivo**: cada barra do stream é entregue a todos os membros, e cada um grava a sua barra
   (`_persist`) na sua própria transação curta.

**A ordem dos passos fica escrita num lugar só.** `run_session` é dividida nos passos que já tem
(carregar, aquecer, passar para o ao vivo, uma barra, fechar), e tanto a sessão sozinha quanto o
processo de mercado chamam esses mesmos passos. A sessão sozinha (paper e live) continua chamando-os
na mesma ordem de hoje, e os testes atuais dela continuam valendo como estão.

### Isolamento

- **A falha de um membro é só dele** (o mesmo princípio de `run_batch`). Um membro cujo setup lança
  uma exceção tem a linha fechada como `failed`, com o motivo, e sai do grupo. Os outros seguem.
- **A falha do processo é de todos**: erro no stream, no disco ou no banco derruba o processo, e o
  supervisor o religa com a espera que já existe (15 s dobrando até 5 min).
- **Parar um membro não espera a próxima barra.** O pedido de parada continua por sessão
  (`live-session:<id>:stop`). O processo confere os pedidos de todos os membros toda vez que acorda
  da leitura do stream (no máximo a cada `block_ms`, 60 s, dentro dos 120 s que o supervisor espera
  antes de matar), fecha a linha do membro e o tira do grupo. O processo só termina quando foi
  sinalizado ou quando não sobrou nenhum membro.

### Quando os setups de um mercado mudam

| Mudança | O que acontece |
|---|---|
| Setup desligado ou ativo removido | O membro é parado **dentro** do processo; os outros não percebem |
| Setup ou ativo novo | Sobe **num processo próprio**, logo, como hoje; os sinais dele começam depois do aquecimento dele |
| Um membro falhou | O supervisor o religa **num processo próprio**, com a espera de sempre |
| Ele aperta "Reiniciar" no bloco do time frame | Os processos daquele mercado param e todos os membros sobem juntos num processo só |
| O supervisor (re)começa (reinício do PC, por exemplo) | Tudo o que precisa rodar sobe agrupado: um processo por mercado |

A regra única por trás da tabela: **o supervisor sobe juntos, num processo novo, todos os membros
de um mercado que deveriam estar rodando e não estão.** Quando o mercado não tem processo nenhum
(no começo), isso dá um processo com todos. Quando já tem um rodando, o que falta sobe num processo
à parte, sem reiniciar quem já está trabalhando.

**Por que não reiniciar o grupo para incluir o novo membro**: reiniciar derruba todos os membros do
mercado. Os sinais armados deles viram órfãos e são **cancelados no canal** (`orphans.py`), os sinais
em trade passam a ser seguidos pelo preço, e todos reaquecem por alguns minutos. Isso é ruído no
canal e atraso por causa de uma mudança que não tem nada a ver com eles. Manter o novo membro à
parte custa só memória, e só até ele apertar "Reiniciar" naquele mercado, na hora que escolher (com
o mercado fechado, de preferência), ou até o próximo começo do supervisor.

Uma **consolidação automática**, que juntaria os processos de um mercado quando ele está fechado e
nenhum membro tem sinal armado ou em trade, fica para depois, se os processos à parte começarem a se
acumular. Ela não faz parte desta decisão.

### O supervisor não morre quando o Redis ou o Postgres somem

Em 12/10, depois de um `wsl --shutdown`, o supervisor morreu e os sinais só voltaram quando ele foi
religado à mão. O laço principal lê o banco (`followed_pairs`) e monta os filhos fora de qualquer
`try`, então uma queda do Postgres ou do Redis encerra o processo, e com ele quem religaria as
sessões. **Uma queda passa a ser uma rodada perdida**: o supervisor registra o erro, espera a próxima
rodada e tenta de novo. É pré-requisito do resto (um botão na tela não adianta se não há ninguém
ouvindo) e vai num PR de correção próprio, antes dos outros.

### O que a tela mostra e controla

A tela de sinais passa a ser organizada como os processos:

```
WIN  (XP)                                   4 setups ligados · 1 armado · 1 em trade
 └─ M5   ● rodando há 3 h · 1 processo                                  [Reiniciar]
     ├─ Choch 15/5          ligado   em trade: compra 128.450, stop 128.300, alvo 129.200
     ├─ Continuação 15/5    ligado   —
     ├─ CHOCH - 5 MIN       ligado   armado: venda limite 128.900
     └─ CONTINUAÇÃO         ligado   —
 └─ M15  ● rodando há 3 h · 1 processo                                  [Reiniciar]
     └─ ...
```

- **Ativo** (bloco maior): resumo de quantos setups estão ligados e quantos sinais estão armados ou
  em trade.
- **Time frame** (bloco interno, que é um processo): a situação que o supervisor publica (rodando,
  aquecendo, religando depois de falha, parado), há quanto tempo, e quantos processos o mercado tem
  agora. Mais de um quer dizer que um setup novo ainda está à parte. Aqui fica o botão **Reiniciar**.
- **Setup** (dentro do time frame): ligar e desligar, os sinais em andamento (armado ou em trade,
  com entrada, stop e alvo) e as métricas **daquele setup naquele ativo**. O histórico completo abre
  ao lado, como hoje.

**O botão Reiniciar** grava um pedido no Redis, e o supervisor o atende na rodada seguinte (≤ 15 s):
pede para todos os processos daquele mercado pararem e sobe todos os membros juntos. Antes de
confirmar, a tela diz o que vai acontecer com os sinais em andamento (por exemplo, "2 armados serão
cancelados no canal, 1 em trade passa a ser seguido pelo preço"). O botão não reinicia o supervisor
em si: a tela roda no Docker e não pode iniciar um processo no Windows, e reiniciar as sessões do
mercado tem o mesmo efeito.

**A situação do supervisor aparece no topo da tela.** A cada rodada ele grava no Redis uma chave
com validade curta, com o horário e os processos de cada mercado. Se a chave venceu, a tela avisa
"supervisor parado: rode `tradeforge-sinais.ps1`".

Adicionar um setup a um ativo continua sendo pôr o ativo num setup existente (o time frame é do
setup, então no bloco M15 aparecem só os setups de M15), e criar um setup novo continua sendo "Watch
this setup live" a partir de um run. **O modelo de dados não muda.** A visão atual, por setup, com
as métricas somadas de todos os ativos, fica como uma segunda aba, porque é a que serve para avaliar
um setup.

### O que o supervisor passa a saber

- `wanted` passa a ser montado por mercado. A chave do processo vira o mercado mais a lista dos
  membros que ele carrega, e cada membro guarda o seu id de sessão (um id por lançamento, como
  antes).
- **Quem está vivo é contado por membro, não por processo.** Um membro que falhou dentro de um
  processo vivo não pode contar como vivo, senão os sinais dele nunca seriam tratados como órfãos.
  Cada processo de mercado publica no Redis a fase e as sessões que ainda carrega
  (`signals:market:<processo>`, válida por 180 s), a cada leitura do stream e a cada barra; uma
  sessão que some dessa lista sem ter sido pedida falhou. *Corrigido em 13/10, na implementação
  (#446): a primeira versão deste ADR dizia que o supervisor leria as linhas de `live_sessions`,
  mas uma sessão ainda aquecendo não tem linha, e "aquecendo" ficaria igual a "morreu antes de
  abrir".*
- `--max-sessions` continua contando **sessões** (setup × mercado), que é como ele pensa nos sinais.
  O padrão pode subir depois da medição em produção.
- Um log por processo de mercado, com o setup em cada linha, no lugar de um log por sessão.

## Por que o resultado é idêntico

Cada membro tem a sua estratégia compilada, a sua corretora, o seu gerenciador de risco e o seu
`iter_run`, e recebe **a mesma sequência de barras** que receberia sozinho, porque o `splice` é o
mesmo e a ordem das barras não muda. Nada é compartilhado entre membros além das barras, que são
objetos imutáveis. A leitura do mercado compartilhada do ADR-0029 (estrutura e regiões calculadas
uma vez) **não** entra aqui: setups diferentes leem o mercado com parâmetros diferentes, e juntar
isso é outra decisão.

## Evidência (protótipo, 13/10, sobre develop e73603e)

Medição feita com o código da sessão (`splice` + `iter_candles` + `iter_run`), aquecendo os setups
que estão no ar com o histórico inteiro do disco. Primeiro todos num processo só, cada barra lida
uma vez e entregue a cada um; depois cada setup sozinho, no seu próprio processo
(`scratchpad/live_mem/measure_group.py`, fora do repositório).

| Mercado | Setups | Juntos (memória reservada) | Separados (soma) | Economia |
|---|---|---|---|---|
| WIN M5 (100 mil barras) | Choch 15/5, Continuação 15/5, CHOCH - 5 MIN, CONTINUAÇÃO | **386 MB** | 1.420 MB (349–361 cada) | 73% |
| Usa500 M5 (662 mil barras) | Choch 15/5, Continuação 15/5 | **617 MB** | 1.239 MB (650 + 589) | 50% |

- **Trades e saldos idênticos**, setup a setup. No WIN: 14 trades e saldo 9.810,966; 2 trades e
  9.900,144; 437 trades e 6.884,758; 450 trades e 8.144,642. No Usa500: 106 trades e 8.465,938; 32
  trades e 9.740,0071.
- **Cada setup a mais no mesmo processo custa ~10–30 MB**, contra ~350–650 MB de um processo novo.
- **Tempo de aquecimento**: o processo aquece os membros um depois do outro. O WIN levou 9 s com os
  4 juntos, contra 3 s de cada um sozinho (em paralelo, em 4 núcleos); o Usa500 levou 39 s, contra
  22–23 s.
- **Projeção para os 14 sinais de hoje**: 5 processos e algo como **2 GB**, contra 5,2 GB. É uma
  projeção a partir das duas medições acima, não uma medição; a medição de produção é critério de
  aceite do PR do supervisor.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|---|---|---|
| **Como está** (um processo por setup × mercado) | Isolamento total; já funciona | Paga a base e a leitura do histórico uma vez por setup; não chega a centenas de ativos |
| Enxugar cada processo (importar menos, devolver mais memória) | Nada muda na arquitetura | Ganho incerto e limitado; não tira a leitura repetida do mesmo histórico |
| **Um processo por mercado** (esta decisão) | Base e leitura pagas uma vez por mercado; o resultado de cada setup não muda; um mercado não interfere em outro | Um erro do processo derruba os setups daquele mercado; aquecimento sequencial dentro do mercado |
| Um processo para tudo (todos os mercados) | Máxima economia | Uma falha derruba todos os sinais; o aquecimento de um mercado atrasa as barras ao vivo de todos os outros; um núcleo só |
| Threads, uma por sessão, num processo | Divide a base | O GIL faz os aquecimentos disputarem com as barras ao vivo; mesmos riscos de isolamento do processo único; depuração mais difícil |
| Reiniciar o grupo do mercado a cada mudança de setup | Sempre um processo por mercado | Cancela no canal os sinais armados dos outros setups e os reaquece por causa de uma mudança que não é deles |

## Trade-off aceito

- **Um erro do processo de mercado derruba todos os setups daquele mercado** até o supervisor
  religá-lo. A falha de um setup continua isolada; a do stream, do disco ou do banco não fica. Hoje,
  com um processo por setup, uma falha de stream já derrubaria os 4 do WIN do mesmo jeito; o que
  muda é só uma falha que fosse exclusiva de um processo.
- **O aquecimento dentro de um mercado é sequencial**: o processo abre depois de aquecer todos os
  membros (39 s no Usa500 com 2 setups, contra ~23 s em paralelo). Como as barras que chegam nesse
  meio-tempo esperam no stream, nada se perde: elas só chegam um pouco mais tarde no começo.
- **Um setup novo fica num processo à parte até ele apertar "Reiniciar" naquele mercado** (ou até o
  próximo começo do supervisor). O custo é memória, não correção. Escolhemos isso no lugar de
  reiniciar o mercado sozinho a cada mudança, que cancelaria sinais no canal sem ele escolher a hora.
- **O botão Reiniciar cancela os sinais armados daquele mercado** e passa os em trade para o
  acompanhamento pelo preço. É o mesmo efeito de hoje quando um processo é religado; a diferença é
  que a tela diz antes quantos sinais serão afetados, e ele decide.
- **Mais código no supervisor**: ele passa a raciocinar por mercado e a contar os vivos por membro.

## Consequências

- **Código**: `run_session` dividida nos seus passos, usados pela sessão sozinha e pelo processo
  de mercado; um módulo novo para o processo de mercado (`live/market.py`), com entrada própria; o
  supervisor monta `wanted` por mercado e conta os vivos por membro. **O motor não muda** (o
  alimentador de uma barra é escrito em `apps/api`, sem tocar `packages/engine`), então não há rodada
  do guardian. Sem migração: `live_sessions` continua com uma linha por sessão. A API ganha uma
  leitura por ativo → time frame → setups (com os sinais em andamento e as métricas por setup e
  ativo), a situação do supervisor e o pedido de reiniciar um mercado; a web ganha a tela nova.
- **Testes**:
  1. membros juntos produzem exatamente os mesmos trades e sinais que cada um sozinho, sobre as
     mesmas barras;
  2. um membro que lança exceção é fechado como `failed`, e os outros seguem;
  3. um membro com pedido de parada sai em até `block_ms`, sem esperar barra;
  4. um membro em trade herdado convive com os outros já ao vivo;
  5. o supervisor sobe juntos os membros que faltam de um mercado e sobe à parte o que chega depois;
  6. um membro morto dentro de um processo vivo conta como órfão;
  7. uma queda do Redis ou do Postgres custa uma rodada ao supervisor, não o processo;
  8. o pedido de reiniciar um mercado para os processos dele e sobe os membros juntos, sem tocar
     os outros mercados;
  9. a tela mostra ativo → time frame → setups, avisa o supervisor parado e diz quantos sinais o
     reinício vai afetar antes de confirmar.
- **Operação**: os logs passam a ser um por mercado (`data/logs/signals/market-<símbolo>-<tf>.log`).
  A medição de memória com os sinais de produção é critério de aceite do PR do supervisor.
- **PRs**, nesta ordem, cada um com a sua lição:
  1. **correção**: o supervisor sobrevive à queda do Redis e do Postgres (teste 7);
  2. os passos de `run_session` + o processo de mercado (testes 1–4);
  3. o supervisor por mercado, a situação publicada e o pedido de reiniciar, mais a leitura por
     ativo na API (testes 5, 6 e 8), com a medição em produção;
  4. a tela por ativo → time frame → setups, com a aba "por setup" mantida (teste 9).
- **Fora daqui**: a consolidação automática dos processos de um mercado; a leitura do mercado
  compartilhada entre setups (ADR-0029) no ao vivo.
