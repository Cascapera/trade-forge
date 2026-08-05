# A fonte da verdade da estrutura: o indicador do Profit

`packages/engine/src/tradeforge_engine/structure.py::MarketStructure` é uma **transcrição** deste
indicador, que o Guilherme opera há anos no Profit Pro. Ele é a definição autoritativa de BOS e
CHoCH neste projeto — não uma leitura do que SMC "deveria" significar.

**Quando os dois divergirem, o indicador ganha.** Este arquivo existe para que a divergência seja
verificável em vez de discutível.

## Equivalência verificada

Em 04/08/2026, sobre **3480 candles reais de AAPL H1** (2024-08-01 → 2026-07-31):

| | eventos |
|---|---|
| indicador | 90 (36 CHoCH, 54 BOS) |
| `MarketStructure` | 88 (34 CHoCH, 54 BOS) |
| **idênticos** (mesma barra, tipo e nível) | **88 de 88** |
| só no indicador | 2 — as barras 1 e 2 |

As duas diferenças são a **única desobediência deliberada**, descrita abaixo.

## A desobediência deliberada: o zero do Pascal

No Profit, variável não inicializada vale `0`. Então `Topo_Choch` começa em zero, o primeiro
fechamento da série é "acima" dele, e o indicador marca `CHOCH` no nível **0,00** — e a barra
seguinte marca o espelho. Num gráfico são duas marcas inofensivas na origem.

**Numa engine que abre ordem, são dois trades num nível que não é um preço.** Por isso uma âncora
nunca plantada é `None` na transcrição, e não rompe nada até existir. Tudo o mais é literal.

## A regra que motivou a transcrição

A implementação anterior errava a âncora do CHoCH. Ela usava *a menor mínima desde a última máxima
nova*; a regra correta é **a menor mínima entre o armamento do BOS e a confirmação dele**
(`Minima_Fundo`, acumulada só enquanto `Topo_BOS` está armado).

Efeito medido num trade real (AAPL, 03/09/2024): a regra certa marca CHoCH de baixa em **223,89**
na barra 155; a implementação antiga marcava **223,04** na barra 159 — nível mais baixo e quatro
barras depois, o que deslocava a zona e a entrada.

## O código, como ele o enviou

