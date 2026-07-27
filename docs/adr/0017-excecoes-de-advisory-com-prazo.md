# ADR-0017 — Exceções de advisory com prazo (o `npm audit` deixa de ser um sim/não)

- **Status**: aceito
- **Data**: 2026-07-27
- **Contexto do PR**: chore/ci-audit-allowlist

## Contexto

O job `Dependency audit` roda `npm audit --audit-level=high` e falha se qualquer pacote instalado tiver advisory high ou critical. Isso funcionou até 27/07/2026, quando o `develop` ficou vermelho **sem ninguém ter tocado em nada**: foi publicado o [GHSA-qwww-vcr4-c8h2](https://github.com/advisories/GHSA-qwww-vcr4-c8h2), CSRF no **RSC Mode** do `react-router` (CWE-352, "action execution before 400 response").

É a mesma lição do PR #35, agora com uma diferença que muda tudo: **daquela vez existia versão corrigida** (`fast-uri` 3.1.3 → 3.1.4, bump de lockfile e pronto). Desta vez não existe.

- Instalado: `react-router@7.18.1`, que já é a **última do ramo 7**.
- Faixa vulnerável: `>=7.12.0 <8.3.0`. Não há release corrigido no 7.x.
- A única saída que o npm oferece é **voltar** para `7.11.0` — sete minors atrás, e o npm rotula como semver-major.

Enquanto isso, o advisory **não alcança este repositório**, e isso foi verificado, não presumido:

| Verificação | Resultado |
|---|---|
| Modo de uso do router | Só declarativo: `BrowserRouter`, `Routes`, `Route`, `Link`, `NavLink`, `useNavigate`, `useParams`, `MemoryRouter`. Zero `createBrowserRouter`, zero data router, zero `loader`/`action`. |
| Pacotes `@react-router/*` no lockfile | **0**. O RSC Mode exige `@react-router/dev` + runtime de servidor. |
| Onde a "action" vulnerável rodaria | Em lugar nenhum: `apps/web` é SPA estática (`vite build`), sem servidor. |

O `npm audit` julga pela **versão instalada**; ele não sabe — e não pode saber — se o caminho vulnerável é alcançável. São duas perguntas distintas, e tratá-las como uma só é o que produz tanto o downgrade desnecessário quanto o `--audit-level` afrouxado.

Com a regra atual, **toda branch nasce vermelha** até 8.3 existir. Isso é pior do que parece: quando um check vive vermelho, ele para de ser sinal. A regra do repositório de nunca seguir sem CI verde perde o dente, e o próximo vermelho — o de verdade — chega no meio do ruído.

## Decisão

O gate de JS deixa de ser "nenhum high" e passa a ser **"nenhum high sem um argumento escrito e datado"**.

`scripts/check_js_audit.py` lê o `npm audit --json` e falha em **três** situações, não uma:

1. **Sem exceção** — advisory high/critical que ninguém analisou. O alarme continua armado para tudo que é novo.
2. **Exceção vencida** — entrada cuja `review_by` passou. A exceção tem prazo de validade e obriga a re-decidir; ela não expira em silêncio.
3. **Exceção órfã** — entrada que não casa com nada no relatório de hoje. Quando o upstream corrigir, o build manda apagar a linha.

As exceções vivem em `.github/audit-allowlist.json`, versionadas. Cada entrada exige `id`, `package`, `reason`, `decided_by` e `review_by` — campo faltando, campo em branco ou **chave desconhecida** derrubam o build, porque uma chave com typo se lê como "foi decidido" e se comporta como "não foi".

A exceção é por **par (advisory, pacote)**, nunca por pacote. "Já olhamos o react-router uma vez" não pode silenciar o próximo advisory do react-router.

### O detalhe de formato que decide a implementação

O `npm audit` conta **duas** vulnerabilities high aqui, mas existe **um** advisory. O `react-router` carrega o objeto do advisory; o `react-router-dom` aparece com `via: ["react-router"]` — uma *string*, porque ele é **efeito** do vulnerável, não vulnerável ele mesmo. Percorrer o mapa de topo inventaria um achado **sem ID nenhum**, que nenhuma allowlist poderia nomear e nenhum build poderia deixar verde. Por isso o parser percorre os objetos de advisory dentro de `via` e deduplica. Está travado por teste de ouro com o relatório real capturado deste repo.

### Falhar alto, nunca "limpo"

Um gate de segurança que não consegue ler a entrada **não pode reportar limpo**. Versão de relatório desconhecida, `vulnerabilities` ausente, entrada malformada ou JSON inválido → erro, com código de saída **2** ("não rodou"), distinto do **1** ("rodou e reprovou").

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| Downgrade para `react-router-dom@7.11.0` | zera o audit hoje; política de CI intocada | volta 7 minors por um risco **inexistente aqui**; risco de quebrar rotas e os 25 testes do web; preso no 7.11 até o 8.3; e não resolve o caso geral — o próximo advisory sem fix corrigido repete tudo |
| `--audit-level=critical` | uma palavra | cega o projeto para **todo** high futuro, inclusive os que alcançam. Troca um falso positivo por um falso negativo permanente |
| Deixar vermelho | zero trabalho | o check vira ruído; a regra de "não seguir sem CI verde" morre na prática |
| Pacote `audit-ci` | pronto, tem `--allowlist` | adiciona **dependência** para calar alerta de dependência; sem data de revisão; sem detecção de exceção órfã; a semântica da exceção passa a ser de terceiro |
| `jq` inline no workflow | nenhum arquivo novo | ilegível e **intestável** — e é justamente o mecanismo que silencia alarmes, o que mais precisa de teste |
| **Script Python + allowlist versionada** (escolhida) | testável no pytest que já roda; sob `mypy --strict`; validade e obsolescência; o argumento fica no `git blame` | um arquivo novo para manter |

## Trade-off aceito

**Passa a existir um jeito de o build ficar verde com um advisory high instalado.** Isso é, por construção, uma superfície de abuso: alguém com pressa pode escrever uma entrada ruim e seguir.

Aceitamos porque a alternativa honesta não existe — as outras três formas de lidar com um advisory sem correção (downgrade pior, `--audit-level` afrouxado, vermelho permanente) são todas **menos** auditáveis que uma linha versionada com autor, motivo e prazo. E as três regras limitam o abuso: a exceção precisa nomear o advisory exato, casar com o relatório de hoje, e morrer numa data.

O segundo custo é operacional: `review_by` vai reprovar o build um dia, provavelmente num momento inconveniente. **É o mecanismo funcionando**, não um defeito.

## Consequências

- `Dependency audit` volta a **verde** no `develop` e em toda branch nova. O `pip-audit` do lado Python fica **inalterado** (não tem exceções e não precisa).
- Novos arquivos: `scripts/check_js_audit.py`, `.github/audit-allowlist.json`, `tests/test_audit_allowlist.py` (48 testes, 100% linha e branch no script), `tests/conftest.py` (põe `scripts/` no `sys.path`).
- `pyproject.toml`: `scripts` entra em `mypy files`/`mypy_path` (o script fica sob `--strict`), ganha `per-file-ignores` de `T201` (a saída de um script de CI **é** a interface dele) e `check_js_audit` entra em `known-first-party`.
- **Quando o `react-router` 8.3 chegar** (provavelmente junto do trabalho de React 19, PRs #48/#49): o dependabot bumpa, o gate acusa a entrada órfã, e o fix é apagar as 7 linhas. Se o 8.3 não chegar até **27/10/2026**, a entrada vence e a decisão volta à mesa.
- **Se `apps/web` algum dia adotar data router ou runtime de servidor**, esta exceção deixa de valer — está escrito na própria `reason`.
