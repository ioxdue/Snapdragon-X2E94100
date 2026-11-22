# Hexagon Local Coding Agent: Architettura, Integrazione e Validazione

Guida tecnica e architetturale per l'esecuzione di un coding agent locale completo su **Windows 11 ARM64** e **Qualcomm Snapdragon X2 Elite Extreme** (`X2E94100`), utilizzando **Kilo Code** come frontend/agent framework e il runtime **GGUF Hexagon (`llama-server`)** come motore di inferenza locale.

---

## 1. Architettura di Sistema

Il sistema realizza una pipeline di inferenza ed esecuzione agentica **100% offline e locale**:

```
+-----------------------------------------------------------------+
|                   Kilo Code CLI / VS Code                       |
|               (@kilocode/cli, nativo ARM64)                     |
+-----------------------------------------------------------------+
                                |
                                | API OpenAI-compatibile (/v1)
                                | Streaming SSE & Tool Calling
                                v
+-----------------------------------------------------------------+
|             Router llama-server Hexagon (Loopback)              |
|                   http://127.0.0.1:18803/v1                     |
|           (Gestione on-demand e LRU cache dei modelli)          |
+-----------------------------------------------------------------+
                                |
        +-----------------------+-----------------------+
        |                                               |
        v                                               v
+-------------------------------+               +---------------+
|         NPU Hexagon           |               |  GPU Adreno   |
|         HTP0 (v81)            |               |  OpenCL       |
|  (Dense <=6GB, Q4_0, K-Quants)|               | (Offload 25%) |
+-------------------------------+               +---------------+
        |                                               |
        +-----------------------+-----------------------+
                                |
                                v
+-----------------------------------------------------------------+
|                          CPU Oryon                              |
|         (Controllo di processo, fallback e I/O di rete)         |
+-----------------------------------------------------------------+
```

### Componenti Principali
1. **Frontend / Agent Framework:** Upstream **Kilo Code CLI** (`@kilocode/cli` v7.8.32, pacchetto nativo `@kilocode/cli-windows-arm64`), basato su Node.js v24. Integra il provider `@ai-sdk/openai-compatible`. **Nessun fork di Kilo Code è stato necessario.**
2. **Motore di Inferenza:** `llama-server.exe` compilato per Windows ARM64 (build `b12323`, commit `93cf736`), con backend GGML Hexagon (`ggml-hexagon.dll`, catalogo Authenticode `libggml-htp.cat` valido) e supporto OpenCL.
3. **Router Multi-Modello:** Binding fisso su `127.0.0.1:18803`, caricamento modelli on-demand da directory `.cache\geniex\models` e parametri di calcolo ricavati da `hexagon-models.ini`.
4. **Hardware Target:** Qualcomm Snapdragon X2 Elite Extreme (12 core Oryon, GPU Adreno, NPU Hexagon NPU 45 TOPS, 48 GB LPDDR5X unificata).

---

## 2. Configurazione Riproducibile (`kilo.json` e Agenti)

La configurazione del progetto è centralizzata in due punti del repository:

### `kilo.json` (Workspace Root)
- **Provider locale:** configurato con ID `hexagon`, endpoint `http://127.0.0.1:18803/v1` e pacchetto `@ai-sdk/openai-compatible`.
- **Modelli primari:**
  - `model`: `hexagon/Ornith-1.5-9B-Q4_0` (Coding agent principale con tool calling nativo verificato).
  - `small_model`: `hexagon/MiniCPM5-2B-Q4_K_M` (Modello leggero e veloce per compattazione e compiti rapidi).
- **Politica di Sicurezza:**
  - `read`: `allow` (esplorazione del codice consentita).
  - `edit`: `ask` (richiede approvazione prima di modificare file).
  - `write`: `ask` (richiede approvazione prima di creare nuovi file).
  - `bash`: `ask` (comandi di sola lettura come `git status`, `git diff`, `git log` pre-autorizzati).
  - `task` e `external_directory`: `ask` (nessuna evasione dal workspace o deleghe non supervisionate).

