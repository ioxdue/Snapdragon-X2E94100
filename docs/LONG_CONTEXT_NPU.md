# Prova di contesto oltre 64k e recupero su NPU

`test-long-context-npu.ps1` e' un test hardware da eseguire su Windows ARM64 con
il runtime gia' preparato dal progetto. Richiede Windows PowerShell 5.1 o successivo;
non richiede Python, Git, SDK o nuovi pacchetti. Nessuna prova oltre 64k viene
considerata acquisita dalla sola presenza dello script.

## Esecuzione

Preparare normalmente il runtime con GenieX installato:

```powershell
.\sync-geniex.ps1 -Apply
.\test-long-context-npu.ps1 -Model 'C:\modelli\Qwen3.5-0.8B-Q4_0.gguf'
```

Specificare un file realmente presente, con architettura/quantizzazione supportate
da HTP e contesto nativo sufficientemente lungo. Il nome dell'esempio non garantisce
la compatibilita' di tutte le sue varianti. Iniziare con un modello piccolo riduce
il costo della prova. Un modello limitato a 32k/64k puo' rifiutare la richiesta:
lo script conserva l'errore e non forza estensioni RoPE.

Default: almeno **69.632 token di input**, contesto richiesto **98.304**, fino a
256 token di output, tre ripetizioni. Il tokenizer calibra l'input in un intervallo
da `PromptTokens` a `PromptTokens + 256`, senza tagliare arbitrariamente il testo.

```powershell
# Una prima prova; il report dichiara che la ripetizione e' una sola.
.\test-long-context-npu.ps1 -Model 'C:\modelli\modello.gguf' -Repetitions 1

# Prova piu' lunga, se il modello e la memoria lo consentono.
.\test-long-context-npu.ps1 -Model 'C:\modelli\modello.gguf' `
    -PromptTokens 100000 -ContextSize 131072 -Repetitions 3

# Esperimento distinto con split per layer, esplicitamente registrato come Hybrid.
.\test-long-context-npu.ps1 -Model 'C:\modelli\modello.gguf' `
    -Mode Hybrid -NpuShare 50
```

Il test avvia un proprio server su loopback, porta libera e chiave temporanea.
Non usa il router o la TUI e non aggiorna runtime/configurazioni. Arresta soltanto
il processo che ha creato, anche in caso di errore. Altri carichi CPU/GPU/NPU possono
influire sui tempi: per una prova pulita chiudere le altre inferenze e usare la rete
elettrica. I contatori sono filtrati sul PID del server, non sull'attivita' globale.

## Che cosa viene verificato

1. **Input effettivo.** Applica il chat template del modello, tokenizza e invia gli
   stessi ID interi a `/completion`. Controlla `tokens_evaluated`,
   `timings.prompt_n`, `timings.cache_n = 0`, `truncated = false`, capacita'
   effettiva da `/slots` e generazione completata. Sono disabilitati cache del prompt,
   riuso, cache RAM, ridimensionamento automatico e context shift. Nella build
   b11938/19cce6919, `tokens_cached` indica i token residenti e **non** quelli
   riutilizzati: non viene scambiato per `timings.cache_n`.
2. **Recupero.** Il corpus contiene record casuali chiave/codice, con distrattori
   nello stesso formato. Si interrogano tre record a circa 1%, 50%, 99% e una
   chiave assente. I codici da 16 cifre esadecimali non compaiono nella domanda o
   nello schema JSON. La corrispondenza e' esatta, inclusa la risposta `MISSING`.
   Ogni ripetizione usa un corpus diverso e salva offset token/distanze dalla fine.
   Un controllo breve usa gli stessi record e domande senza distrattori: se fallisce,
   il risultato non isola un problema di memoria lunga. Il controllo scalda anche
   il runtime; la richiesta lunga deve comunque rielaborare tutti i token.
