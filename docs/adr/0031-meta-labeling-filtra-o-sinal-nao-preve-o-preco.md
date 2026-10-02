# ADR-0031 — Meta-labeling: um modelo filtra o sinal do setup, não prevê o preço

- **Status**: aceito
- **Data**: 2026-10-02
- **Contexto do PR**: PR-372

## Contexto

O `sdd.md` (§1.2 e ADR-08) deixa "previsão de preço por ML" fora do escopo, por baixa
credibilidade: a IA entra via LLM, em análise e geração de estratégia. O plano do Guilherme
(23/09) pede ML para outra coisa: depois de varrer cada setup em muitos ativos, saber **em que
condições um sinal do setup funciona**, para pegar os bons e pular os ruins, e ler isso de forma
legível.

Essa técnica é o **meta-labeling** (López de Prado, *Advances in Financial Machine Learning*,
cap. 3). O setup continua decidindo onde e para que lado entrar. Um modelo só responde "pega" ou
"pula" um sinal que o setup já deu, com base no contexto da barra de decisão. Não é previsão de
preço. Mesmo assim, é um classificador treinado no resultado de trades, e é aí que backtests
mentem. Por isso as regras abaixo fazem parte da decisão, e não são detalhe de implementação.

O que o banco tem hoje não serve como está (medido em 02/10):

- **Viés de sobrevivência.** Um run de varredura só guarda os trades quando deu lucro
  (`retention`, 23/09). Os 329 mil trades do banco vêm de 11 mil runs vencedores. Um modelo treinado
  neles aprende com uma amostra já filtrada pelo resultado.
- **Contexto quase vazio.** O `trades.context` guarda só a zona e o valor das médias. As variáveis
  do modelo precisam ser calculadas dos candles.
- **Amostra pequena por ponto.** O melhor MM9 do GBPUSD H1 tem 149 trades em 6 anos. Só juntando
  ativos há amostra para treinar.

## Decisão

**Meta-labeling entra no escopo como camada de análise offline**: um modelo treinado para aceitar
ou recusar os sinais de **um setup com configuração fixa**, sob estas regras, que valem para
qualquer modelo daqui em diante.

1. **Sem lookahead (invariante 1).** Toda variável é calculada só com barras **fechadas até a
   barra de decisão**, a mesma que o motor usou. Um teste prova isso por construção: a variável
   não muda quando as barras posteriores são alteradas.
2. **Uma linha por entrada distinta.** A chave é (setup e configuração de entrada, ativo, gráfico,
   hora de entrada, lado). Pontos que diferem só na saída abrem as mesmas entradas (PR-370/371), e
   contá-los várias vezes infla a amostra.
3. **Sem viés de sobrevivência.** O dataset só usa runs que guardaram **todos** os trades, ganhando
   ou perdendo, e diz de onde cada linha veio.
4. **O rótulo é o resultado em R líquido do trade** (com custos e swap). O rótulo binário (R > 0)
   e o contínuo vão juntos.
5. **Validação no tempo e em ativos que o modelo não viu, nunca embaralhada.** Treina no passado e
   testa no futuro, com uma folga (embargo) entre os dois do tamanho do trade mais longo, para que
   um trade do treino não se sobreponha a um do teste. O último ano fica reservado, como nas
   varreduras. E um grupo de ativos fica fora do treino inteiro: um modelo que só acerta nos ativos
   em que treinou aprendeu sobre esses pares, não sobre o setup.
6. **A métrica é R médio por trade aceito, fora da amostra**, comparado com pegar todos os sinais,
   ao lado de quantos trades sobraram. Acurácia e AUC aparecem, mas não decidem.
7. **Reprodutível.** Semente fixa, e cada modelo grava a versão do motor, a versão do construtor de
   variáveis e um hash do dataset em que foi treinado.
8. **Fora do caminho ao vivo.** Nenhum modelo filtra ordem no backtest oficial nem no live sem um
   ADR próprio. Quando isso acontecer, será um filtro declarado na DSL e lido pela engine, passando
   pelo `engine-guardian` (invariantes 2, 3 e 5).
