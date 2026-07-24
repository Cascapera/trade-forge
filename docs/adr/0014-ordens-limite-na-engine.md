# ADR-0014 — Ordens limite na engine (fill ao cruzar o nível)

- **Status**: aceito
- **Data**: 2026-07-20
- **Contexto do PR**: PR-202

## Contexto

Até aqui a engine sabe preencher de um jeito só: uma decisão tomada no fechamento do candle N vira uma ordem que preenche no **open** do candle N+1 (`BacktestBroker._fill_entry_at_open`). Foi o suficiente para os setups de indicador — "IFR cruzou 30, compre" não tem preço de entrada preferido, o próximo open serve.

Os setups de estrutura (SMC) quebram essa premissa. Todos eles entram **no toque de uma região**: o preço se afasta de uma zona de oferta/demanda, volta, e a ordem é ativada exatamente quando ele encosta na borda. É assim que o método é operado na mesa — uma ordem pendente na zona, não um clique quando a barra fecha.

A diferença não é cosmética. Se o CHoCH deixa uma zona em 100–102 e o preço volta tocando 102 mas a barra seguinte abre em 104, o fill "no próximo open" mede um trade a 104 — quatro pontos pior, com um stop que foi dimensionado para uma entrada em 102. O backtest passa a medir uma operação que o trader nunca faria. Para um método cujo edge inteiro é *entrar barato na borda*, isso não é ruído: é medir a estratégia errada.

O `sdd.md` descreve a engine event-driven e o fill no próximo open, mas não prevê ordem pendente por nível. Adicionar isso **estende** o `sdd.md`, daí este ADR (`AGENTS.md §8`).

## Decisão

A engine ganha **ordens limite**: uma ordem submetida com um `limit_price` fica pendente e preenche no primeiro candle **posterior** cujo range cruza esse preço — ao próprio `limit_price`, não ao open. Se o candle **abre já além** do limite, preenche no **open** (o mercado nunca ofereceu o nível melhor).

A regra que isso fixa:

> **Uma ordem só preenche numa barra que abriu depois de ela existir.**

O preço de fill de uma limite é intrabar — é o único fill da engine que não é um open. É exatamente aí que o lookahead entra, e é exatamente aí que essa regra o barra: a ordem é colocada no fechamento de N e só é elegível a partir de N+1. A barra que preenche nunca é a barra que decidiu.

O ciclo de vida da ordem é **herdado da zona que a gerou**, não um prazo próprio: a ordem vive enquanto a zona é `usable` e é cancelada quando a zona deixa de ser (mitigada, ou fechada através) sem ter preenchido. Uma zona que envelhece para fora da janela leva sua ordem junto. Não há um segundo relógio a acertar.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Ordem limite, fill ao cruzar (escolhida)** | Mede o trade que o método realmente faz; preço de entrada é a borda da zona; serve todos os setups de estrutura de uma vez | Peça nova no broker; fill intrabar exige a guarda anti-lookahead explícita; o tratamento de gap é mais um caso a pinar |
| **A mercado no próximo open** (o que já existe) | Zero código novo; anti-lookahead trivial (já é sempre um open) | Preço de entrada descolado da zona; num gap de abertura mede uma operação fictícia — justamente o caso que o método evita |
| **Fill ao preço médio da barra que toca** | Sem viés otimista de pegar sempre a borda exata | Não corresponde a nenhuma ordem real; um preço que ninguém consegue colocar na mesa |

## Trade-off aceito

Aceitamos um caminho de fill mais complexo no broker — estado pendente por nível, cruzamento intrabar, e a guarda que impede a barra decisora de ser a barra que preenche — em troca de um backtest que mede a entrada que o trader de fato colocaria. O otimismo residual (a limite assume que você foi preenchido assim que o pavio tocou o nível, mesmo num pavio de um tick) é conhecido e fica documentado no fill; é o preço de simular ordem pendente sem dados de tick.

## A barra que preenche a limite: o que ela **não** pode fazer

Emenda registrada na revisão do `engine-guardian` (21/07/2026). Uma entrada limite é a primeira posição do sistema que **nasce no meio de uma barra**, num preço que a estratégia escolheu em vez de aceitar. Várias regras da engine assumiam, sem dizer, o contrário — que toda posição existia desde a abertura e entrava no preço de referência. Sem as correções abaixo o backtest fabrica lucro:

