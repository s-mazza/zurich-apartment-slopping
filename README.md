# Apartment Finder (Zurigo)

Script per raccogliere annunci da Flatfox, filtrarli con criteri configurabili e generare:

- `output/listings_filtered.csv`
- `output/listings_filtered.md` (con link cliccabili, incluso contatto 1-click)
- `quick_contact_message.txt`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Uso

```bash
python3 src/apartment_finder.py
```

## Configurazione

Tutti i parametri sono in `config.yaml`:

- URL di ricerca Flatfox
- Data limite disponibilita
- Camere minime
- Vincoli (arredato, cucina, bagno, soggiorno, divano, ecc.)
- Feature opzionali (lavatrice, lavastoviglie)

L'impostazione `include_unknowns_to_avoid_false_negatives: true` mantiene un comportamento permissivo:
se un dato non e esplicito nell'annuncio, non viene scartato automaticamente.

## Note importanti

- Il parser tenta prima endpoint API pubblici, poi fallback HTML.
- Se Flatfox mostra challenge anti-bot, e attivo fallback Playwright (browser reale).
- Alcuni annunci potrebbero richiedere sessione loggata/cookie per dettagli o contatto.
- In caso di blocchi anti-bot, serve usare cookie browser/sessione autenticata.

### Per challenge/cookie

Configura `search.flatfox.playwright` in `config.yaml`:

- `headless: false` per vedere il browser
- `manual_continue: true` per risolvere challenge manualmente e premere Invio
- `cookies` per iniettare cookie sessione Flatfox
- `dump_html_path` per salvare HTML finale utile al debug
