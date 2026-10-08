# ADR-0032 — Várias corretoras: um nome interno por ativo, a corretora e o ticker ao lado

- **Status**: aceito (08/10, por ele)
- **Data**: 2026-10-08
- **Contexto do PR**: pedido dele de 08/10 (ações dos EUA na Tradeview ao lado do forex na ActivTrades;
  ações do Brasil numa terceira corretora; sinais ao vivo dos três — `specs/sinais-discord-telegram.md`)

## Contexto

O sistema nasceu com **uma corretora por vez**: um terminal MT5 aberto, um agente coletor (ADR-0021)
preso a ele, uma fila `collect`, e o ativo identificado **só pelo nome** (`instruments.symbol`,
único). Trocar de corretora em 02/10 foi apagar tudo e coletar de novo.

Agora ele quer três ao mesmo tempo:

| Terminal | Mercados | Relógio |
|---|---|---|
| **ActivTrades** | forex, metais, índices, cripto | +2 h |
| **Tradeview** | ações e ETFs dos EUA (9.549) | +3 h |
| **corretora BR** (a definir) | ações da B3 | a medir |

O #404 já deixou o **catálogo** (`broker_symbols`) aceitar várias corretoras lado a lado, mas o resto
não sabe de onde um ativo vem, e dois problemas aparecem:

1. **O mesmo nome, coisas diferentes.** `GOLD` é o ouro na ActivTrades e a Barrick Gold na Tradeview.
   Hoje a segunda ficou de fora (#404).
2. **Um terminal por processo.** A biblioteca `MetaTrader5` conversa com **um** terminal por processo
   Python. Três terminais exigem três agentes, e cada coleta (ou transmissão ao vivo) tem de chegar ao
   agente da corretora **certa**.

O nome do ativo é a chave de quase tudo: a pasta dos candles (`data/ohlcv/symbol=X/...`), as varreduras
(`sweeps.symbols`), as telas, os streams ao vivo (`candles.{X}.{TF}`), 26 arquivos da API.

## Decisão

**Cada ativo continua com um nome interno único — o que todo o sistema já usa — e ganha a corretora de
onde vem e o ticker que ela usa.** Quando dois tickers colidem, o segundo recebe um nome interno
diferente na hora do cadastro (ex.: `GOLD` da Tradeview vira `BARRICK` ou `GOLD.TV`), e o ticker da
corretora continua `GOLD`. **Um agente por terminal**, cada um com a sua fila.

Concretamente:

1. **Tabela `brokers`**: `slug` (`activtrades`, `tradeview`, `br-…`), nome, servidor MT5, caminho do
   terminal, relógio do servidor, fila do agente (`collect.<slug>`).
2. **`instruments` ganha `broker_id` e `broker_symbol`** (o ticker na corretora). `symbol` segue único:
   é o nome interno. Migração: os 19 ativos atuais → ActivTrades (ticker = nome), as 21 ações →
   Tradeview (ticker = nome).
3. **Agente com `--broker <slug>`**: lê da tabela o terminal, o relógio e a fila; atende **só** a fila
   da sua corretora; traduz nome interno ↔ ticker em cada pergunta ao MT5. O `tradeforge.ps1` liga um
   agente por corretora configurada.
4. **A API enfileira na fila da corretora do ativo** (coleta, sonda de histórico, transmissão ao vivo),
   nunca mais na `collect` genérica.
5. **Sincronização do catálogo por corretora**: cada agente fotografa o seu terminal (já é por servidor
   desde o #404). Uma colisão de ticker deixa de ser "pulada": aparece na tela para ele escolher o nome
   interno ao cadastrar.
6. **Nada muda na pasta dos candles, nas varreduras, nas telas e nos streams**: todos seguem pelo nome
   interno.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Nome interno único + corretora e ticker ao lado** (escolhida) | pasta, varreduras, telas e streams intocados; migração pequena e só aditiva; colisão resolvida uma vez, no cadastro | o nome interno pode diferir do ticker (precisa aparecer nas telas e nos sinais) |
| Ativo identificado por *(corretora, símbolo)* em todo lugar | modelo "puro", sem apelidos | muda a pasta de 3,4 GB de candles, `sweeps.symbols`, a API inteira (26 arquivos), as telas e os streams; varreduras antigas deixam de casar com os ativos |
| Um banco (ou uma stack inteira) por corretora | isolamento total | três de tudo para manter; nada de comparar setups entre mercados; os sinais teriam de juntar três sistemas |
| Continuar com uma corretora por vez | nenhum código | forex + ações dos EUA + ações BR ao mesmo tempo é exatamente o pedido |

## Trade-off aceito

O nome que aparece no sistema **pode não ser o ticker da corretora** (no caso raro de colisão: hoje só
`GOLD`). Aceitamos esse apelido em troca de não mexer em nada que já funciona: os 3,4 GB de candles, as
varreduras rodando e já rodadas, as telas e o motor. Toda tela que mostra um ativo passa a mostrar também
a corretora, e o sinal ao vivo mostra o **ticker da corretora**, que é o que o aluno procura no app dele.

## Consequências

- **Banco:** migração aditiva (`brokers`; `instruments.broker_id` não nulo com as duas corretoras
  semeadas; `instruments.broker_symbol`). Nenhum dado apagado.
- **Coletor:** `--broker`; tradução nome ↔ ticker; fila por corretora; o relógio vem da tabela (deixa de
  ser uma variável de ambiente que se esquece de trocar — o erro que este ADR também evita).
- **API:** enfileirar pela corretora do ativo; o seletor de ativos mostra a corretora; cadastro de ativo
  com colisão pede o nome interno.
- **Operação:** um terminal MT5 e um agente por corretora, os três ligados ao mesmo tempo no Windows
  daqui. Coletar da ActivTrades e da Tradeview deixa de exigir trocar o terminal aberto.
- **Executor (depois, fora deste ADR):** uma conta por terminal, para o modo real por corretora.
- **Sinais (`specs/sinais-discord-telegram.md`):** a transmissão ao vivo sob demanda vai para o agente da
  corretora de cada item monitorado.
- **PRs:** (1) migração + modelo; (2) agente por corretora + filas; (3) API e telas (corretora visível,
  colisão no cadastro); (4) transmissão ao vivo sob demanda.
