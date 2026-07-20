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

## Consequências

- `OrderRequest` ganha um `limit_price: Money | None`. `None` mantém o comportamento atual (fill no próximo open) — a mudança é **aditiva**, nenhum setup existente muda.
- O `BacktestBroker` mantém as ordens limite em estado pendente e, a cada `on_bar`, preenche as que a barra cruzou (ao limite, ou ao open num gap além dele) e cancela as cujo motivo de vida expirou. A ordem carrega o `decided_at` da barra que a colocou, e a guarda `fill_bar > decided_at` é o que sustenta o anti-lookahead.
- A camada de entrada dos setups (CHoCH, continuação, grab, flip) coloca a ordem na borda da zona com o stop em ±10% do tamanho e o alvo na região oposta (ou num múltiplo de risco); a expiração da ordem é o `usable` da zona.
- Testes de ouro cobrem: fill ao nível quando o pavio cruza; fill ao open num gap de abertura além do limite; **nenhum** fill na barra que colocou a ordem (anti-lookahead); cancelamento quando a zona morre antes do toque.
- O `engine-guardian` revisa antes de qualquer setup consumir a ordem limite — é mudança de corretude no caminho de fill.