### Ruoli Agente (`.kilo/agents/`)
Nella directory `.kilo/agents/` sono configurati 5 ruoli specializzati:
1. **`code.md` (primary):** Agente per sviluppo e refactoring. Permessi di edit e bash su `ask`.
2. **`plan.md` (primary):** Agente di progettazione architetturale. Modifiche al codice sorgente **deny**, consentita solo la scrittura dei piani in `plans/*.md`.
3. **`debug.md` (primary):** Agente per diagnosi ed esecuzione test mirati.
4. **`explore.md` (subagent):** Subagente di ricognizione a sola lettura. `edit: deny`, `write: deny`, `bash: deny`.
5. **`reviewer.md` (subagent):** Subagente di revisione diff e security audit. `edit: deny`, `write: deny`, `bash: deny`.

---

## 3. Matrice di Compatibilità dei Modelli

I test eseguiti sul runtime Hexagon hanno identificato le reali capacità dei modelli disponibili nella cache:

| Modello | Dimensione | Backend Hardware | Prefill (t/s) | Decode (t/s) | Tool Calling Nativo | Stato & Raccomandazione |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Ornith-1.5-9B-Q4_0** | 5.11 GB | **HTP0 (100% NPU)** | **762 - 964** | **12.3** | **Funzionante** (`tool_calls`) | **RACCOMANDATO come modello principale**. Ottimo ragionamento e aderenza ai tool. |
| **MiniCPM5-2B-Q4_K_M** | 1.45 GB | **HTP0 (100% NPU)** | **>1000** | **28 - 32** | **Funzionante** (`tool_calls`) | **RACCOMANDATO come small_model**. Estremamente rapido, ideale per subtask. |
| **Qwen3.5-0.8B-Q4_0** | 0.47 GB | **HTP0 (100% NPU)** | **461** | **97 - 99** | Non affidabile | Ottimo per classificazione o chat sintetica; sconsigliato per tool calling complesso. |
| **qwen2.5-coder-14b-instruct-q4_0** | 7.93 GB | **HTP0 (75%) + GPU (25%)** | **215 - 339** | **11.4 - 11.6** | **Non funzionante** (emette testo `<function-call>`) | Supporta chat e spiegazione del codice; il template Jinja corrente non converte i tool in schema OpenAI strutturato. |
| **granite-4.2-8b-Q4_0** | 4.71 GB | **HTP0 (100% NPU)** | - | - | Non testato | Tempo di allocazione grafo HTP elevato al primo caricamento (>60s). |

> [!NOTE]
> **Tool Calling:** A differenza di quanto comunemente ipotizzato, modelli orientati al coding come Qwen 2.5 Coder 14B non emettono nativamente chiamate strutturate conformi ad `@ai-sdk/openai-compatible` con l'attuale template Jinja di `llama-server`. Al contrario, **Ornith-1.5-9B** e **MiniCPM5-2B** generano chiamate `finish_reason: "tool_calls"` impeccabili, consentendo all'agente Kilo di eseguire comandi bash, leggere ed editare file senza errori di parsing.

---

## 4. Script e Strumenti di Gestione

Il progetto include tre strumenti pronti all'uso:

### 1. `setup-kilo.ps1` (Verifica Prerequisiti e Sicurezza)
Controlla lo stato del sistema su 11 punti critici:
```powershell
powershell -ExecutionPolicy Bypass -File .\setup-kilo.ps1
```
Verifiche effettuate:
- Piattaforma Windows 11 ARM64.
- Presenza di Node.js e CLI `kilo`.
- Integrità runtime Hexagon e allineamento commit (`stack_manager.py`).
- Firma Authenticode DSP (`libggml-htp.cat`).
- Presenza dei modelli GGUF nella cache locale.
- Conformità offline di `kilo.json` (loopback obbligatorio, assenza di rotte cloud).
- Presenza e permessi dei ruoli in `.kilo/agents/`.

### 2. `launch-kilo.ps1` (Launcher Unificato)
Avvia e gestisce il ciclo di vita dell'agente:
```powershell
# Esegue un controllo rapido dello stato senza avviare
powershell -ExecutionPolicy Bypass -File .\launch-kilo.ps1 -CheckOnly

# Avvia una sessione interattiva con Kilo Code
powershell -ExecutionPolicy Bypass -File .\launch-kilo.ps1

# Avvia l'agente specificando un ruolo (es. debug o code)
powershell -ExecutionPolicy Bypass -File .\launch-kilo.ps1 -Agent code
```
Caratteristiche:
- Rileva automaticamente se il router su `127.0.0.1:18803` è già attivo. Se offline, lo avvia su loopback.
- Rigenera `kilo.json` per garantire sincronizzazione con i modelli effettivamente esposti da `/v1/models`.
- Inoltra l'esecuzione a `kilo.cmd`.

