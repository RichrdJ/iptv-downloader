# IPTV Series Downloader

Een zelfgehoste web-applicatie om series van Xtream IPTV providers te downloaden. Selecteer een seizoen of download een hele serie in één klik. Draait als Docker container.

---

## Screenshots

| Login | Browse | Seizoenen | Downloads |
|-------|--------|-----------|-----------|
| M3U+ URL invoeren | Bladeren per categorie of zoeken | Seizoen of alles selecteren | Live voortgang per aflevering |

---

## Features

- **Web UI** — toegankelijk via de browser, geen installatie nodig
- **M3U+ URL** — plak je provider URL en je bent klaar
- **Bulk download** — download een heel seizoen of alle seizoenen tegelijk
- **Slimme naamgeving** — `Serie Naam - S01E03 - Aflevering Titel.mkv`
- **Voortgang** — live download progress met stop-knop
- **Skip** — bestanden die al bestaan worden automatisch overgeslagen
- **Accounts opslaan** — meerdere providers opslaan en wisselen
- **ffmpeg** — verwerkt HLS, MPEG-TS, MP4 en MKV streams correct

---

## Snel starten

### Via Portainer (aanbevolen)

1. Ga naar **Stacks → Add stack**
2. Plak de inhoud van [`stack.yml`](stack.yml)
3. Deploy — bereikbaar op poort `2233`

### Via Docker Compose

```bash
git clone https://github.com/RichrdJ/iptv-downloader.git
cd iptv-downloader
docker compose up -d
```

Open vervolgens [http://localhost:2233](http://localhost:2233)

---

## Gebruik

### 1. Verbinden

Plak je M3U+ URL:
```
http://jouw-provider.com/get.php?username=gebruiker&password=wachtwoord&type=m3u_plus
```

Of kies voor **handmatig invoeren** en vul server, gebruikersnaam en wachtwoord apart in.

Vink **Account opslaan** aan om de gegevens te bewaren voor een volgende keer.

### 2. Series zoeken

- Blader door de **categorielijst**
- Of gebruik de **zoekbalk** om direct op naam te zoeken

### 3. Downloaden

1. Klik op een serie
2. Kies een seizoen — of klik **Alle seizoenen downloaden**
3. Volg de voortgang op de **Downloads** pagina

---

## Bestandsnaming

Downloads worden opgeslagen als:

```
/downloads/
└── Breaking Bad/
    ├── Breaking Bad - S01E01 - Pilot.mkv
    ├── Breaking Bad - S01E02 - Cat's in the Bag.mkv
    └── Breaking Bad - S02E01 - Seven Thirty-Seven.mkv
```

---

## Configuratie

| Omgevingsvariabele | Standaard | Omschrijving |
|--------------------|-----------|--------------|
| `CONFIG_DIR` | `/config` | Locatie opgeslagen accounts |
| `DOWNLOAD_DIR` | `/downloads` | Locatie gedownloade bestanden |
| `SECRET_KEY` | willekeurig | Flask sessie sleutel (stel in voor persistente sessies) |

### Volumes

| Container pad | Omschrijving |
|---------------|--------------|
| `/config` | Opgeslagen accounts (`accounts.json`) |
| `/downloads` | Gedownloade afleveringen |

---

## Stack YAML

```yaml
services:
  iptv-downloader:
    image: ghcr.io/richrdj/iptv-downloader:latest
    container_name: iptv-downloader
    ports:
      - "2233:2233"
    volumes:
      - iptv_config:/config
      - iptv_downloads:/downloads
    restart: unless-stopped

volumes:
  iptv_config:
  iptv_downloads:
```

---

## Techniek

| Component | Keuze |
|-----------|-------|
| Backend | Python 3.12 + Flask |
| Downloader | ffmpeg |
| IPTV protocol | Xtream Codes API |
| Container | Docker (image via GHCR) |
| UI | Vanilla HTML/CSS/JS — geen framework |

---

## Licentie

GPL-3.0
