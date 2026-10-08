# Sinais ao vivo no Discord e no Telegram

**Objetivo:** acompanhar, ao vivo, os setups que ele escolher nos ativos que ele escolher e, quando um
deles estiver para acionar uma entrada, **avisar os alunos** no Discord e no Telegram — com ativo,
setup, preço de entrada, stop, alvo e a imagem do gráfico no momento da decisão — para que eles olhem
o ativo e avaliem visualmente se a entrada é boa. **O sistema não manda ordem nenhuma.**

**Pedido dele:** 08/10/2026. **Ideia original:** 02/10 ("sinais no Telegram para os alunos").
**Depende de:** ADR de várias corretoras (seção 4) — forex, ações dos EUA e ações do Brasil vêm de
três terminais MT5 ligados ao mesmo tempo.

---

## 1. O que o sistema já tem (medido em 08/10 no código)

| Peça | Onde | Serve para os sinais? |
|---|---|---|
| **Candles ao vivo** — `tradeforge-collector live <ativo> <TFs>` lê o MT5 e publica cada barra fechada em `candles.{ativo}.{TF}` (Redis stream) | `apps/collector/.../live.py`, `publisher.py` | **Sim.** Mas hoje é iniciado à mão, um processo por ativo, e só com um terminal |
| **Sessão ao vivo** — o **mesmo motor** dos backtests rodando barra a barra sobre o stream (aquece no histórico, depois segue ao vivo) | `apps/api/.../live/session.py`, `splice.py`, `heartbeat.py` | **Sim.** É o coração: garante que o sinal é exatamente o que o backtest faria (AGENTS.md §5.3) |
| Modos `PAPER` e `LIVE` da sessão | `live_sessions.mode` | Falta um terceiro modo: **`SIGNAL`** (simula como paper, publica mensagem, nunca ordem) |
| **Retrato da entrada** — 50 barras antes da decisão + regiões, níveis e médias do setup | `EntrySnapshot` (motor), `trades.snapshot` | **Sim** — é a imagem que vai para os alunos |
| Desenho do retrato | `apps/web/src/components/TradeSnapshot.tsx` (SVG próprio) | Hoje só no navegador; **não existe geração de PNG no servidor** |
| Início de sessão pela tela | — | **Não existe** (só pela linha de comando) |
| Envio para Discord/Telegram, HTTP de saída | — | **Não existe** |
| Vários MT5 ao mesmo tempo | — | **Não existe** (um agente, um terminal) |

⚠️ O retrato que já sabemos fazer **não é TradingView**: é o nosso gráfico em SVG (`TradeSnapshot`),
o mesmo da tela de trades. A imagem dos sinais sai dele, com a mesma cara do app.

---

## 2. Como um sinal nasce — o "quase acionando"

Os setups de estrutura e o MM9 **armam uma ordem pendente** (stop ou limite) num preço, e ela só vira
entrada se o preço chegar lá nas barras seguintes. Esse é o momento natural do aviso:

| Evento | Quando | Mensagem |
|---|---|---|
| **ARMADO** | o setup posicionou a ordem pendente (fechamento da barra de decisão) | 🟡 "Setup armado" — entrada, stop, alvo, imagem |
| **PERTO** *(opcional)* | o preço chegou a menos de X ATR do gatilho | 🟠 "Perto de acionar" |
| **ACIONADO** | a ordem foi executada (o motor de paper diz que encheu) | 🟢 "Acionou" |
| **CANCELADO** | o setup retirou a ordem (a virada acabou, o preço invalidou) | ⚪ "Cancelado" |
| **ENCERRADO** *(opcional)* | stop, alvo ou saída do setup | 🔴/🟢 resultado em R — histórico transparente |

No Discord e no Telegram a mensagem do ARMADO **é editada** a cada mudança de estado, em vez de mandar
mensagens novas: o aluno vê um card só, que muda de 🟡 para 🟢 ou ⚪. (Webhook do Discord e
`editMessageCaption` do Telegram permitem isso.)

**Onde se pega o evento:** um *broker* que embrulha o de paper (`BacktestBroker`) e, a cada `submit`
(ordem armada), `cancel`, e a cada *fill* devolvido pela barra, publica um evento num stream Redis
`signals.events`. A ordem de entrada já carrega tudo: lado, preço de gatilho, stop, alvo, motivo e o
`EntrySnapshot`. ⚠️ No aquecimento a sessão re-arma ordens antigas (`session.py`, hand-over): essas
**não** podem virar mensagem.

