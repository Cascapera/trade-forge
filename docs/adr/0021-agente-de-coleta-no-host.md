# ADR-0021 — Um agente de coleta no host Windows, alcançado por fila

- **Status**: aceito
- **Data**: 2026-08-19
- **Contexto do PR**: PR-232

## Contexto

O pedido é: escolher **qualquer** ativo que o MT5 oferece, pela tela — digitar as primeiras letras,
ver as opções, saber quanto histórico existe, e coletar. Hoje a única porta é a CLI do collector, e
a tela oferece uma lista fixa: `GET /instruments` devolve as linhas da tabela `instruments`, que só
tem o que alguém já coletou à mão.

A restrição que decide a forma não é de estilo:

- `AGENTS.md §5.4` / **ADR-02**: nada fora de `apps/collector` e `apps/executor` importa a lib
  `MetaTrader5`.
- E mesmo sem a regra, é física: a wheel do `MetaTrader5` **só existe para Windows**, e a API e o
  worker rodam **em container Linux** (`docker-compose.yml`). O `uv sync --locked` do CI prova isso
  em toda push — a lib não é instalável lá.

Ou seja: **a API não pode falar com o MT5, nem hoje nem depois.** Alguma coisa tem de rodar no host
Windows, ao lado do terminal, e alguma coisa tem de ligar as duas.

### O que foi medido antes de decidir (19/08/2026, terminal do Guilherme, build 6090)

| Pergunta ao MT5 | Custo | Observação |
|---|---|---|
| `symbols_get()` (84 símbolos) | **0,5 ms** | |
| `symbols_get(group="A*")` | **0,1 ms** | casa por prefixo, e **enxerga símbolos ocultos** |
| busca binária por posição (H1) | **0,6 ms** | 34 chamadas |
| busca binária por posição (M15) | 537 ms | |
| busca binária por posição (**H4**) | **207.888 ms** | o terminal baixa o histórico durante a pergunta |
| `copy_rates_from(1970, …)` | — | **derruba o processo** (`OSError: Errno 22`) |
| `copy_rates_from(1990, …)` num símbolo oculto | 165.848 ms | e devolve `Terminal: Call failed` |

Duas leituras que mudam o desenho:

1. **A chamada ao MT5 é grátis; o caro é o transporte.** Filtrar 9550 símbolos por prefixo custa
   menos que um round-trip container→host. Fazer esse round-trip **por tecla** paga latência de
   fila para responder uma pergunta que o MT5 responde em 0,1 ms.
2. **"Qual a data mais antiga" não cabe num request síncrono.** 207 segundos no H4 não é um
   outlier a ser otimizado: é o terminal baixando histórico, e é o comportamento normal do primeiro
   pedido de um símbolo/timeframe frio.

## Decisão

**Um segundo worker `arq`, `tradeforge-collector agent`, rodando no host Windows ao lado do
terminal, consumindo uma fila própria (`collect`) do mesmo Redis.** A API só enfileira e lê o
resultado do Postgres; o host é o único processo que fala MT5.

E, decorrente da medição: **a busca de símbolo é servida do Postgres a partir de um snapshot** que o
agente publica, com uma ação explícita para reconsultar o MT5. Não há round-trip por tecla.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|---|---|---|
| **Worker `arq` no host, fila própria** ✅ | Reusa Redis + arq + o polling que a tela já faz com backtests; zero transporte novo; a API continua sem saber que MT5 existe | Ele precisa manter mais um processo no ar, além do terminal |
| Serviço HTTP no host, API faz proxy | Round-trip mais curto, bom para busca ao vivo | Transporte novo, porta nova, `host.docker.internal`, e um modo de falha novo (host no ar mas serviço fora) para uma resposta de 0,1 ms |
| Montar o MT5 no container | — | Impossível: a lib é Windows-only |
| Tirar API e worker do Docker | Tudo no host, sem fronteira | Joga fora a reprodutibilidade do compose e contraria o `sdd.md §9`; e o executor da fase 3 tem o mesmo problema, então a fronteira teria de voltar |
| Busca ao vivo por tecla | Sempre fresca | ~0,5–1,5 s por tecla contra 0,1 ms de MT5; e a busca **para de funcionar com o terminal fechado**, que é o estado normal de quem está montando estratégia |

## Trade-off aceito

**A lista de símbolos pode estar velha.** Um snapshot é uma foto, e o Guilherme troca de corretora
dentro do MT5 — a conta que tinha 9550 símbolos com AAPL hoje tem 84 de forex/CFD. Aceitamos isso
porque trocar de corretora é um ato deliberado e raro, e ele é modelado por uma ação explícita
("atualizar do MT5") em vez de por um custo pago em toda tecla de toda busca.

**Mais um processo para ele manter no ar.** É o mesmo preço que ele já paga com o terminal aberto, e
é o preço mínimo: alguma coisa tem de estar do lado do Windows.

## Consequências

- `apps/collector` ganha um subcomando `agent`, e passa a ter um `WorkerSettings` próprio — a
  fronteira do ADR-02 continua exatamente onde estava, porque quem importa MT5 continua sendo só o
  collector.
- A API ganha uma fila além da de backtest. **Duas filas, não uma**: um `sync_symbols` não pode
  ficar atrás de um walk-forward de vinte minutos, e um worker Linux nunca deve poder reivindicar
  um job que só o host sabe executar. A separação é a garantia disso, não uma otimização.
- Tabela nova para o catálogo do broker, **separada de `instruments`**: `instruments` é o que o
  sistema conhece e precifica (e tem FK de `datasets` e `backtests` com `RESTRICT`); o snapshot é
  o que a corretora oferece hoje. Misturar os dois faria uma troca de corretora apagar símbolos com
  histórico coletado.
- Nada disto toca o caminho *live* da fase 3, então não altera a ordem obrigatória dos PRs 302→304.