3. **NPU.** DXCore identifica l'adapter mediante attributo hardware NPU, oppure
   descrizione NPU/Hexagon nella lista compute. Il LUID viene risolto a ogni
   esecuzione. Occorrono assegnazioni esplicite di layer `HTP*` nei log e incrementi
   dei contatori raw Windows `GPU Engine`, associati a quel LUID **e al PID del
   server**, durante la richiesta. Un engine `Compute` dell'Adreno, una DLL caricata
   o un generico messaggio `offloaded ... to GPU` non bastano.

Il campionamento usa `Win32_PerfRawData_GPUPerformanceCounters_GPUEngine`:
`UtilizationPercentage` raw e' il numeratore cumulativo del timer a 100 ns;
le differenze vengono confrontate con `Timestamp_Sys100NS`, dopo aver verificato
il qualificatore WMI `CounterType = 542180608` (`PERF_100NSEC_TIMER`). I valori raw sono
conservati. Campioni mancanti non vengono interpolati; reset/incrementi incoerenti
rendono l'attribuzione inconcludente. I confini prima/dopo il primo token sono
quelli osservati dal client e non delimitano perfettamente i kernel prefill/decode.

## Esiti e limiti

- **PASS, exit 0:** tutte le ripetizioni completano input oltre 65.536 token,
  recupero 3/3 piu' assenza, controllo breve e riscontro NPU.
- **FAIL, exit 2:** almeno una prova completata contraddice i controlli su
  elaborazione o recupero; consultare i singoli campi per distinguere la causa.
- **INCONCLUSIVE, exit 3:** mancano evidenze NPU, il driver non espone i contatori,
  lo stream si interrompe, il caricamento fallisce o la suite resta incompleta.
  Un errore DXCore non impedisce di raccogliere le prove di input e recupero.
  L'assenza di contatori attribuiti al PID non dimostra un fallback CPU: il driver
  potrebbe attribuire il lavoro a un servizio invece che al processo chiamante.

La NPU puo' essere usata insieme alla CPU anche in modalita' `Npu`: questi controlli
non quantificano la frazione degli operatori e non provano che l'intero prompt sia
calcolato esclusivamente sulla NPU. Uno split per layer non prova sovrapposizione
temporale dei kernel NPU/GPU.

Per i modelli SWA, lunghezza del prompt e finestra di attenzione dei layer restano
grandezze diverse. Il recupero casuale dimostra solo questa specifica capacita';
non dimostra qualita' generale, attenzione globale, superiorita' prestazionale o
priorita' rispetto ad altri progetti. Per confronti di velocita' misurare anche
senza campionatore, con lo stesso modello, parametri e condizioni.

## Artefatti e riproduzione

Ogni esecuzione crea una directory nuova in `long-context-results/` (ignorata da Git),
oppure quella indicata con `-OutputDir`. Conserva:

- `report.json` e `report.md`, con esiti distinti e riepilogo;
- corpus, chiavi attese, prompt renderizzati e richieste con gli ID esatti;
- stream SSE, risposte, conteggi e telemetria raw per ogni richiesta;
- log completi del server, argomenti e `/props`;
- SHA-256 di script, interop DXCore, modello (incluse le parti dei GGUF suddivisi)
  e file runtime;
- versione PowerShell, OS, CPU, RAM, batteria, driver e identita' DXCore della NPU.

Il seed rende il corpus riproducibile nello stesso ambiente .NET; i file salvati
sono il riferimento esatto anche fra versioni diverse. Condividere l'intera
directory dei risultati per permettere una revisione indipendente. I file della
macchina contengono percorsi locali e identificativi dei dispositivi.

Test software, senza hardware:

```powershell
.\tests\long-context.ps1
python -m unittest discover -s tests -p test_long_context_http.py -v
```

Il secondo comando e' facoltativo per lo sviluppo e richiede Python/PowerShell:
usa un server e un tokenizer fittizi e non produce evidenza di inferenza NPU.

Riferimenti: [API llama-server b11938](https://github.com/ggml-org/llama.cpp/blob/19cce6919/tools/server/README.md),
[contatori del server](https://github.com/ggml-org/llama.cpp/blob/19cce6919/tools/server/server-common.cpp),
[interfacce DXCore](https://github.com/microsoft/DirectX-Headers/blob/main/include/directx/dxcore_interface.h).