---

## 3. O conteúdo do sinal

```
🟡 SETUP ARMADO — EURUSD · H1 · CHOCH (compra)
Entrada: 1.08450 (ordem stop)   Stop: 1.08210   Alvo: 1.08930 (2R)
Risco: 24 pips · Corretora: ActivTrades · Decidido às 14:00 (Brasília)
[imagem: 50 barras + região + stop/alvo]
⚠️ Material educacional — não é recomendação de investimento.
```

- **Ativo, tempo gráfico, setup** (nome do catálogo + parâmetros principais), **direção**.
- **Entrada** (e o tipo: stop ou limite), **stop**, **alvo**, **risco** em pontos/pips e em %.
- **Alvo quando o setup não tem alvo** (muitos rodam sem): cada item da lista de monitoramento define o
  alvo do aviso (ex.: 2R), escolhido por ele.
- **Horário** de Brasília e da corretora.
- **Imagem:** o retrato da entrada em PNG, desenhado igual ao `TradeSnapshot`.
- **Aviso legal fixo** (seção 8).

---

## 4. Pré-requisito: várias corretoras e vários MT5 ao mesmo tempo

Hoje um ativo é só um nome (`AAPL`, `GOLD`), e o sistema conhece uma corretora por vez. Em 08/10 o
#404 deixou o **catálogo** aceitar várias corretoras lado a lado, mas o resto ainda não:

1. **A corretora entra na identidade do ativo.** `GOLD` é o ouro na ActivTrades e a Barrick na
   Tradeview. O ativo passa a ser *(corretora, símbolo)*; a pasta dos candles, as varreduras, as telas e
   os sinais passam a separar os dois. Migração com os ativos atuais apontando para a ActivTrades (forex,
   metais, índices, cripto) e para a Tradeview (as 21 ações dos EUA).
2. **Um agente por terminal.** A biblioteca do MT5 fala com **um terminal por processo**, então cada
   corretora tem o seu agente (coleta + candles ao vivo), com o caminho do terminal, o relógio do
   servidor e uma **fila própria** (`collect.activtrades`, `collect.tradeview`, `collect.<br>`).
   Três terminais: **forex/metais/índices/cripto**, **ações EUA**, **ações Brasil** (corretora a definir,
   precisa ter MT5 da B3).
3. **Candles ao vivo sob demanda.** Hoje cada `collector live` é iniciado à mão. O agente de cada
   terminal passa a ligar/desligar a transmissão dos pares (ativo, TF) que a lista de monitoramento pede.

Isto vira um ADR próprio (**várias corretoras**) antes dos sinais, porque mexe no banco, no coletor, na
API e no executor.

---

## 5. A arquitetura proposta

```
 Tela "Sinais"  ──►  lista de monitoramento (setup × ativo × TF, alvo do aviso, canais, ligado/desligado)
                          │
 agente MT5 (×3) ─► candles.{corretora}.{ativo}.{TF}  (Redis streams)
                          │
 sessão SIGNAL (uma por item) — o motor de sempre, broker de paper embrulhado
                          │  ARMADO / ACIONADO / CANCELADO / ENCERRADO
                          ▼
                    signals.events  (Redis stream)
                          │
 notificador ─► gera o PNG do retrato ─► Discord (webhook) + Telegram (bot)
                          │
                    signal_messages (banco: o que foi enviado, ids das mensagens, estado)
```

- **Sessão SIGNAL:** um terceiro modo da sessão ao vivo. Simula como paper (para saber quando acionou e
  quando encerrou), **nunca** fala com o executor, e não passa pelas salvaguardas do modo real porque não
  tem conta. Uma por item monitorado, iniciada pela lista (hoje só existe a linha de comando).
- **Notificador:** um serviço pequeno que consome `signals.events`, desenha a imagem, envia, edita as
  mensagens quando o estado muda e grava tudo em `signal_messages`. Envio **idempotente** (o mesmo evento
  nunca sai duas vezes), com nova tentativa e limite de frequência (Discord e Telegram bloqueiam excesso).
