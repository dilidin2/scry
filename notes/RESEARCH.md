# Ricerche tecniche: scraping TikTok & Instagram senza browser

## Contesto
- Server: Debian 13, Ryzen 7 7800X3D (16C), 30GB RAM, **CPU-only** (GPU occupata da llama.cpp)
- **IP RESIDENZIALE** (EOLO S.p.A., Toscana IT) — non datacenter. I wall sono
  molto più deboli: pagina completa e download diretti funzionano.
- Vincolo iniziale: niente selenium/playwright. Eccezione approvata:
  **Camoufox** (Firefox anti-detect self-contained) come tier fallback per
  Instagram, uso sporadico.
- Display :1 (Wayland) disponibile → browser headed funziona sulla desktop.

## Risultati da IP residenziale (testati 2026-08-15)
| Metodo | TikTok | Instagram |
|---|---|---|
| curl_cffi impersonate=**chrome136** + cookies | ✅ pagina completa (JSON embedded) | ⚠️ 200 ma **senza dati post** (nessun XDT nel HTML) |
| curl_cffi impersonate="chrome" (alias vecchio) | ❌ bloccato (fingerprint vecchio) | n/t |
| Download diretto media (CDN) | ✅ con Referer www.tiktok.com | ✅ video_versions (URL firmati) |
| API commenti /api/comment/list/ | ✅ 100% (msToken da pagina) | n/d (popup client-side) |
| **Camoufox headful + cookies** | (non usato) | ✅ XDT completo + popup commenti DOM |
| Camoufox headless | n/t | ❌ challenge wall, nessun XDT |

### Nota impersonation (importante)
In curl_cffi 0.15.0 l'alias generico `"chrome"` mappa una versione Chrome
**vecchia** il cui JA3 è bloccato da TikTok. Usare `"chrome136"` (o
safari/firefox/edge/chrome131, tutti verificati OK).

### Nota URL TikTok
`https://www.tiktok.com/video/<id>` **senza @username → 404** (pagina
/404?fromUrl=...). Gli URL di share reali sono
`/​@user/video/<id>?is_from_webapp=1` — usare sempre quelli.

## Cosa sbloccano i cookies (validato 2026-08-15 da IP residenziale)
- TikTok: pagina completa → JSON embedded con tutto (stats, author, music,
  hashtags, media URL firmati CDN) → download diretto senza yt-dlp.
- Instagram: i cookie da soli NON bastano (la pagina curl non contiene XDT);
  bastano per il tier Camoufox (sessione valida → nessun challenge).

## Architetture documentate (riferimenti)

### TikTok
- **Firecrawl** (docs.firecrawl.dev): `scrape` con `stealth`/`fire-engine` gira
  un browser headless in cloud. Funziona ma è un servizio esterno + costi.
  La nostra alternativa: curl_cffi (TLS impersonation) che è gratis e locale.
- **__UNIVERSAL_DATA_FOR_REHYDRATION__**: JSON nel `<script>` della pagina.
  Percorso: `__DEFAULT_SCOPE__` → `webapp.video-detail` → `itemInfo` →
  `itemStruct` (desc, stats, author, music, textExtra, createTime).
  In versioni più vecchie: `itemInfo.item` (gestito come fallback).
- **Commenti**: `GET https://www.tiktok.com/api/comment/list/`
  Params: `aweme_id, cursor, count=35, current_region, aid=1988, item_id,
  device_platform=webapp, app_language, msToken`.
  - `msToken` è nel payload JS della pagina (regex `"msToken":"([^"]+)"`).
  - Senza X-Bogus/sign: con impersonation TLS + msToken la API risponde
    (status_code 0). `hasMore`+`cursor` per la paginazione.
  - Ordine di ritorno: rilevanza TikTok (≈ like). Riordiniamo noi per like.
- **Item detail API**: `GET /api/post/item/detail/?aweme_id=...&aid=1988&...`
  con msToken → `item_list[0]` con `video.play_addr.uri` (URL playback diretto
  su CDN akamai) → download senza yt-dlp. Da validare con cookies.
- **short-link** (vm.tiktok.com/xxx): redirect 302 verso /video/ID — risolvibile
  anche senza login (da datacenter a volte serve retry).

