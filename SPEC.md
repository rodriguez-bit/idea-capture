# Ridea — Špecifikácia aplikácie

**Verzia:** 1.0.14
**Dátum:** 2026-03-04
**Repo:** `rodriguez-bit/idea-capture`
**Produkcia:** `https://ridea.onrender.com`

---

## Čo Ridea robí

Ridea je interný nástroj pre zachytávanie hlasových nápadov od zamestnancov. Zamestnanec nahrá hlas (max pár minút), app ho automaticky prepíše na text (Whisper AI) a uloží do databázy. Manažéri/admin si nápady prezerajú v admin konzole, hodnotia ich a môžu spustiť AI analýzu (Claude).

**Flow v 3 krokoch:**
1. Zamestnanec otvorí recorder → vyberie oddelenie + rolu → nahrá hlas → odošle
2. Server prepíše audio cez Whisper → uloží do DB
3. Admin si otvorí konzolu → vidí všetky nápady → môže ohodnotiť, komentovať, spustiť AI analýzu, vymazať

---

## Ako je postavená

### Tech stack

| Vrstva | Technológia |
|--------|-------------|
| Backend | Python / Flask (jeden súbor `app.py`, ~760 riadkov) |
| Frontend admin | Vanilla JS SPA (`static/index.html`, žiadny framework, žiadny build step) |
| Frontend recorder | Vanilla JS (`static/recorder.html`) |
| Databáza | SQLite (lokálne) / PostgreSQL Neon.tech (produkcia) |
| Transkripcia | OpenAI Whisper API (`whisper-1`, jazyk SK) |
| AI analýza | Anthropic Claude (`claude-haiku-4-5-20251001`) |
| Hosting | Render.com (auto-deploy z GitHub `main` vetvy) |
| Desktop app | Electron v28 (Windows + Mac) |
| Android app | PWA (inštalovateľná z Chrome) |

---

## Súbory projektu

```
idea-capture/
├── app.py                  # Flask backend — všetky routy, auth, Whisper, Claude, GitHub backup
├── db.py                   # DBConnection abstrakcia (SQLite + PostgreSQL)
├── schema_pg.sql           # PostgreSQL schéma (pre Neon.tech)
├── requirements.txt        # Python závislosti
├── render.yaml             # Render deployment config (gunicorn)
│
├── static/
│   ├── index.html          # Admin konzola (SPA)
│   ├── recorder.html       # Web recorder (PWA shell)
│   ├── manifest.json       # PWA manifest (Android "Pridať na plochu")
│   ├── sw.js               # Service worker (offline shell cache)
│   ├── icon-192.png        # PWA ikona 192×192
│   └── icon-512.png        # PWA ikona 512×512
│
├── electron/
│   ├── main.js             # Electron main process (okno, tray, store, auto-update check)
│   ├── preload.js          # Electron preload (IPC bridge pre renderer)
│   ├── recorder.html       # Electron recorder UI (rovnaký flow ako web)
│   ├── src-clean/          # Čistý zdroj pre packaging (main.js, preload.js, recorder.html, package.json)
│   ├── installer.nsi       # NSIS skript pre Windows installer
│   └── Install-Ridea.bat   # Batch installer pre Windows (stiahne a spustí Setup.exe)
│
└── .github/
    └── workflows/
        └── build-mac.yml   # GitHub Actions: builduje Mac DMG (arm64 + x64)
```

---

## Databázová schéma

### Tabuľka `users`

| Stĺpec | Typ | Popis |
|--------|-----|-------|
| `id` | INTEGER PK | Auto ID |
| `email` | TEXT UNIQUE | Prihlasovací email |
| `display_name` | TEXT | Zobrazené meno |
| `password_hash` | TEXT | bcrypt hash |
| `role` | TEXT | `submitter` / `reviewer` / `admin` |
| `department` | TEXT | Predvolené oddelenie |
| `active` | INTEGER | 1 = aktívny |
| `created_at` | TEXT | Dátum vytvorenia |

### Tabuľka `ideas`

| Stĺpec | Typ | Popis |
|--------|-----|-------|
| `id` | INTEGER PK | Auto ID |
| `author_id` | INTEGER | FK → users.id |
| `author_name` | TEXT | Snapshot mena autora |
| `department` | TEXT | `development` / `marketing` / `production` / `management` / `other` |
| `role` | TEXT | `c-level` / `manager` / `employee` / `majo-markech` |
| `audio_filename` | TEXT | Pôvodný názov súboru (len záznam) |
| `duration_seconds` | INTEGER | Dĺžka nahrávky |
| `transcript` | TEXT | Whisper transkript |
| `status` | TEXT | `new` / `in_review` / `accepted` / `rejected` |
| `ai_score` | INTEGER | Claude skóre 1–10 (0 = neanalyzované) |
| `ai_analysis` | TEXT | JSON s analýzou od Claude |
| `reviewer_note` | TEXT | Komentár reviewera |
| `reviewed_by` | TEXT | Meno reviewera |
| `reviewed_at` | TEXT | Dátum review |
| `created_at` | TEXT | Dátum odoslania |

---

## API endpointy

