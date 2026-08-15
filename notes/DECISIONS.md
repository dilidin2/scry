# Decisioni di progetto

## 1. Niente browser pesante → Camoufox solo come tier 2 (eccezione approvata)
**Decisione**: stack HTTP leggero + impersonazione TLS (curl_cffi) come tier
primario per entrambi i platform. Eccezione: **Camoufox** (Firefox
anti-detect self-contained, ~200MB, API Playwright) come fallback per
Instagram quando curl non produce dati.
**Ragione**: selenium/playwright = 500MB-1GB, CPU/GPU pesante, fingerprint
facilmente rilevabile. curl_cffi replica il TLS di Chrome: passa i check di
base ed è ~20MB. Per Instagram da IP residenziale la pagina curl NON
contiene i dati del post (XDT assente) e i cookies da soli non bastano:
serve un browser. Camoufox è il compromesso: self-contained (niente
system deps), anti-fingerprint per design, geoip alignment, uso
sporadico (1 avvio di ~30-60s solo quando serve).
**Trade-off**: finestra visibile sul display :1 durante il fallback
(--headless disponibile ma più rilevabile: headless = challenge wall).
**Fallback cookies**: se anche Camoufox fallisce, i cookies utente restano
la leva principale.

## 2. faster-whisper (small) per STT
**Decisione**: faster-whisper, modello `small` int8, auto-detect lingua.
**Ragione**: 15x realtime su 16 core CPU, qualità multilingua (IT/EN) ottima,
modelli piccoli (~460MB small), cache portable. `base` come fallback rapido.
**Alternative scartate**: whisper.cpp (più lento a parità di modello),
Parakeet (EN-only), Moonshine (qualità inferiore), PaddleSpeech (pesante).

## 3. RapidOCR (PP-OCRv6) per OCR
**Decisione**: RapidOCR v3.x su onnxruntime.
**Ragione**: SOTA per dimensione (~15MB di modelli), 99-100% su italiano,
0.2s/immagine, install senza torch/paddle.
**Alternative scartate**: Tesseract (IT mediocre), PaddleOCR nativo (deps),
EasyOCR/Surya (torch da 2GB).

## 4. Pipeline TikTok: pagina → API commenti → oEmbed
**Decisione**: tiering con fallback espliciti, sempre metadata minimi.
**Ragione**: nessun singolo endpoint è affidabile da ogni IP; la combinazione
dà robustezza. oEmbed è il floor garantito (title+author).
**Commenti**: API `/api/comment/list/` con msToken estratto dalla pagina
(funziona con impersonation, senza X-Bogus). Ordinati per like.

## 5. Pipeline Instagram: curl → Camoufox (XDT + popup commenti) → og meta
**Decisione**: Tier 1 curl_cffi (XDT o legacy `__additionalData` o og meta).
Tier 2 Camoufox: apre il reel, parse XDT (`code`, non `shortcode`), apre il
popup commenti ed estrae dal DOM (testo/username/like/tempo/immagini).
Download media: curl_cffi → request context del browser → gallery-dl.
**Ragione**: i dati XDT sono nel HTML solo per il browser (la pagina curl
non li contiene da IP residenziale, testato). I commenti sono solo nel
DOM del popup (mai nell'HTML iniziale, mai in una URL dedicata: l'URL non
cambia quando si apre il pannello). L'estrazione DOM via JS con ancore
strutturali (permalink `/c/<id>/`, span "Mi piace: N") è più affidabile
di screenshot+OCR (testo esatto, like numerici, zero OCR).
**Legacy**: il parser `__additionalData` resta come fallback per formati
vecchi (es. cache/pagine diverse).

## 6. Consenso sui commenti = euristica trasparente pre-LLM
**Decisione**: scoring lessicale (marker EN/IT di accordo/disaccordo) +
reliability basata sui like del commento (convalida della community).
**Ragione**: l'utente vuole "capire cosa pensano e quanto è affidabile"
PRIMA di passare a un LLM (llama.cpp). Un modello di sentiment sarebbe
overkill e opaco; l'euristica è leggibile, testabile, e il LLM fa poi
il giudizio contestuale. I like dei commenti sono il segnale più onesto:
la community "vota" l'opinione.
**Limite dichiarato nell'output**: "usale come indizio, non come verità".

## 7. CPU-only, niente GPU
**Decisione**: tutto onnxruntime/CTranslate2 con int8, niente torch CUDA.
**Ragione**: la GPU è di llama.cpp; i modelli che scegliamo pesano ~600MB
totali e girano a 15x realtime su CPU.

## 8. Output: markdown per LLM + JSON per machine
**Decisione**: ogni run produce `output/<ts>-<plat>-<id>.md` (formattato per
essere incollato a un LLM: sezioni chiare, stats, transcript, commenti top,
consenso) e `.json` (dati grezzi).
**Ragione**: il consumo primario è "fai vedere al LLM" → il markdown è il
prodotto. Il JSON consente riprocessazioni (es. consenso avanzato in seguito).

## 9. Pin curl_cffi==0.15.0
**Decisione**: pin esplicito.
**Ragione**: yt-dlp supporta solo 0.5.10/0.10-0.15; 0.16 rompe
`--impersonate` (verificato: "Impersonate target chrome is not available").
**Nota**: in 0.15.0 l'alias `"chrome"` = versione Chrome vecchia bloccata
da TikTok. Costante `IMPERSONATE` in common.py, default `"chrome136"`
(overridable via env).

## 10. Commenti = top-level visibili, non corpus completo
**Decisione**: TikTok → top N per like via API (paginazione completa
disponibile). Instagram → commenti top-level visibili nel popup dopo
scroll (15-30), con like e immagini allegate.
**Ragione**: per il goal (consenso/affidabilità pre-LLM) i commenti
più convalidati bastano e rappresentano il "mainstream" del thread.
Le reply annidate collassate su IG non sono nel DOM (servirebbero click
addizionali per ogni thread: costo/beneficio negativo per uso sporadico).
**Nota**: il bias verso i top-level viene dichiarato nell'output
(comments_source + nota nei limiti SKILL).
