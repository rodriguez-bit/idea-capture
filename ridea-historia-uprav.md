# Ridea - Historia uprav

## O projekte
- **Nazov:** Ridea (Recording Idea)
- **Popis:** Interny nastroj na zachytavanie hlasovych a textovych napadov pre firmu
- **Web:** https://ridea.onrender.com
- **GitHub:** https://github.com/rodriguez-bit/idea-capture
- **Tech stack:** Python Flask, vanilla JS, SQLite/PostgreSQL, ElevenLabs Scribe (primary STT), OpenAI Whisper (fallback), Anthropic Claude
- **Platformy:** Web (PWA), Windows (Electron), Android (WebView APK)
- **Hosting:** Render.com (auto-deploy z main branch)

---

## v1.0 - Zakladna verzia
- Flask backend s REST API
- Nahravanie hlasovych napadov cez mikrofon (WebRTC / MediaRecorder)
- Prepis hlasu na text cez OpenAI Whisper
- AI sumarizacia a kategorizacia napadov cez Anthropic Claude
- Admin konzola na prehlad vsetkych napadov
- Prihlasovanie uzivatelov (admin, manazer, zamestnanec)
- Oddelenia: Vyvoj, Marketing, Produkcia, Manazment, Ine
- SQLite databaza (lokalne) / PostgreSQL (produkcia)

---

## v1.1 - Statistiky a opravy
- Oprava pocitania statistik v admin konzole
- Navrhy od kolegov na zlepsenie
- Pridanie textoveho vstupu (okrem hlasoveho nahravania)
- Odstranenie "majo-markech" funkcionality

---