| Metóda | Route | Auth | Popis |
|--------|-------|------|-------|
| GET | `/` | login_required | Admin konzola (index.html) |
| GET | `/recorder` | login_required | Web recorder |
| GET | `/login` | — | Login stránka |
| GET | `/sw.js` | — | Service worker |
| GET | `/health` | — | Health check |
| POST | `/api/login` | — | Prihlásenie (email + heslo) |
| POST | `/api/logout` | — | Odhlásenie |
| GET | `/api/current-user` | login_required | Info o prihlásenoom užívateľovi |
| GET | `/api/ideas` | login_required | Zoznam nápadov (filtre: dept, role, status, search) |
| GET | `/api/ideas/<id>` | login_required | Detail nápadu |
| PATCH | `/api/ideas/<id>` | reviewer_required | Zmena statusu / poznámky |
| POST | `/api/ideas/upload` | login_required | Upload audia → Whisper transkripcia → uloženie |
| POST | `/api/ideas/<id>/analyze` | login_required | Claude AI analýza nápadu |
| DELETE | `/api/ideas/<id>` | admin_required | Vymazanie nápadu |
| GET | `/api/users` | admin_required | Zoznam používateľov |
| POST | `/api/users` | admin_required | Vytvorenie používateľa |
| PATCH | `/api/users/<id>` | admin_required | Úprava používateľa |
| GET | `/api/stats` | login_required | Štatistiky (počty podľa statusu, dept, role) |

---

## Používateľské roly

| Rola | Čo môže robiť |
|------|---------------|
| `submitter` | Prihlásiť sa, nahrávať nápady |
| `reviewer` | + meniť status nápadov, pridávať poznámky |
| `admin` | + správa používateľov, mazanie nápadov |

---

## Klienti (ako sa pristupuje k app)

### 1. Web browser
- Recorder: `https://ridea.onrender.com/recorder`
- Admin konzola: `https://ridea.onrender.com/`

### 2. Android (PWA)
- Otvor `https://ridea.onrender.com/recorder` v Chrome
- Menu → **"Pridať na plochu"** → Ridea sa nainštaluje ako app
- Funguje ako standalone app (bez URL baru, bez prehliadača)

### 3. Windows desktop
- Inštalátor: `Ridea-Setup-1.0.12.exe` (~75 MB)
- Electron app, pripája sa na `https://ridea.onrender.com`
- Systémová tray ikona, okno sa dá skryť/zobraziť
- Ukladá oddelenie/rolu lokálne (`ridea-config.json` v `%APPDATA%`)
- Automaticky kontroluje nové verzie pri štarte (GitHub Releases API)

### 4. Mac desktop
- Apple Silicon: `Ridea-1.0.14-arm64.dmg` (~296 MB)
- Intel: `Ridea-1.0.14-x64.dmg` (~314 MB)
- Rovnaká Electron app ako Windows
- Unsigned → pri prvom otvorení: pravý klik → Otvoriť → Otvoriť

---

## Dáta a zálohovanie

- Produkčná DB: **PostgreSQL na Neon.tech** (persistentné)
- Render kontajnery sú stateless → po každom deployi sa DB resetuje
- Každá zmena dát spustí automatickú zálohu:
  1. Uloží `ideas_backup.json` lokálne
  2. Pushne ho do GitHub repozitára `dajanarodriguez/ridea` na vetvu `data-backups` (async vlákno)
- Pri štarte servera: ak je DB prázdna → obnoví zo zálohy (GitHub → lokálny súbor → prázdne)

---

## Premenné prostredia

| Premenná | Povinná | Účel |
|----------|---------|------|
| `DATABASE_URL` | Produkcia | PostgreSQL connection string (Neon.tech) |
| `SECRET_KEY` | Produkcia | Session encryption key |
| `DEFAULT_USER_PASSWORD` | Áno | Počiatočné heslo pre seed používateľov |
| `OPENAI_API_KEY` | Pre nahrávanie | Whisper transkripcia |
| `ANTHROPIC_API_KEY` | Pre AI analýzu | Claude hodnotenie nápadov |
| `GITHUB_TOKEN` | Produkcia | GitHub PAT pre backup push/fetch |
| `GITHUB_REPO` | — | Default: `dajanarodriguez/ridea` |
| `FLASK_DEBUG` | Dev | Nastaviť na `true` pre debug mód |

---

## Lokálne spustenie

```bash
# Backend (Python 3.11+)
cd idea-capture
pip install -r requirements.txt
DEFAULT_USER_PASSWORD=tajne python app.py
# Beží na http://localhost:5001

# Electron app
cd electron
npm install
npm start
```

---

## Ako vydať novú verziu

1. Upraviť kód
2. Zvýšiť verziu v `electron/src-clean/package.json` (napr. `1.0.14` → `1.0.15`)
3. **Windows:** Zbuildovať NSIS installer lokálne (`makensis installer.nsi` v `electron/`)
4. **Mac:** Pushnúť na GitHub → spustiť workflow *"Build Mac DMG"* v GitHub Actions
5. Vytvoriť GitHub Release s tagom `v1.0.15` → nahrať `.exe` a `.dmg`
6. Všetci používatelia dostanú automatické upozornenie pri ďalšom štarte Electron app

---

## Seed používatelia (prvé spustenie)

| Email | Meno | Rola |
|-------|------|------|
| `admin@dajanarodriguez.com` | Admin | admin |
| `raul@dajanarodriguez.com` | Raul | reviewer |
| `dajana@dajanarodriguez.com` | Dajana | reviewer |

Heslo: hodnota premennej `DEFAULT_USER_PASSWORD`
