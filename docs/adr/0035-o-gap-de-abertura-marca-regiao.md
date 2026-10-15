# ADR-0035 — O gap de abertura marca região (opção do setup)

- **Status**: aceito (15/10, por ele: "abertura como gap"; regra e forma escolhidas nas perguntas do
  mesmo dia)
- **Data**: 2026-10-15
- **Contexto do PR**: a entrada de continuação do WDO M5 na abertura de 15/10, que o ao vivo não
  pegou
- **Relaciona-se com**: ADR-0014 (ordem limite), ADR-0029 (leitura compartilhada dos lotes),
  `docs/referencia/indicador-regioes-order-block.md` (a regra das três velas)

## Contexto

Em 15/10 ele viu, no WDO M5, uma entrada de continuação numa região secundária logo na abertura, e
o sinal ao vivo não veio. O ao vivo fez exatamente o que o backtest faz: o motor, rodado do começo
com as velas do disco e do stream, arma a mesma ordem (só na primária, sinal #45). A diferença
estava na região. O indicador do Profit dele (SMC_Estruturas_v102) desenhou uma caixa na vela das
18:25 de 14/10 (5.063–5.068), e o motor não marca região nenhuma ali:

| Vela (BRT) | Abertura | Máxima | Mínima | Fechamento |
|---|---|---|---|---|
| 14/10 18:20 | 5.061,5 | **5.068** | 5.060 | 5.067,5 |
| 14/10 18:25 | 5.067,5 | 5.068 | 5.063 | **5.066** |
| 15/10 09:00 | **5.080** | 5.083 | **5.072,5** | 5.075,5 |

A regra das três velas exige que a vela do meio feche além da máxima da primeira
(`close[1] > high[2]`). A das 18:25 fechou em 5.066, abaixo de 5.068, então não há gap. O buraco
entre 5.068 e 5.072,5 quem fez foi o **salto da abertura**. Perguntado, ele respondeu que o
indicador trata a abertura como gap: *"se ele abre com gap com relação à barra de fechamento do dia
anterior"*.

**O que a regra das três velas já cobre, e o que não cobre.** Quando a região sobrevive à vela
seguinte à abertura, as três velas (última antes da pausa, abertura, a seguinte) já formam um gap e
marcam a mesma vela, **uma barra depois**. O que não cobrem é o caso de 15/10: a abertura que **é** a
própria barra do BOS. Uma região nascida na barra seguinte já pertence à perna seguinte e nunca é
oferecida por aquele BOS. A regra nova marca a região na própria barra de abertura, dentro da perna.

## Decisão

Uma opção `opening_gap` (desligada por padrão) nos setups de estrutura (CHoCH e continuação). Com
ela ligada, depois de uma **pausa** no gráfico, uma barra que **abre** além da última barra antes da
pausa (acima da máxima, ou abaixo da mínima) marca essa última barra como região, nascida na barra
de abertura.

Os detalhes, e de quem é cada um:

1. **A condição é a abertura**, não a mínima: abrir além da barra de fechamento (dele).
2. **Pausa** é ficar sem barras por mais de uma barra **e** por pelo menos 30 minutos
   (`SHORTEST_PAUSE`). O piso é meu: mercado ilíquido pula os minutos em que ninguém negociou, e um
   buraco de duas barras de M1 não é abertura de pregão. Toda pausa que ele lê é bem mais longa: a
   noite da B3, a hora dos CFDs, o fim de semana do FX.
3. **Só de M1 a H4** (dele). A partir do D1 cada barra já é um pregão inteiro, e o salto entre elas
   é a regra das três velas que lê. Vale também no HTF do setup, com a mesma opção.
4. **A idade mínima até o toque vale igual** (`min_bars_to_touch`, dele). A entrada de 15/10, com
   toque uma barra depois, continua cancelada com 7 barras (ou 3).
5. **Tocada na própria barra de abertura, nasce tocada.** É consequência da regra do primeiro toque:
   a região existe a partir dessa barra, e o pavio dela que volta até a borda é o primeiro toque.
6. **Uma trava só para as duas regras.** Um empurrão que salta a pausa e continua abrindo gaps é uma
   região, não uma por regra; sem isso a barra seguinte marcaria a mesma vela de novo como gap de
   três velas. Se as duas regras valem na mesma barra, a das três velas ganha: a vela que ela marca
   é onde o empurrão começou.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| Regra fixa no motor, sempre ligada | como o indicador dele; menos código | muda o resultado de todo setup de estrutura em mercado com pausa (B3, ações, CFDs); a fila da varredura (1,55 mi de runs) roda no motor antigo até reconstruir os workers, e o ao vivo fica diferente do backtest até lá |
| **Opção no setup, desligada por padrão (escolhida)** | resultados antigos e a fila continuam valendo; ele liga nos setups que quiser; dá para varrer ligada contra desligada | schema, fábrica, chave da leitura compartilhada; uma varredura que varie a opção lê o mercado duas vezes |
| Condição na mínima (o buraco que sobra sem negociar) | mesmo espírito da regra das três velas | não é o que o indicador dele faz (ele escolheu a abertura); na prática a diferença some, porque a abertura que volta à borda deixa a região nascida tocada (item 5) |

## Trade-off aceito

- **Duas leituras do mercado convivem.** A opção entra na chave da leitura compartilhada (ADR-0029)
  e na chave do lote: um run com ela e um sem ela nunca dividem um lote.
- **O motor não muda de versão.** Desligada, a opção não muda nada, e há prova: propriedades
  (hypothesis) com e sem pausas, a suíte inteira intocada, e o caso real do WDO e do WIN. Por isso o
  commit diz `Engine-Results: unchanged`, como no #356. Ficam valendo os runs já feitos e o
  reaproveitamento deles.
- **Um worker antigo ignora a opção sem avisar.** O worker não valida o documento, e o motor antigo
  só lê os parâmetros que conhece. Um run com `opening_gap` executado num worker antigo sairia sem
  o gap e com a mesma versão, e o reaproveitamento poderia copiá-lo depois. **Não lançar varredura
  com a opção ligada antes de reconstruir os workers** (aqui, no Xeon e onde mais houver).
- **Ligar a opção também pode tirar uma região.** Se a abertura salta a última vela e o pavio
  volta até a borda, a região nasce tocada e segura a trava. Aí o gap de três velas da vela
  seguinte não marca nada. Desligada, esse gap marca a mesma vela uma barra depois, já depois do
  toque, e ela é oferecida. É a consequência dos itens 5 e 6, e o teste
  `test_switching_it_on_can_take_a_region_away` fixa esse caso. É também onde o v102 pode
  divergir.
- **Uma pausa que cabe dentro de uma barra não aparece.** No H4 de um CFD, a hora de pausa fica
  dentro da barra das 20:00, e o gráfico não mostra barra faltando.
- **O código do v102 não foi lido.** Os itens 5 e 6 são a leitura coerente das regras que já temos.
  Se o indicador divergir, ele ganha, e esta regra muda.
- **No D1/W1 a opção não faz nada, mas a varredura não sabe disso** (`unread_params` só olha o bloco
  do setup, sem o tempo gráfico). Uma varredura em D1 que varie a opção roda o par duas vezes.

## Consequências

- `StructureParams.opening_gap` (schema 1.0, aditivo — ADR-0013); JSON Schema e tipos TS regerados.
  A tela de construção mostra a caixa a partir do schema; a descrição diz "Gap de abertura".
- Motor: `OrderBlockDetector(timeframe=..., opening_gap=...)`, `MarketReading.opening_gap`,
  `RegionTracker(opening_gap=...)`, `StructureStrategy(opening_gap=...)`. A chave
  `setup_factory.ReadingKey` passa a ter três partes, e o `BatchKey` da API cinco.
- Deploy: `api` e `web` reconstruídas para salvar estratégias com a opção. O ao vivo pega o motor
  novo no reinício do mercado (fora do pregão, porque o reinício cancela sinais armados). Os workers
  só depois que a fila permitir, e **antes** de qualquer varredura com a opção.
- Testes de ouro: o caso real do WDO de 15/10, as bordas (pausa de 25/30 min, um tempo gráfico
  exato, H4 sim e D1 não, toque na própria abertura na borda exata), as duas regras na mesma barra,
  o empurrão que salta a pausa, o espelho de baixa; propriedades "sem pausa nada muda" e "desligada
  nada muda"; lote igual a cada run sozinho em séries com pregões e saltos.
