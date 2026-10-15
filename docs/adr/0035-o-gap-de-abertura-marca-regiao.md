# ADR-0035 — O gap de abertura marca região (opção do setup)

- **Status**: aceito (15/10, por ele: "abertura como gap"; regra e forma escolhidas nas perguntas do
  mesmo dia). Condição revista no mesmo dia (#465), pela resposta dele sobre a abertura que volta
  até a vela: itens 1 e 5
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
ela ligada, depois de uma **pausa** no gráfico, uma barra de abertura que fica **inteira** além da
última barra antes da pausa (a mínima acima da máxima dela, ou a máxima abaixo da mínima) marca
essa última barra como região, nascida na barra de abertura.

Os detalhes, e de quem é cada um:

1. **A condição é a barra de abertura inteira**, não só a abertura (dele). O indicador não desenha
   *"se a mínima do candle fechar o gap ou não confirmar o rompimento pelo fechamento"*. A mínima
   acima da máxima da última barra diz as duas coisas, porque o fechamento nunca fica abaixo da
   mínima. É estrita como a regra das três velas: um pavio exatamente na borda fecha o gap. No #464
   a condição era só a abertura, porque foi o que a primeira resposta dele descrevia.
2. **Pausa** é ficar sem barras por mais de uma barra **e** por pelo menos 30 minutos
   (`SHORTEST_PAUSE`). O piso é meu: mercado ilíquido pula os minutos em que ninguém negociou, e um
   buraco de duas barras de M1 não é abertura de pregão. Toda pausa que ele lê é bem mais longa: a
   noite da B3, a hora dos CFDs, o fim de semana do FX.
3. **Só de M1 a H4** (dele). A partir do D1 cada barra já é um pregão inteiro, e o salto entre elas
   é a regra das três velas que lê. Vale também no HTF do setup, com a mesma opção.
4. **A idade mínima até o toque vale igual** (`min_bars_to_touch`, dele). A entrada de 15/10, com
   toque uma barra depois, continua cancelada com 7 barras (ou 3).
5. **A abertura que volta até a vela não marca nada** (dele, #465). Ela não é gap, então também não
   segura a trava, e a barra seguinte fica com a regra das três velas, exatamente como com a opção
   desligada. No #464 essa região nascia tocada e segurava a trava, e o guardian mostrou o efeito:
   ligar a opção engolia o gap de três velas da barra seguinte e tirava uma região que a opção
   desligada oferecia.
6. **Uma trava só para as duas regras.** Um empurrão que salta a pausa e continua abrindo gaps é uma
   região, não uma por regra; sem isso a barra seguinte marcaria a mesma vela de novo como gap de
   três velas. Se as duas regras valem na mesma barra, a das três velas ganha: a vela que ela marca
   é onde o empurrão começou.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| Regra fixa no motor, sempre ligada | como o indicador dele; menos código | muda o resultado de todo setup de estrutura em mercado com pausa (B3, ações, CFDs); a fila da varredura (1,55 mi de runs) roda no motor antigo até reconstruir os workers, e o ao vivo fica diferente do backtest até lá |
| **Opção no setup, desligada por padrão (escolhida)** | resultados antigos e a fila continuam valendo; ele liga nos setups que quiser; dá para varrer ligada contra desligada | schema, fábrica, chave da leitura compartilhada; uma varredura que varie a opção lê o mercado duas vezes |
| Condição só na abertura, região nascida tocada se o pavio volta (o #464) | lia a primeira resposta dele ao pé da letra | não é o que o indicador desenha (a segunda resposta dele); a região nascida tocada segurava a trava e tirava a região que a barra seguinte marcaria |
| **Condição na barra de abertura inteira (escolhida no #465)** | é o que o indicador dele faz; a abertura que volta não deixa rastro, e a barra seguinte lê como desligada | nenhum além do #464 |

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
- **Ligar a opção ainda pode tirar uma região, num caso só: a trava que atravessa a pausa** (item
  6). Se o pregão termina numa sequência de gaps e a abertura também é gap, o empurrão continua do
  outro lado da pausa, e o gap de três velas da barra seguinte não marca nada. Desligada, a abertura
  não é gap, a sequência acaba nela, e esse gap marca a última vela como uma segunda região. O
  teste `test_a_run_carried_across_the_pause_is_one_region_where_off_reads_two` fixa esse caso, e
  é onde o v102 ainda pode divergir. O outro caso, o da abertura que volta até a vela, deixou de
  existir no #465 (item 5).
- **Uma pausa que cabe dentro de uma barra não aparece.** No H4 de um CFD, a hora de pausa fica
  dentro da barra das 20:00, e o gráfico não mostra barra faltando.
- **O código do v102 não foi lido.** O item 5 veio dele (#465). O 6 é a leitura coerente das regras
  que já temos. Se o indicador divergir, ele ganha, e esta regra muda.
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
  exato, H4 sim e D1 não, pavio da abertura na borda exata e meio ponto antes), as duas regras na
  mesma barra, o empurrão que salta a pausa, a abertura que volta até a vela deixando a barra
  seguinte ler como desligada, a trava que atravessa a pausa, o espelho de baixa; propriedades "sem
  pausa nada muda" e "desligada nada muda"; lote igual a cada run sozinho em séries com pregões e
  saltos.