### 3. `kilo_config.py` (Modulo di Sincronizzazione)
Utility Python per interrogare e aggiornare la configurazione:
```powershell
python kilo_config.py --status      # Mostra stato configurazione e router
python kilo_config.py --check       # Valida vincoli di sicurezza offline
python kilo_config.py --generate    # Ricrea kilo.json dai preset hardware
```

---

## 5. Report di Validazione (Scenari E2E Eseguiti)

Tutti i 9 scenari previsti dalla specifica sono stati verificati in un repository di test isolato (`poc_test_repo`):

1. **Lettura e spiegazione del codice:** L'agente ha letto `test_calc.py` tramite il tool `Read` e ne ha prodotto un'analisi accurata in italiano.
2. **Modifica di più file:** L'agente ha creato `math_utils.py` con `multiply(a, b)` e modificato contemporaneamente `test_calc.py` aggiungendo l'import e la relativa asserzione.
3. **Esecuzione di un test approvato:** Esecuzione di `python test_calc.py`, verifica dell'exit code `$LASTEXITCODE == 0`.
4. **Diagnosi e correzione di un errore:** Inserito deliberatamente un bug (`return a - b`), l'agente ha eseguito il test, catturato l'errore `AssertionError`, modificato il codice ripristinando `a + b` e riverificato il superamento del test.
5. **Revisione di un diff senza modifiche:** Eseguito `git diff` tramite l'agente; prodotta una code review tecnica dettagliata (casi limite divisione per zero, eccezioni native vs custom, stile PEP 8) confermando esplicitamente l'assenza di modifiche ai file.
6. **Subagenti e perimetri di sicurezza:** Confermato che i subagenti `explore` e `reviewer` mantengono le direttive `edit: deny` e `bash: deny`.
7. **Streaming e Compattazione KV Cache:** Il riuso della KV Cache (LCP similarity) ha raggiunto valori tra **98.7% e 99.9%**, abbattendo i tempi di prompt processing per i turni successivi da ~20s a meno di 400ms.
8. **Gestione Errori e Casi Limite:**
   - *Modello inesistente:* Restituisce immediatamente `Error: Model not found: hexagon/...` con exit code 1, senza tentare fallback cloud.
   - *Server offline:* Diagnosi immediata ed exit controllato.
9. **Operatività Offline Garantita:**
   - Binding HTTP forzato su `127.0.0.1`.
   - Nessun token o credenziale esterna richiesta o memorizzata.
   - Disabilitati plugin remoti e Kilo Gateway.

---

## 6. Suite di Test Automatizzati

La suite di test del progetto include i test di regressione esistenti e i nuovi test di integrazione Kilo:
```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```
**Risultato:** **67/67 test superati con successo** (inclusi `test_kilo_integration.py`, `test_stack_manager.py`, `test_tui.py`, `test_tui_http.py`).

---

## 7. Inventario Componenti e Tracciabilità

- **Componenti Upstream Invariati:**
  - Kilo Code CLI (`@kilocode/cli` 7.8.32): nessuna modifica, zero fork.
  - Runtime Qualcomm GenieX / GGML Hexagon (`b12323`, commit `93cf736`): binari preservati e verificati con Authenticode.
- **Modifiche Locali Preservate:**
  - `launch-hexagon.ps1`: mantenuta la regola locale per i modelli K-quants >15GB su `HTP0`.
  - Log e file non tracciati del working tree (`*.txt`): rigorosamente intatti.
- **Nuovi File di Integrazione Introdotti:**
  - `kilo.json`
  - `kilo_config.py`
  - `launch-kilo.ps1`
  - `setup-kilo.ps1`
  - `.kilo/agents/*.md`
  - `tests/test_kilo_integration.py`
  - `docs/HEXAGON_LOCAL_CODING_AGENT.md`

---

## 8. Avvertenze Legali e Disclaimer

- Questo progetto è un'integrazione di ricerca indipendente e **non è un prodotto ufficiale o supportato da Qualcomm né da Kilo Code / Kilo-Org**.
- I binari del runtime Hexagon provengono dall'installazione locale di Qualcomm GenieX e restano soggetti alle rispettive licenze di Qualcomm Technologies, Inc.
- I modelli linguistici GGUF sono soggetti alle rispettive licenze d'uso (es. Qwen License, Apache 2.0).
