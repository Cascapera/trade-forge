# ADR-0028 — Um estreitamento que não invalida documento salvo não bumpa o `schema_version`

- **Status**: aceito
- **Data**: 2026-09-10
- **Contexto do PR**: PR-234
- **Revisita**: ADR-0013

## Contexto

Ele decidiu que nenhum setup pode ser construído sobre uma média mais curta que **3 barras**, e o
PR-234 aplicou isso como `AVERAGE_FLOOR = 3` em sete campos do schema — o `period` dos quatro
`mme9_*` e do `ponto_continuo`, e o `long_average_period` dos dois que têm filtro de direção. O
domínio de cada um passou de `[1, 1000]` para `[3, 1000]`.

O **ADR-0013** nomeia exatamente isto. Ele diz que mudanças aditivas ficam no `schema_version`
atual, e que *"o `schema_version` só é bumpado numa mudança **quebrante**: remover ou renomear um
campo, **estreitar um domínio**, ou mudar a semântica de um bloco existente"*. E fecha prometendo
que *"quando chegar a primeira mudança quebrante, este ADR será revisitado"*.

Esta é a primeira. Pela letra do 0013, `schema_version` deveria ir para `"1.1"`.

⚠️ **E o bump seria pior que o problema.** O gate é de **igualdade exata**
(`schema_version: Literal["1.0"]`, `SUPPORTED_SCHEMA_VERSION = "1.0"`), então subir para `"1.1"`
faria a API **recusar as 267 estratégias já salvas** — todas `"1.0"` — para proteger **zero**
documentos. Nenhuma delas usa período abaixo de 3: conferido no banco antes de aplicar o piso, só
9, 20 e 200.

## Decisão

Um estreitamento de domínio **mantém** o `schema_version` quando **nenhum documento persistido cai
fora do domínio novo**, e essa condição é **medida** antes de aplicar, não presumida. Só um
estreitamento que de fato invalide documento salvo é quebrante, e é aí que o gate vira conjunto
suportado.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Medir e não bumpar** (escolhida) | O critério deixa de ser a forma da mudança e passa a ser o efeito dela, que é verificável; nenhum dado é orfanado | Exige uma consulta ao banco como passo obrigatório do PR; o banco de produção dele é o único juiz |
| Bumpar para `"1.1"`, pela letra do 0013 | Literal | Recusa 267 estratégias para proteger zero; cerimônia sem semântica, que é exatamente o que o 0013 rejeitou no caso aditivo |
| Bumpar **e** trocar o gate para conjunto suportado agora | Resolve a tensão de vez | Muda o contrato de validação inteiro para uma mudança que não exige nada disso; o 0013 já decidiu fazer essa troca só quando for necessária |
| Deixar o piso só na engine, sem tocar o schema | Zero risco de gramática | A gramática publicada continuaria oferecendo um período que o autor recusa, e o construtor continuaria desenhando o campo a partir de 1 — a UI deriva do schema |

## Trade-off aceito

**A verificação passa a ser parte do trabalho, e ela olha para um banco.** "Nenhum documento cai
fora" é uma afirmação sobre os dados dele num instante, não um teorema. Se amanhã existir uma
estratégia salva com média de 2, o mesmo estreitamento **será** quebrante e este ADR não o
autoriza — a medição é que decide, e ela precisa ser refeita a cada estreitamento.

Em troca, o projeto deixa de ter um gatilho de bump baseado na forma sintática da mudança, que
orfanaria dados por cerimônia.

## Consequências

- **Todo PR que estreitar um domínio da DSL roda a consulta antes**, e escreve o resultado na
  descrição. A do PR-234 foi: 267 estratégias, nenhuma com `period` ou `long_average_period`
  abaixo de 3.
- `schema_version` fica em `"1.0"` e o gate fica de igualdade exata. A promessa do ADR-0013 de
  trocar para conjunto suportado continua de pé, agora condicionada a um estreitamento que
  **invalide** algo.
- ⚠️ **O piso vive no schema, não na engine.** As classes continuam aceitando período a partir de
  1, porque lá o mecanismo é são — e um teste prende o comportamento no extremo degenerado
  (`test_the_two_readings_come_apart_at_a_period_of_one`). Gramática publicada e mecanismo são
  camadas diferentes, e esta decisão é sobre a primeira.
- O invariante dos sete campos é um teste por reflexão sobre a união `Setup`
  (`packages/schema/tests/test_average_floor.py`), não uma lista: um setup novo entra nele sozinho.
  Foi o que o guardian exigiu depois de mostrar que seis dos sete pisos não tinham prova nenhuma.