### Instagram
- **Formato XDT (corrente 2025/26)**: `window.__additionalData` **non c'è
  più**. I dati sono in tag `<script type="application/json"
  data-content-len="N" ...>{...}</script>` (39+ per pagina, dimensioni da
  qualche KB a ~54KB). Quello del post contiene
  `xdt_api__v1__clips__home__connection_v2` → `edges[].node.media`.
  - L'oggetto media usa il campo **`code`** (NON `shortcode`), ~63 chiavi:
    `caption.text`, `like_count`, `comment_count`, `view_count`,
    `video_versions[]` (type 101/102/103, URL firmati scontent),
    `image_versions2.candidates` (copertina), `carousel_media[]`,
    `user.username`, `taken_at`, `media_type` (1=image, 2=video),
    `location.name`, `link`.
  - Strategia: scansiona tutti i tag data-content-len ≥ 5KB, JSON-parse,
    cerca ricorsivamente `code == shortcode` + marker media, scegli l'oggetto
    con più chiavi (il più completo).
  - **I commenti NON sono nell'HTML iniziale**: paginati client-side, si
    aprono in un **popup** (clic sull'icona commenti; l'URL non cambia).
- **Commenti nel DOM del popup** (estrazione via `page.evaluate`, ancore
  strutturali stabili, non classi effimere):
  - Permalink: `a[href*="/p/<code>/c/<id>/"]` — uno per commento.
  - Righe: [username `a[href="/nome/"]` + `time[datetime]`] / [testo, sibling
    successivo] / [riga azioni: "Rispondi" + **span `Mi piace: N`** + heart
    `svg[aria-label="Mi piace"]`]. Il conteggio like vive nello span
    "Mi piace: N" (assente se 0 like) e sta **un livello sopra** la riga
    username+time (walk-up fino al contenitore con heart/"Mi piace:").
  - Immagini/sticker allegati: `img[src*="cdninstagram"]` nel contenitore
    commento (escluse le s150x150 di profilo).
  - `is_reply`: heuristica sulla profondità DOM (reply caricati = +2 livelli);
    le reply collassate ("N risposte") non sono nel DOM.
  - Scroll: via JS sul contenitore scrollabile del popup (dal primo
    permalink, walk-up a `scrollHeight > clientHeight`). Lo scroll con
    `mouse.wheel` finisce sul feed dietro il popup se il mouse non è
    sopra l'elenco.
  - Timing: il popup impiega ~2-10s a popolare i commenti → polling,
    non wait fisso.
- **window.__additionalData** (formato legacy, pre-2025): JSON in
  `<script>` con `shortcode_media` / `xdt_api_graphql...` — il parser
  `parse_additional_data()` resta come fallback.
- **Meta og**: og:description = caption; og:image; `"video_url":"..."` nel JS.
  Fallback se __additionalData assente.