```pascal
input
  Fundo_Escuro(True);
var
  Fundos                                                                      : array[0..20] of float;
  Topos                                                                       : array[0..20] of float;
  Fundo,Fundo_Sobe,Fundo_Desce,Fundo_BOS,Fundo_CHoch,Minima_Fundo,Maxima_Topo : float;
  Topo,Topo_Sobe,Topo_Desce,Topo_BOS,Topo_CHoch,cor_letra                     : float;
  DIR,cor                                                                     : integer;
  Direcao                                                                     : string;
begin
  //
  if (CurrentDate <= 120251231) then
    begin
      If (MaxBarsBack = 1) then
        begin
          Fundo_Desce := Minima;
          Topo_Desce := Maxima;
          Fundo_Sobe := Minima;
          Topo_Sobe := Maxima;
          DIR := - 1;
          If (Fundo_Escuro) then
            cor_letra := clWhite
          else
            cor_letra := clblack;
        end;
      // Gravando ultimo dado max
      If (Minima < Fundo_Desce) then
        Fundo_Desce := Minima;
      If (Maxima > Topo_Desce) then
        Topo_Desce := Maxima;
      If (Minima < Fundo_Sobe) then
        Fundo_Sobe := Minima;
      If (Maxima > Topo_Sobe) then
        Topo_Sobe := Maxima;
      If (DIR = - 1) then
        begin
          If (Maxima > Maxima[1]) e (Minima > Minima[1]) e (Maxima[1] > Maxima[2]) e (Minima[1] > Minima[2]) e (Fundo_BOS = 0) then
            begin
              Fundo_BOS := Fundo_Desce;
              Topo_Desce := Maxima;
              NoPlot(3);
            end;
          If (Maxima < Maxima[1]) e (Minima < Minima[1]) e (Maxima[1] < Maxima[2]) e (Minima[1] < Minima[2]) then
            begin
              Topo_BOS := Topo_Sobe;
              Fundo_Sobe := Minima;
            end;
          If (Fechamento < Fundo_BOS) then
            begin
              PlotText("BOS",cor_letra,3,6,Fundo_BOS);
              Fundo_Bos := 0;
              Topo_Choch := Maxima_Topo;
              Direcao := "BOS_Queda";
              cor := clRed;
              NoPlot(3);
            end;
          If (Fundo_BOS = 0) then
            Maxima_Topo := Maxima;
          If (Fundo_BOS <> 0) then
            begin
              If (Maxima > Maxima_Topo) then
                Maxima_Topo := Maxima;
            end;
          If (Fechamento > Topo_Choch) then
            begin
              PlotText("CHOCH",cor_letra,3,6,Topo_CHOCH);
              If (Fundo_BOS = 0) then
                Fundo_Choch := Fundo_Desce
              else
                Fundo_Choch := Fundo_BOS;
              Topo_Sobe := Maxima;
              Topo_BOS := 999999;
              DIR := 1;
              Direcao := "CHOCH_Alta";
              cor := rgb(140,160,140);
            end;
          NoPlot(5);
          Plot6(Topo_Choch);
          Plot3(Fundo_BOS);
        end;
      If (DIR = 1) then
        begin
          If (Maxima > Maxima[1]) e (Minima > Minima[1]) e (Maxima[1] > Maxima[2]) e (Minima[1] > Minima[2]) then
            begin
              Fundo_BOS := Fundo_Desce;
              Topo_Desce := Maxima;
            end;
          If (Maxima < Maxima[1]) e (Minima < Minima[1]) e (Maxima[1] < Maxima[2]) e (Minima[1] < Minima[2]) e (Topo_BOS = 999999) then
            begin
              Topo_BOS := Topo_Sobe;
              Fundo_Sobe := Minima;
              NoPlot(4);
            end;
          //
          If (Fechamento > Topo_BOS) then
            begin
              PlotText("BOS",cor_letra,3,6,Topo_BOS);
              Topo_Bos := 999999;
              Fundo_Choch := Minima_Fundo;
              Direcao := "BOS_Alta";
              NoPlot(4);
              cor := clGreen;
            end;
          If (Topo_BOS = 999999) then
            Minima_Fundo := Minima;
          If (Topo_BOS <> 999999) then
            begin
              If (Minima < Minima_Fundo) then
                Minima_Fundo := Minima;
            end;
          //
          If (Fechamento < Fundo_Choch) then
            begin
              PlotText("CHOCH",cor_letra,3,6,FUndo_CHOCH);
              If (Topo_BOS = 999999) then
                Topo_Choch := Topo_SObe
              else
                Topo_Choch := Topo_BOS;
              Fundo_Desce := Minima;
              Fundo_BOS := 0;
              DIR := - 1;
              Direcao := "CHOCH_Baixa";
              cor := rgb(160,140,140);
            end;
          NoPlot(3);
          NoPlot(6);
          Plot4(Topo_BOS);
          Plot5(Fundo_ChoCH);
        end;
      //
      PaintBar(cor);
    end;
end;
```

## Como reler a transcrição contra ele

Os nomes foram mantidos legíveis em inglês, mas o mapeamento é um para um:

| Pascal | `MarketStructure` |
|---|---|
| `DIR` | `_trend` (`-1` → bearish/None, `1` → bullish) |
| `Fundo_Desce` / `Topo_Desce` | `_low_down` / `_high_down` |
| `Fundo_Sobe` / `Topo_Sobe` | `_low_up` / `_high_up` |
| `Fundo_BOS` (0 = nenhum) | `_armed_low` (`None` = nenhum) |
| `Topo_BOS` (999999 = nenhum) | `_armed_high` (`None` = nenhum) |
| `Fundo_Choch` / `Topo_Choch` | `_choch_down` / `_choch_up` |
| `Minima_Fundo` / `Maxima_Topo` | `_lowest_since_armed` / `_highest_since_armed` |

⚠️ **A ordem importa.** No Pascal, `Minima_Fundo` é atualizado **depois** dos testes de armar e
confirmar. É isso que faz a janela da âncora abrir na barra *ao lado* do armamento em vez de na
barra do armamento. `_track_lowest` é chamado no mesmo lugar, e mover a chamada muda a regra.
