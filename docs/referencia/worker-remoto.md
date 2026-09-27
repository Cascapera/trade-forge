# Workers em outra máquina (Xeon)

Os workers só precisam de três coisas: a **fila** (Redis), o **banco** (Postgres) e os **candles**
(`data/ohlcv`). Os dois primeiros ficam na máquina principal e são alcançados pela rede. Os candles
são copiados para o disco da outra máquina. Cada worker lê uma série **uma vez** para a memória
(`candle_cache`) e usa essa cópia em todos os runs seguintes; só lê de novo quando os arquivos mudam.

Na outra máquina roda **só** o serviço `worker` (`docker-compose.worker.yml`). API, banco, Redis,
web e coletor continuam só na principal.

## Na máquina principal (uma vez)

1. **IP na rede local:** `ipconfig` → "Endereço IPv4" do adaptador em uso (ex.: `192.168.0.10`).
2. **Rede como "Privada"** (Configurações → Rede → propriedades da conexão).
3. **Firewall — só rede privada**, num PowerShell **como administrador**:

   ```powershell
   New-NetFirewallRule -DisplayName "TradeForge Postgres" -Direction Inbound -Protocol TCP -LocalPort 5433 -Profile Private -Action Allow
   New-NetFirewallRule -DisplayName "TradeForge Redis"    -Direction Inbound -Protocol TCP -LocalPort 6379 -Profile Private -Action Allow
   ```

   ⚠️ O Redis **não tem senha**. Nunca libere essas portas no perfil Público nem no roteador.

4. **Compartilhar os candles só para leitura:** botão direito em `data\ohlcv` → Propriedades →
   Compartilhamento → Compartilhamento avançado → nome `ohlcv`, permissão **Leitura**.

## Na outra máquina

1. **Docker** (Docker Desktop com WSL2 no Windows, ou Docker Engine no Linux) e **git**.
2. **O código:**

   ```bash
   git clone https://github.com/Cascapera/trade-forge.git
   cd trade-forge
   git checkout develop
   ```

   ⚠️ O mesmo motor da principal. Cada run grava `ENGINE_VERSION`, e dois motores respondendo à
   mesma fila poriam duas respostas numa varredura só. Sempre que a principal for reconstruída,
   atualize aqui também (`git pull` e o passo 5 de novo).

3. **Os candles** (Windows; repita depois de cada coleta na principal):

   ```powershell
   robocopy \\192.168.0.10\ohlcv .\data\ohlcv /MIR /R:2 /W:5
   ```

4. **A configuração:** copie `worker.env.example` para `worker.env` e preencha `TRADEFORGE_HOST`
   (o IP da principal), a senha do Postgres (a mesma do `.env` da principal) e
   `TRADEFORGE_WORKERS` (um núcleo cada; deixe núcleos para a própria máquina).

5. **Subir:**

   ```bash
   docker compose -f docker-compose.worker.yml --env-file worker.env up -d --build
   ```

6. **Conferir:** `docker compose -f docker-compose.worker.yml --env-file worker.env logs -f`
   mostra os runs sendo pegos. Na principal, o número de runs `running` sobe na varredura.

**Parar:** `docker compose -f docker-compose.worker.yml --env-file worker.env down`. O run que
estiver no meio é perdido e volta a rodar quando alguém pegar de novo, como na principal.

## Cuidados

- ⚠️ **Lotes (ADR-0029): o Xeon tem de estar no mesmo código da principal antes de qualquer
  varredura.** Uma varredura manda os runs em lotes (`run_backtest_batch`). Um worker de código
  antigo que pegar um lote **não o devolve à fila**: o arq marca "function not found" e os runs do
  lote ficam `queued` para sempre. Atualize o Xeon junto com a principal; se não der, lance com
  `TRADEFORGE_BATCH=false` no `.env` da principal (tudo roda run a run, como antes).

- **Conexões no Postgres:** o limite padrão é 100. 12 workers da principal + a API + 20–30 remotos
  cabem sem mudar nada.
- **Coleta nova na principal:** repita o `robocopy`. Um worker percebe que os arquivos mudaram e lê
  de novo; sem a cópia, ele roda com os candles antigos.
- **Reconstruir a principal** com migração nova: pare os workers remotos antes e suba de novo
  depois do `git pull`, pelo mesmo motivo do motor.
