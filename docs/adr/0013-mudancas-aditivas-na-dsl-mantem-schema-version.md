# ADR-0013 — Mudanças aditivas na DSL mantêm o `schema_version`

- **Status**: aceito
- **Data**: 2026-07-17
- **Contexto do PR**: PR-201

## Contexto

A invariante do `AGENTS.md §5` diz: *"toda mudança no JSON de estratégia atualiza o JSON Schema e `schema_version`; estratégias salvas são imutáveis por versão."* Lida ao pé da letra, adicionar o indicador RSI (um novo membro da união discriminada `Indicator`) exigiria bumpar `schema_version` de `"1.0"` para `"1.1"`.

Mas a implementação atual do gate é de **igualdade exata**, não de intervalo:

- `packages/schema/models.py`: `schema_version: Literal["1.0"]`
- `packages/engine/strategy.py`: `if version != SUPPORTED_SCHEMA_VERSION: raise`

Bumpar para `"1.1"` faria o gate **rejeitar todas as estratégias `"1.0"` já persistidas** (a coluna gerada `strategies.schema_version` no Postgres guarda o valor do JSON). E como o schema é um **único arquivo monolítico** (`strategy.schema.json`), a versão não gateia *quais* indicadores são permitidos — o RSI passaria a ser aceito para documentos `"1.0"` de qualquer forma. O bump seria cerimônia sem semântica, ao custo de orfanar dados salvos.

Isto expõe uma tensão entre a *letra* do §5 e o desenho "gate exato + schema monolítico". Adicionar um bloco é justamente o que o **ADR-03** previu como não-quebrante.

## Decisão

Mudanças **aditivas e retrocompatíveis** na DSL — em especial novos membros da união discriminada `Indicator`/`Condition`/`Exit` — **permanecem no `schema_version` atual** (`"1.0"`). O `schema_version` só é bumpado numa mudança **quebrante**: remover ou renomear um campo, estreitar um domínio, ou mudar a semântica de um bloco existente. Quando isso acontecer, o gate passará de igualdade exata para conjunto suportado.

Isto **estende** a redação do §5: "toda mudança atualiza `schema_version`" passa a ler-se "toda mudança **quebrante** atualiza `schema_version`".

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **B — aditivo fica em `"1.0"`, bump só em quebra** (escolhida) | Não orfã estratégias salvas; coerente com schema monolítico; adicionar bloco = ADR-03; gate atual intocado | Contraria a letra do §5 → precisa deste ADR para reconciliar |
| A — bumpar para `"1.1"` + gate por conjunto `{"1.0","1.1"}` | Segue a letra do §5 | Estratégias `"1.0"` só sobrevivem se o gate virar conjunto; a versão não gateia indicadores de fato (schema monolítico) → bump sem semântica; mais superfície de mudança agora |

## Trade-off aceito

Sacrificamos a leitura literal do §5 em troca de estabilidade dos dados e de um modelo de versão que só muda quando o significado muda. O risco é semântico: alguém pode assumir que `"1.0"` fixa o conjunto de indicadores. Ele não fixa — `"1.0"` fixa o **contrato estrutural** (formas de campo, invariantes), e o conjunto de blocos cresce aditivamente dentro dele. Uma estratégia `"1.0"` salva antes do RSI continua válida; uma que use RSI também declara `"1.0"`.

## Consequências

- Adicionar RSI (e, no restante do PR-201, MACD) **não** bumpa `schema_version`; `Literal["1.0"]` e `SUPPORTED_SCHEMA_VERSION = "1.0"` ficam como estão.
- Quando chegar a primeira mudança quebrante, este ADR será revisitado: o gate de igualdade exata vira conjunto suportado (`version in SUPPORTED_SCHEMA_VERSIONS`), e a política de compatibilidade (quais versões antigas a engine ainda interpreta) será decidida então.
- O JSON Schema e os tipos TS **continuam** sendo regenerados a cada mudança (aditiva ou não) — o que o §5 acerta é que o artefato gerado nunca é editado à mão; o que este ADR ajusta é apenas *quando o número da versão anda*.