1. **Na barra da entrada, os níveis preenchem exatos — sem tratamento de gap.** A abertura é um preço *anterior* à posição, então ela não pode ser onde a posição saiu. Precificar a saída ali mede um trade de 1R como 2,5R (e 17R numa barra larga). O tratamento de gap contra a abertura continua valendo para toda posição que já existia na abertura — lá ele significa o que sempre significou.
2. **Na barra da entrada, o alvo exige *prova*; o stop não.** A máxima daquela barra pode ter impresso antes de a entrada existir — uma compra limite preenche na *descida* — então "a máxima alcançou o alvo" não prova nada. O **fechamento** prova: o preço caminhou do fill até o fechamento, logo um fechamento além do alvo o cruzou depois da entrada, necessariamente. O stop não precisa de prova: ele fica do lado oposto ao próprio movimento que preencheu a entrada. ⚠️ **Reter o alvo sem essa regra não é conservador — é errado nos dois sentidos:** adiar um alvo comprovadamente cruzado deixa a posição atravessar para a barra seguinte, onde ou ela é paga na abertura (um gap que a própria engine fabricou) ou o stop da barra seguinte transforma um vencedor de 2R numa perda cheia. O resíduo — máxima toca o alvo, fechamento fica aquém — é genuinamente desconhecido e a posição simplesmente segue para a barra seguinte.
3. **Nenhuma limite preenche numa barra em que outra posição foi segurada *além da abertura*.** A barra que estopou uma posição em 1.09000 também negociou 1.09500 na descida; deixar uma compra limite em 1.09500 preencher a partir desse range registra uma entrada num preço que só existiu enquanto **outra** posição estava aberta. Ambiguidade intrabar resolve contra o trade. **Uma posição que saiu *na abertura* não bloqueia nada** — ela morreu no primeiro tick e o resto da barra ficou comprovadamente livre. A distinção importa: bloquear esse caso apagaria em silêncio a reversão canônica do método (fecha o runner na abertura, a mesma barra volta na próxima zona) e ainda deixaria a ordem viva para preencher noutro nível barras depois.
4. **A ordem que o mercado atravessou junto com o próprio stop não preenche — é descartada.** Uma compra limite em 1.09500 com stop em 1.09300 numa barra que abre em 1.09200 nasceria já abaixo da própria saída. O trade vira um zero-a-zero cujo único conteúdo é o custo, e a zona que ela esperava ficou para trás do mercado. Descartar **não encerra a barra**: a ordem seguinte na fila continua elegível.

5. **O tamanho da posição é medido a partir do `limit_price`, não do fechamento que decidiu.** O `PercentRiskManager` divide o risco em dinheiro pela distância até o stop; com a ordem limite essa distância deixou de ser "do preço de referência até o stop" e passou a ser "da **borda da zona** até o stop". Medindo do fechamento — que fica longe da zona, porque a ordem só arma depois que o preço se afastou — a distância sai inflada e o lote sai **pela metade** em todo setup de estrutura. Era bug real, encontrado nesta revisão: o backtest mostraria o setup certo rendendo metade do que renderia.

Complementos da mesma revisão: uma limite do **lado errado** do mercado (compra acima, venda abaixo) é recusada no `Signal` — é erro de sinal, não ordem exótica, e sem isso ela vira uma ordem a mercado silenciosa; e um `client_id` **já preenchido não pode ser reusado**, senão uma estratégia que reemite o sinal da zona a cada barra cria uma segunda ordem invisível com o mesmo nome.

## Consequências

- `OrderRequest` ganha um `limit_price: Money | None`. `None` mantém o comportamento atual (fill no próximo open) — a mudança é **aditiva**, nenhum setup existente muda.
- O `BacktestBroker` mantém as ordens limite em estado pendente e, a cada `on_bar`, preenche as que a barra cruzou (ao limite, ou ao open num gap além dele) e cancela as cujo motivo de vida expirou. A ordem carrega o `decided_at` da barra que a colocou, e a guarda `fill_bar > decided_at` é o que sustenta o anti-lookahead.
- A camada de entrada dos setups (choch, continuação, grab, flip) coloca a ordem na borda **próxima** da zona, com o stop na borda **distante ± 10%** do tamanho e o alvo num **teto de X·R** (default 5R, editável — decisão de 21/07/2026, que substituiu "mira na região oposta"); a expiração da ordem é o `usable` da zona.
- Testes de ouro cobrem: fill ao nível quando o pavio cruza; fill ao open num gap de abertura além do limite; **nenhum** fill na barra que colocou a ordem (anti-lookahead); cancelamento quando a zona morre antes do toque.
- O `engine-guardian` revisa antes de qualquer setup consumir a ordem limite — é mudança de corretude no caminho de fill.
