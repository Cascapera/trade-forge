# Onde paramos — PR-223, as regiões

> Handoff para retomar. **Apagar este arquivo quando o PR fechar** — o `RETOMAR-estrutura-profit.md`
> do PR-222 virou documentação mentindo sobre trabalho pendente porque ficou no repo depois de
> pronto. A referência durável é `indicador-regioes-order-block.md`, ao lado.

## Estado

Branch `feat/pr-223-regioes-do-indicador`, pushada, working tree limpa.

| commit | o quê |
|---|---|
| `79e07c8` | produção: as quatro regras dele |
| `fa1f0b0` | `test_structure.py` verde (87) |
| `342d490` | mesmo conserto no impulso do `test_setups.py` |
| `0c000e7` | a espera removida + 6 dos 7 testes |

**1 teste vermelho.** `ruff`, `format` e `mypy` (130 arquivos) limpos.

## O que falta — o último vermelho

`test_an_invisible_fill_does_not_leave_a_phantom_armed_order` (`test_setups.py`).

O cenário precisa de **duas** regiões vivas ao mesmo tempo: a zona A `[80, 100]` (a barra 3 do
`_IMPULSE` cavada até 80) e a secundária B `[110, 117]`, para que o script nomeie B na barra 13
depois do fill invisível em A.

**Por que quebrou:** B nasce marcada na barra 9 e as barras da descida a matam antes da 13 —
`bar(10, …, low="116")` toca o topo 117 de B. Medido:

```
zones: [('80','100', primary=True,  mitigated=False),
        ('110','117', primary=False, mitigated=True)]   <- B já morta na barra 13
```

**O conserto** é redesenhar a descida para passar **abaixo** de B sem tocá-la — ou escolher outra
secundária cuja borda de entrada a descida não cruze. Medir com sonda antes de escrever, como
sempre: alimentar `_drive_from_bullish` e imprimir `s._blocks.zones` com o `mitigated` de cada uma.

## Depois disso, na ordem

1. **Equivalência região a região contra o Pine dele** — portar
   `docs/referencia/indicador-regioes-order-block.md` literalmente para Python (script de
   referência, não a engine), rodar os dois sobre os 3480 candles de AAPL H1 e comparar barra de
   marcação, bordas e instante de mitigação. É o análogo dos 88/88 da estrutura.
2. **Gate como o CI roda** — `uv run pytest` e `uv run mypy` sem caminhos.
3. **`engine-guardian` MODO: FULL.** Dois pontos para ele atacar de propósito:
   - a remoção da espera em `_entry_for` é por **alcançabilidade** (argumento + medição de zero
     acertos). Argumento de alcançabilidade já enganou antes neste repo.
   - `_traded` ficou redundante com a mitigação e foi **mantido** como defesa em profundidade.
     Vale conferir se a redundância é real ou se existe caminho em que só um dos dois pega.
4. **Aula** pelo subagente `professor`.
5. **PR contra `develop`**, e ler a **lista** de checks — o `gh pr checks --watch` já saiu com
   código 0 com job vermelho e com 8 checks pendentes.

## Decisões já tomadas, não reabrir

- As quatro regras são dele e estão medidas em `indicador-regioes-order-block.md`.
- **"ZONA MORTA NÃO SERVE DE FLIP"** — o setup de flip nunca existiu; a máquina que o servia foi
  removida com 16 testes.
- Ele **validou na tela** em 05/08: CHoCH e BOS certos, entrando só em primárias.
- A trava do Pine (gaps consecutivos = uma região) substituiu o agrupamento por corridas; `_runs`
  foi apagado.

## Armadilhas desta frente

- ⚠️ Ao apagar testes por regex, **não** usar `(?=^(?:@|def |# ---))` como fim do bloco: engole
  código de módulo entre funções. Varrer linha a linha parando na primeira em coluna 0.
- ⚠️ `_OB_IMPULSE` e `_IMPULSE` são **cópias separadas** do mesmo impulso, em arquivos diferentes.
  Conserto num não conserta o outro.
- ⚠️ Nos testes dirigidos (`_drive_from_bullish`) **não há broker**, então onde um backtest real
  preencheria a ordem, o teste vê a região mitigar e a ordem ser retirada. Não confundir com bug.
