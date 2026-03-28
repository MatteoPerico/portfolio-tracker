# Portfolio Tracker

A private, password-protected web app to track your ETF investment portfolio.

## Features

- Dashboard with portfolio value, P&L, and allocation charts
- Analytics with per-ETF breakdown (weight vs target, within asset class)
- Geographic allocation analysis split by azionario / obbligazionario
- Manual target weight setting with live chart preview
- One-click price refresh via Yahoo Finance (EUR-denominated)
- Transaction history with add/delete

## Setup

### 1. Install dependencies

```bash
pip install flask>=3.0 openpyxl>=3.1
```

### 2. Configure

```bash
cp config.example.py config.py
# Edit config.py and set your SECRET_KEY and PASSWORD
```

### 3. Initialize the database

Import your data from an Excel file with ETF, Analytics and Allocation sheets:

```bash
python init_db.py /path/to/Registry.xlsx
```

### 4. Run

```bash
python app.py
```

Open http://localhost:5001 and log in with the password set in `config.py`.

## Notes

- `config.py` is git-ignored — never commit it
- `portfolio.db` is git-ignored — it contains your personal data
- VWCE transactions are intentionally excluded from all calculations
- Price updates fetch EUR-denominated listings from Yahoo Finance (Borsa Italiana `.MI` / Euronext `.PA`)
- Yahoo Finance ticker mapping can be customised in `config.py` under `YAHOO_TICKERS`
