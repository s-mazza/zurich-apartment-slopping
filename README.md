# Zurich Apartment Finder 🏠🇨🇭

Uno strumento professionale per automatizzare la ricerca di appartamenti a Zurigo, ottimizzato per la zona **Europaallee**. Aggrega annunci da più portali, calcola le distanze dall'ufficio e usa l'AI per filtrare solo i risultati migliori.

## 🚀 Caratteristiche Principali

- **Multi-Provider:** Supporto completo per **Flatfox**, **Homegate** e **Comparis.ch**.
- **LLM Reasoning (AI):** Usa modelli di pensiero avanzati (**DeepSeek-R1**) per interpretare le descrizioni e scartare annunci non idonei (es. stanze in condivisione, subaffitti temporanei).
- **Paginazione Automatica:** Recupera tutti i risultati disponibili (es. oltre 250 annunci su Homegate).
- **Distanza Smart:** Calcola automaticamente la distanza in km dall'ufficio (Europaallee 1) usando coordinate geografiche reali.
- **Bypass Anti-Bot:** Sistema di "Stealth Masking" e iniezione automatica di cookie per superare Cloudflare e DataDome.
- **Audit Completo:** Genera report dettagliati degli appartamenti idonei e di quelli scartati (con motivazione).

## 🛠️ Setup

```bash
# 1. Crea e attiva virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 2. Installa dipendenze
pip install -r requirements.txt
playwright install chromium
```

## 📖 Utilizzo

### Modalità Standard (Veloce)
Usa regex e keyword predefinite. Ideale per una scansione rapida.
```bash
python3 src/apartment_finder.py
```

### Modalità AI (Consigliata)
Usa un modello linguistico su Hugging Face per un filtraggio perfetto.
```bash
python3 src/apartment_finder_llm.py
```

**Opzioni Avanzate:**
- Filtra per provider: `python3 src/apartment_finder_llm.py --providers homegate`
- Configurazione custom: `python3 src/apartment_finder_llm.py --config config_alt.yaml`

## ⚙️ Configurazione (`config.yaml`)

Il file è diviso in tre sezioni chiave:
1.  **Search:** Parametri geografici (bounding box), locali minimi e impostazioni Playwright.
2.  **LLM:** Token di Hugging Face e ID del modello (default: `DeepSeek-R1-Distill-Qwen-32B`).
3.  **Criteria:** Filtri locali rigorosi come `max_price`, `must_be_indefinite` (no temporanei), `min_bedrooms`.

### Gestione Cookie (Homegate)
Se vieni bloccato, crea un file `cookies_homegate.txt` nella root e incolla i cookie esportati dal tuo browser. Lo script li inietterà automaticamente.

## 📊 Output

I risultati vengono salvati nella cartella `output/`:
- `listings_filtered_llm.md`: Elenco ordinato degli appartamenti idonei con link 1-click.
- `listings_excluded_llm.md`: Log di audit con il motivo dello scarto per ogni annuncio.
- `apartment_finder_llm.log`: Log tecnici per il debug.

## 🧪 Test Suite

Ho incluso una suite di **48 test** per garantire la precisione del parsing (prezzi, date, distanze).
```bash
PYTHONPATH=. pytest tests/test_apartment_finder.py
```

---
*Progettato per trovare casa a Zurigo in modo chirurgico, evitando perdite di tempo con annunci irrilevanti.*