## v1.2 - Fialovy rebrand + mobilna app
- **Fialovy rebrand** - cela aplikacia prerobena do fialovej farby (#512D6D)
- **Android APK** (v1.2.0) - WebView wrapper pre mobilne zariadenia
  - Subor: `Ridea-1.2.0.apk`
- Viditelnost napadov: "Moj napad" (osobne) vs "Pre celu firmu"

---

## v1.3.x - Electron Windows aplikacia
- **Electron desktop app** pre Windows
- Tray ikona (system tray) s kontextovym menu
- Login screen s moznostou zapamitat prihlasenie
- Cookies handling pre cross-origin (file:// -> ridea.onrender.com)
- `electron-packager` + Inno Setup installer (electron-builder NSIS ticho zlyhavalo)
- **v1.3.8** - Oprava ikony na ploche:
  - `icon.ico` generovany cez Pillow v CI (fialove "RI" logo)
  - Kopirovanie `icon.ico` do instalacneho adresara
  - `IconFilename` v Inno Setup pre desktop/start menu skratky
  - `ie4uinit.exe -show` na obnovenie Windows icon cache
- **Auto-update** cez `electron-updater`:
  - `checkForUpdates()` v main.js
  - Manualne generovanie `latest.yml` v GitHub Actions
  - Dialog na restart po stiahnuti aktualizacie
- GitHub Actions workflow (`build-windows.yml`):
  - Automaticky build pri zmene v `electron/` alebo workflow subore
  - Generovanie ICO ikony cez Pillow
  - electron-packager + Inno Setup
  - Upload EXE, ZIP a latest.yml do GitHub Releases

---

## v2.4.0 - Whisper anti-halucination + vizualizer + systemovy zvuk
- **Whisper anti-halucination:**
  - Rozdelenie dlhych nahravok na chunky (`_split_audio_chunks`)
  - Funkcia `_clean_hallucinations()` - detekcia a odstranenie opakovanych fraz
  - Riesenie problemu kde 7min nahravka generovala len "Dakujem za pozornost" opakovane
- **Audio vizualizer / equalizer:**
  - Canvas + Web Audio API (AnalyserNode, FFT)
  - Fialove stlpce reagujuce na frekvencny spektrum
  - Zobrazeny pocas nahravania
- **Systemovy zvuk (getDisplayMedia):**
  - Moznost nahravat zvuk z Google Meet, YouTube a pod.
  - Prepinac Mikrofon / Systemovy zvuk
  - Automaticke skrytie prepinaca ak prehliadac nepodporuje getDisplayMedia
- **Service Worker** cache aktualizovany na v2.4.0

---

## v2.4.1 - Audio backup + oprava deploy chyby
- **Problem:** Nahravky sa stratili - "poslal a nic neprislo do ridei"
- **Pricina:** `apt-get install ffmpeg` v render.yaml rozbil Render build
- **Riesenie:**
  - `_save_audio_backup()` - uklada surove audio do `audio_uploads/` PRED transkripciou
  - `audio_filename` ulozeny v databaze
  - Graceful error handling - napad sa ulozi aj ked transkripcia zlyha (prazdny prepis)
  - Revert render.yaml - odstranenie apt-get (nefunguje pre Python services na Render)
  - Endpoint `/api/ideas/<id>/audio` na stiahnutie povodnej nahravky
  - Individualny try/except pre kazdy Whisper chunk

---

## v1.4.0 / v2.5.0 - Oprava ikony + nahravanie mic+system naraz
- **Oprava ikony v Electron app (v1.4.0):**
  - `BrowserWindow` teraz ma nastavenu `icon` property
  - Tray pouziva skutocny `icon.ico` namiesto generovaneho kruhu
  - Funkcia `getIconPath()` hlada ikonu na viacerych miestach
  - `--no-asar` v electron-packager - subory priamo pristupne
  - Kopirovanie `icon.ico` do `resources/app/assets/` v zabalenej app
  - Fallback na generovanu ikonu ak `icon.ico` nie je najdeny
- **Nahravanie mic + system audio naraz (v2.5.0):**
  - Odstraneny prepinac "Mikrofon" / "Systemovy zvuk"
  - Pri kliknuti na nahravanie sa automaticky zapne mikrofon
  - Nasledne sa pokusi pridat aj systemovy zvuk (getDisplayMedia)
  - Ak uzivatel odmietne/nepodporuje, nahra len mikrofon
  - Oba streamy mixovane cez AudioContext (createMediaStreamDestination)
  - Vizualizer zobrazuje zmiksovany signal
  - Info text pod casom zobrazuje aktivne zdroje ("mikrofon + systemovy zvuk")
  - Zmena v oboch: web (`static/recorder.html`) aj electron (`electron/recorder.html`)

---

## v2.5.1 - Kompresia audia + ochrana pri uploade
- **Problem:** 27-minutova nahravka = ~30MB, upload padal na "connection already closed"
- **Kompresia nahravok:**
  - `audioBitsPerSecond: 32000` (32kbps) - znizi 27min nahravku z ~30MB na ~6MB
  - Opus codec automaticky optimalizuje pre rec pri nizkom bitrate
  - Kvalita prepisu Whisperom zostava zachovana (rec nevyzaduje vysoky bitrate)
- **Upload timeout ochrana:**
  - `AbortController` s 120s timeout na vsetky fetch upload volania
  - Predchadza nekonecnemu cakaniu pri vypadku spojenia
  - Vault system zachrani nahravku lokalne aj pri zlyhani
- **UX vylepsenia:**
  - Zobrazenie velkosti nahravky po zastaveni ("X.X MB")
  - Zobrazenie velkosti pocas uploadu ("Odosielam X.X MB...")
  - Vault retry zobrazuje velkost pri opatovnom posielani
- Zmena v oboch: web (`static/recorder.html`) aj electron (`electron/recorder.html`)

---

## v2.5.2 - AudioVault v2 - Neprerusitelna ochrana nahravok
- **Problem:** Ak sa stratilo spojenie pocas dlhej nahravky, alebo app spadla, nahravka bola stratena navzdy
- **AudioVault v2 - Live backup:**
  - `vaultStartLiveSave()` - uklada chunky do IndexedDB kazdych 10 sekund POCAS nahravania
  - Ak app spadne, zamkne sa obrazovka, alebo dojde k chybe, nahravka je ulozena lokalne
  - `vaultPromoteLive()` - ak sa nahravanie prerusilo, automaticky presunie live zaznam do stavu "caka na odoslanie"
- **Auto-sync engine:**
  - `vaultAutoSync()` - bezi automaticky kazdych 30 sekund na pozadi
  - Kontroluje vsetky cakajuce a zlyhane nahravky v IndexedDB
  - Automaticky sa pokusi odoslat ich na server
  - Spusti sa automaticky pri prihlaseni uzivatela (`vaultStartAutoSync()` v `init()`)
- **Exponencialny backoff pri chybach:**
  - Intervaly: 30s, 60s, 120s, 240s, max 300s (5 min)
  - Predchadza zahlteniu servera opakovanyni pokusmi
  - Pocitadlo retries sa uklada v IndexedDB
- **Online/offline detekcia:**
  - `window.addEventListener('online')` - pri obnoveni siete okamzite spusti sync
  - `window.addEventListener('offline')` - informuje uzivatela, nahravanie pokracuje lokalne
- **Promise-based async upload:**
  - `_doVaultUploadAsync()` a `_pollVaultJobAsync()` - upload na pozadi bez blokovania UI
  - Tichy rezim - auto-sync neukazuje UI spravy (nebrani uzivatelovi)
- **Scenare ktore su teraz pokryte:**
  - Vypadok internetu pocas nahravania - nahravka sa odosle po obnoveni
  - App pad/restart - nahravka prezije v IndexedDB, odosle sa po restarte
  - Zamknutie obrazovky na Androide - live backup zachrani data
  - Server timeout - exponencialny backoff, automaticke opakovanie
  - Dlhe nahravky (30+ min) - priebezne zalohovane kazdych 10s
- **IndexedDB verzia:** upgradovana z 1 na 2 (pridane pole `nextRetryAt`)
- Zmena v oboch: web (`static/recorder.html`) aj electron (`electron/recorder.html`, `electron-app/recorder.html`)

---

## Klucove poznatky a lekcie
- `electron-builder` NSIS na GitHub Actions ticho zlyha (ziadny Windows output) - nepouzivat
- `electron-packager` + Inno Setup = spolahlivy sposob pre Windows installer
- `apt-get` nefunguje v Render.com Python buildCommand (odstranene od v2.4.1)
- `push_files` tool konvertuje 4-byte emoji na surrogate pairs - pouzit HTML entity
- GitHub owner musi byt `rodriguez-bit` (nie `dajanarodriguez` - vracia 403)
- Whisper halucination je znamy problem pri dlhych nahravkach / tichu
- Windows icon cache vyzaduje `ie4uinit.exe -show` na obnovenie

---

## Subory v repozitari
| Subor | Popis |
|-------|-------|
| `app.py` | Flask backend - API, Whisper, Claude, audio backup |
| `db.py` | Databazova vrstva (SQLite/PostgreSQL) |
| `static/recorder.html` | Web recorder s vizualizerom |
| `electron/main.js` | Electron hlavny proces - okno, tray, auto-update |
| `electron/recorder.html` | Electron recorder verzia |
| `electron/preload.js` | Electron preload script |
| `electron/package.json` | Electron zavislosti (v1.4.0) |
| `.github/workflows/build-windows.yml` | CI/CD pre Windows build |
| `render.yaml` | Render.com konfiguracia |
| `static/sw.js` | Service Worker (cache v2.4.0) |

---

## Odkazy
- **Web app:** https://ridea.onrender.com
- **Windows installer:** https://github.com/rodriguez-bit/idea-capture/releases/latest
- **Android APK:** https://github.com/rodriguez-bit/idea-capture/releases/download/v1.2.0/Ridea-1.2.0.apk
- **GitHub repo:** https://github.com/rodriguez-bit/idea-capture