9. **O código de ML fica isolado** em `packages/ml`, com dependências próprias (scikit-learn,
   LightGBM, SHAP). Nada disso entra nas imagens da API nem dos workers.

O `sdd.md` passa a dizer: "Previsão de preço por ML" continua fora; meta-labeling sobre sinais de
setups entra, sob este ADR. O ADR-08 (LLM para análise) não muda: o LLM explica e consulta, nunca
escolhe trade nem calcula estatística.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| Manter ML fora do escopo | Nada a construir; sem risco de overfitting | Não responde à pergunta dele ("em que condições o setup funciona?") com mais de duas variáveis por vez |
| ML que prevê a direção do preço (sem setup) | Não depende de setup | É o não-objetivo do `sdd.md`, por bons motivos: sinal fraco, ruído alto, e joga fora o que o projeto construiu |
| Usar os trades que o banco já tem | Pronto hoje | Viés de sobrevivência: só runs vencedores guardaram trades |
| Validação cruzada embaralhada (k-fold comum) | Padrão das bibliotecas | Vaza o futuro para o treino em série temporal; o resultado sai otimista sem aviso |
| Modelo dentro do motor desde já | Backtest já filtrado | Mexe em determinismo e na DSL antes de saber se o modelo vale algo |

## Trade-off aceito

- **Mais trabalho antes do primeiro modelo.** As fases 1 e 2 (dataset de eventos e coleta sem viés)
  não produzem nenhum número de modelo. Sem elas, qualquer número seria otimista por construção.
- **Uma segunda leitura dos candles fora do motor.** As variáveis são calculadas em `packages/ml`,
  não pela engine. Isso não é uma segunda cópia da lógica da estratégia (§5.3): a entrada continua
  vindo do motor, e o ML só descreve o mercado em volta dela. Mas o cálculo precisa ser fiel à
  barra de decisão, e é por isso que a regra 1 tem teste próprio.
- **Amostra só com muitos ativos.** Um setup por ativo raramente tem trades suficientes. O modelo
  aprende sobre o setup em vários mercados, e a leitura por ativo vem depois, agrupando ativos
  parecidos.

## Consequências

**Os dados vêm antes do modelo** (decisão dele, 02/10). Rodar mais varreduras como estão não
produz dados para ML, porque elas só guardam os trades dos runs vencedores. O que precisa estar
guardado são os trades de **poucas configurações fixas, em muitos ativos, ganhando ou perdendo**.
As variáveis não precisam: são calculadas depois, dos candles e da hora de entrada. A ordem fica:

1. `sdd.md` §1.2 e ADR-08 atualizados neste PR.
2. **Mais ativos:** buscar e adicionar o ativo da corretora ao lançar um teste (pedido aberto desde
   24/09) e coletar em lote. Hoje há 15 instrumentos cadastrados e a corretora oferece 84; chegar a
   500–800 exige outra fonte, decidida depois.
3. **Rodar para ML:** um jeito de rodar configurações fixas em vários ativos guardando **todos** os
   trades (como já faz o teste fora da amostra), desenhado no PR dessa fase.
4. **A base consolidada:** as configurações do MM9 rodadas em todos os ativos.
5. **Dataset de eventos** em `packages/ml` (variáveis sem lookahead, uma linha por entrada
   distinta, rótulo em R), com testes de anti-lookahead.
6. **Modelos:** regressão logística, depois gradient boosting, com validação no tempo e por ativo;
   SHAP para a leitura. Cada fase com sua lição.
7. **Uma página no front** para o ML, com o conteúdo que ele vai definir quando chegar lá.

O primeiro setup é o **MM9**: é o mais rodado e o que mais entra. Um banco "para ML" é bem menor
que o banco de combinações do plano grande: poucas configurações em muitos ativos, não cada
variação em cada ativo.