- **/embed/captioned/**: endpoint embed con caption+media, più permissivo.
- **Media URL**: diretti su CDN scontent (`.akamaihd.net`), scaricabili con
  curl_cffi. Carousel = lista in `carousel_media`.
- **gallery-dl**: fallback di download con logica anti-ban integrata;
  supporta cookies Netscape.

## STT (CPU)
- **faster-whisper** (CTranslate2): `small` int8 = ~15x realtime su 16C,
  `base` ~35x. Qualità: small > base (specie IT).
- Modelli: HF `Systran/faster-whisper-{tiny,base,small,medium,large-v3}`,
  cache portable in `models/whisper/` (download_root).
- VAD silero filtra i silenzi: audio vuoto → testo vuoto (no crash).
- Confronto: whisper.cpp (inferiore), PaddleSpeech (più pesante),
  Parakeet-CT2 (inglese-only), Moonshine (leggero ma qualità <).
  Vinta: faster-whisper small — miglior rapporto qualità/velocità CPU multilingua.

## OCR (CPU)
- **RapidOCR** v3.x: wrapper unificato (det+cls+rec) su onnxruntime,
  modelli PP-OCRv6 (PaddleOCR v6): det 4.1MB + rec 10MB + cls 1MB.
- Benchmark qui: init 0.2s, ~0.2s/immagine, **confidenza 99-100% su italiano**
  (testato chiaro e su sfondo scuro).
- Confronto: Tesseract (inferiore su IT e layout), PaddleOCR nativo
  (+600MB deps), EasyOCR (+2GB torch), Surya/Docling (overkill, torch).
  Vinta: RapidOCR — SOTA per dimensione, install leggera, multilingua.

## Camoufox (tier browser, uso sporadico)
- `camoufox[geoip]>=0.5`: Firefox modificato anti-fingerprint, self-contained
  (~200MB in `~/.cache/camoufox/`), API Playwright-compatible
  (`from camoufox.sync_api import Camoufox`), uBlock Origin incluso.
- `geoip=True`: allinea timezone/locale/fuso all'IP (necessario: IP IT →
  it-IT/Europe/Rome, altrimenti incoerenze geolocali aiutano il detect).
- **Headful > headless**: in headless IG mostra challenge wall (nessun XDT);
  headful (finestra sul display :1) funziona perfettamente con cookies.
- Flow che funziona (testato): warmup `instagram.com/` (6s, genera
  ig_shield) → goto reel (8s) → XDT in pagina → click icona commenti
  (`[aria-label*="omment"]`) → popup → DOM.
- Download media via `ctx.request` (request context Playwright: stessi
  cookie/TLS del browser) quando curl_cffi non basta.
- Cookies Netscape → conversione in formato Playwright (domain con `.`
  leading, path `/`).

## Anti-bot: perché curl_cffi
- I wall moderni fingerprintano TLS (JA3/JA4) e header ordering, non solo UA.
  `requests`/`httpx` = fingerprint Python evidente → bloccati (status 5 / 403 / shell).
- `curl_cffi` reimplementa curl con TLS di Chrome/Firefox/Safari identico al
  browser reale → supera i check di base. `impersonate="chrome"`.
- Limiti: non esegue JS → se la pagina richiede challenge JS (captcha),
  servono cookies o un altro layer. Da IP residenziale raramente serve.

## Versioni compatibili (importantissimo)
- yt-dlp richiede **curl_cffi 0.5.10 oppure 0.10.x–0.15.x** (non 0.16+).
  Pin: `curl_cffi==0.15.0`. (0.16 rompe l'impersonation di yt-dlp.)
- Python 3.13 ok per tutto lo stack.
- ffmpeg/ffprobe di sistema (Debian: `apt install ffmpeg`).

## Ricerche: "cookies + yt-dlp non funziona comunque" (2026-08-15)

Confermato da fonti multiple: è un problema **ben noto** e la causa è il blocco
per IP, non i cookies.

- **yt-dlp/yt-dlp#16605** "[TikTok] Your IP address is blocked from accessing
  this post": l'errore compare **sia con che senza cookies**
  (`--cookies-from-browser brave`). L'utente vede il video nel browser ma
  yt-dlp fallisce. → i cookies NON risolvono un blocco IP.
- **renderio.dev "Why yt-dlp Keeps Breaking: Geo-Blocks, PO Tokens, Cookies"**:
  - **Geo-blocking**: i range IP dei datacenter (AWS/GCP/Azure/Hetzner/DO) sono
    mappati pubblicamente; le piattaforme usano quelle liste. Richiesta da IP
    datacenter = bloccata o degradata *immediatamente*. I cookies non contano.
  - **Cookies**: Instagram/FB invalidano le sessioni di server "aggressivamente"
    — un server che fa richieste dirette senza cookie di sessione validi e
    *recentemente usati* viene flaggato in poche ore.
  - Pattern comune: "funziona in locale, rompe in produzione", manutenzione
    senza fine.

### Risultato dei nostri test da datacenter (con cookies validi)
- **TikTok HTML**: 537 byte "Site Maintenance" (hard block IP). I cookies non
  lo aprono.
- **TikTok item-detail API**: `status_code:0 status_msg:"url doesn't match"`
  → risposta di **risk-control** per richieste senza firma anti-bot (X-Bogus/
  _signature) o fingerprint device. Non è un problema di parametri: serve la
  firma (maintenance treadmill) o un proxy residenziale.
- **TikTok comment API**: funziona da datacenter (endpoint meno protetti).
- **yt-dlp TikTok**: 404 redirect (l'estattore ha bisogno della pagina, che è
  bloccata).
- **Instagram HTML**: 620KB ma è una **checkpoint/challenge wall**
  (contiene "checkpoint", "challenge", "Verify", "blocking"). Blocco IP-based;
  i cookies non lo aprono.

### Conclusione
Da **IP datacenter** il download media e i dati completi sono bloccati da
entrambe le piattaforme, **indipendentemente dai cookies**. È il comportamento
atteso e documentato. Da **IP residenziale** (es. server di casa) pagina,
yt-dlp e download funzionano normalmente — il tool è già fatto per quello.

### Opzioni per far funzionare il download DA datacenter
1. **Proxy residenziale** — la fix standard; costa + infrastruttura (la
   ricerca lo chiama "a project in itself").
2. **Server su IP residenziale** — niente da aggiungere, il tool funziona.
3. **Terzo servizio di download** (es. API tipo RenderIO/Apify, o il video
   download di Firecrawl) — delega il download a chi gestisce i geo-block;
   trade-off: costo + dipendenza esterna.

## Piano cookies
- Formato Netscape (7 campi tab-separati), export via extension browser
  (Cookie-Editor / Get cookies.txt LOCALLY).
- `tiktok_cookies.txt`, `instagram_cookies.txt` nella root del progetto.
- Caricamento: `session.cookies.set(name, value, domain=..., path=...)`.
- yt-dlp: `--cookies <file>`. gallery-dl: `--cookies <file>`.
- Privacy: i cookie = sessione; li si può revocare rigenerando.