- **Imagem (PNG):** três caminhos —

  | Alternativa | Prós | Contras |
  |---|---|---|
  | **Chromium sem tela (Playwright) desenhando o próprio `TradeSnapshot`** | imagem idêntica ao app; zero desenho novo | container com navegador (~400 MB) |
  | Portar o desenho para Python (SVG → PNG com `resvg`) | leve, sem navegador | dois desenhos para manter iguais |
  | `mplfinance` (matplotlib) | pronto | cara diferente do app |

  **Recomendação:** Playwright desenhando o `TradeSnapshot` — o aluno vê no Discord o mesmo gráfico que
  vê no app, e um desenho só para manter.
- **Segredos:** URL do webhook do Discord e token do bot do Telegram no `.env`, nunca no banco nem no
  repositório.

---

## 6. Validar antes de ligar para os alunos

1. **No histórico, com o replay** (#400): para cada item da lista, o replay já reproduz cada entrada que
   o setup propõe; a sessão SIGNAL, rodando sobre os mesmos candles, tem de emitir **exatamente** os
   mesmos ARMADOS/ACIONADOS. Teste automático.
2. **Ao vivo, num canal privado** (só ele) por alguns dias: conferir horário, preços, imagem e o
   intervalo entre o fechamento da barra e a mensagem.
3. Só então os canais dos alunos.

---

## 7. Fases (um PR por item, na ordem)

| # | PR | Entrega |
|---|---|---|
| 0 | **ADR — Sinais** | este desenho aprovado por ele (modo SIGNAL, eventos, notificador, imagem) |
| 1 | **ADR — Várias corretoras** | corretora na identidade do ativo; agente por terminal; fila por corretora |
| 2 | Várias corretoras: banco + coleta | migração (corretora no ativo e nos candles), agente com `--broker`/terminal/relógio próprios |
| 3 | Candles ao vivo sob demanda | o agente liga/desliga `live` pelos pares pedidos |
| 4 | Lista de monitoramento | tabela + API + tela (escolher a partir de um run do Best by market ou da varredura) |
| 5 | Sessão SIGNAL + eventos | modo novo, broker que publica, stream `signals.events`, teste contra o replay |
| 6 | Notificador (texto) | Discord + Telegram, edição do card, idempotência, canal de teste |
| 7 | Imagem | PNG do retrato pelo Playwright, anexada na mensagem |
| 8 | Tela de sinais | histórico, estado de cada sinal, resultado em R, % de acerto por setup |

Os PRs 0–3 servem também para coletar e varrer as ações do Brasil e dos EUA, independentemente dos sinais.

---

## 8. Riscos e cuidados

- ⚠️ **Regulatório.** No Brasil, recomendar compra/venda de valores mobiliários é atividade de analista
  credenciado (CVM). O texto fixo deve deixar claro que é **material educacional / estudo de setup**,
  sem recomendação. **Vale ele confirmar com um advogado** antes de abrir para os alunos, principalmente
  para ações brasileiras.
- **Relógio:** cada corretora tem o seu (ActivTrades +2 h, Tradeview +3 h); a mensagem mostra Brasília.
- **Terminal desconectado / mercado fechado:** a sessão já tem *heartbeat*; o notificador avisa **ele**
  (não os alunos) quando um feed para.
- **Excesso de mensagens:** limite por canal e por item; opção de mandar só os ARMADOS dos itens
  marcados como "alta prioridade".
- **Sinal ≠ resultado do backtest:** o aviso de ARMADO sai antes do acionamento; muitos serão
  CANCELADOS. Mostrar isso no card evita frustração.
- **Memória:** cada sessão ao vivo é um processo com o histórico em memória (~1–2 GB nos setups de
  estrutura em M15). Com muitos itens, rodar as sessões SIGNAL numa máquina própria (a VPS, por exemplo).

---

## 9. Decisões que são dele

1. **Quais eventos postar:** só ARMADO? ARMADO + ACIONADO + CANCELADO (editando o card)? Também o resultado?
2. **Alvo do aviso** para setups sem alvo: fixo (ex.: 2R) ou por item?
3. **Canais:** um canal por mercado (forex / ações EUA / ações BR) ou um só?
4. **Horários:** avisar 24 h ou só no horário das aulas/mercado?
5. **Corretora/conta para ações do Brasil** (precisa de MT5 com B3).
6. **Imagem:** o retrato igual ao app (recomendado) basta, ou quer outro estilo?
