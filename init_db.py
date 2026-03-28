"""
Run once to create the DB and import data from the Excel file.
Usage:  python init_db.py [path/to/Registry.xlsx]
"""
import sys
import sqlite3
import openpyxl
from datetime import datetime
import config

EXCEL_PATH = sys.argv[1] if len(sys.argv) > 1 else 'Portfolio tracker/Registry.xlsx'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS geo_allocation (
    ticker      TEXT NOT NULL,
    country     TEXT NOT NULL,
    etf_weight  REAL,
    PRIMARY KEY (ticker, country)
);
CREATE TABLE IF NOT EXISTS country_region (
    country TEXT PRIMARY KEY,
    region  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT,
    time         TEXT,
    ticker       TEXT NOT NULL,
    op_type      TEXT NOT NULL,
    amount_euro  REAL,
    price_euro   REAL,
    fees_euro    REAL DEFAULT 0,
    quantity     REAL,
    isin         TEXT,
    note         TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS etf_info (
    ticker              TEXT PRIMARY KEY,
    name                TEXT,
    isin                TEXT,
    type                TEXT,
    currency            TEXT,
    ter                 REAL,
    current_price_euro  REAL,
    target_weight       REAL,
    last_price_update   TEXT
);
'''

def cell_val(cell):
    v = cell.value
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d')
    return v


def main():
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()

    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)

    # ── Import transactions from ETF sheet ────────────────────────────────────
    ws_etf = wb['ETF']
    rows_imported = 0
    for row in ws_etf.iter_rows(min_row=2):
        vals = [cell_val(c) for c in row]
        date, time_, ticker, op_type, amount, price, fees, qty, isin, note = (vals + [None]*10)[:10]
        if not ticker or not op_type:
            continue
        ticker = str(ticker).strip().upper()
        if ticker == 'VWCE':
            continue
        op_type = str(op_type).strip().upper()
        date_str = date if date else None
        try:
            amount = float(amount) if amount is not None else None
            price  = float(price)  if price  is not None else None
            fees   = float(fees)   if fees   is not None else 0.0
            qty    = float(qty)    if qty    is not None else None
        except (ValueError, TypeError):
            continue
        conn.execute(
            '''INSERT INTO transactions
               (date, time, ticker, op_type, amount_euro, price_euro, fees_euro, quantity, isin, note)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (date_str, str(time_) if time_ else '', ticker, op_type,
             amount, price, fees, qty, isin, note)
        )
        rows_imported += 1

    print(f'Imported {rows_imported} transactions from ETF sheet.')

    # ── Import ETF metadata from Analytics sheet ──────────────────────────────
    ws_an = wb['Analytics']
    etfs_imported = 0
    for row in ws_an.iter_rows(min_row=2):
        vals = [cell_val(c) for c in row]
        if len(vals) < 16:
            vals += [None] * (16 - len(vals))
        (ticker, type_, currency, holding, cur_price, holding_val, total_spent,
         avg_buy, since_buy_eur, since_buy_pct, delta_contrib, port_weight,
         port_weight_compl, target_weight_cls, target_sb, target_weight_compl) = vals[:16]

        if not ticker:
            continue
        ticker = str(ticker).strip().upper()
        if ticker == 'VWCE':
            continue

        try:
            cur_price            = float(cur_price)            if cur_price            is not None else None
            # store within-class target (0-1, sums to 1 within STOCK or BOND group)
            target_weight        = float(target_weight_cls)    if target_weight_cls    is not None else None
        except (ValueError, TypeError):
            cur_price = None
            target_weight = None

        conn.execute(
            '''INSERT INTO etf_info (ticker, type, currency, current_price_euro, target_weight, last_price_update)
               VALUES (?,?,?,?,?,datetime('now'))
               ON CONFLICT(ticker) DO UPDATE SET
                   type=excluded.type,
                   currency=excluded.currency,
                   current_price_euro=excluded.current_price_euro,
                   target_weight=excluded.target_weight,
                   last_price_update=excluded.last_price_update''',
            (ticker, type_, currency, cur_price, target_weight)
        )
        etfs_imported += 1

    print(f'Imported/updated {etfs_imported} ETFs from Analytics sheet.')

    # ── Ensure etf_info rows exist for all tickers in transactions ────────────
    conn.execute(
        '''INSERT OR IGNORE INTO etf_info (ticker)
           SELECT DISTINCT ticker FROM transactions'''
    )

    # ── Set known ETF names / ISINs from Allocation sheet ────────────────────
    known_names = {
        'CSSPX': ('iShares Core S&P 500 UCITS ETF USD (Acc)',  'IE00B5BMR087', 0.07),
        'EXUS':  ('Xtrackers MSCI World ex USA UCITS ETF',      'IE0006WW1TQ4', 0.15),
        'LCJP':  ('Amundi MSCI Japan UCITS ETF Acc',            'LU1781541252', 0.12),
        'MEUD':  ('Amundi Stoxx Europe 600 UCITS ETF Acc',      'LU0908500753', 0.07),
        'EIMI':  ('iShares Core MSCI EM IMI UCITS ETF (Acc)',   'IE00BKM4GZ66', 0.18),
        'IWVL':  ('iShares Edge MSCI World Value Factor ETF',   'IE00BP3QZB59', 0.25),
        'EM15':  ('Amundi Euro Gov Bond 15+Y UCITS ETF Acc',    'LU1287023268', 0.15),
        'VECA':  ('Vanguard EUR Corporate Bond UCITS ETF Acc',  'IE00BGYWT403', 0.09),
        'GGOV':  ('Amundi Prime Global Gov Bond UCITS ETF Acc', 'LU1437016204', 0.20),
        'EM57':  ('Amundi Euro Gov Bond 5-7Y UCITS ETF Acc',    'LU1287023003', 0.15),
        'VWCE':  ('Vanguard FTSE All-World UCITS ETF (Acc)',    'IE00B3RBWM25', 0.22),
    }
    for ticker, (name, isin, ter) in known_names.items():
        conn.execute(
            'UPDATE etf_info SET name=?, isin=?, ter=? WHERE ticker=? AND (name IS NULL OR name="")',
            (name, isin, ter, ticker)
        )

    # ── Import geographic allocation from Allocation sheet ────────────────────
    ws_alloc = wb['Allocation']

    STOCK_ETFS = [
        ('CSSPX', 0), ('EXUS', 6), ('LCJP', 12),
        ('MEUD', 18), ('EIMI', 24), ('IWVL', 30),
    ]
    BOND_ETFS = [
        ('EM15', 42), ('VECA', 47), ('GGOV', 52), ('EM57', 57),
    ]
    OVERALL_STOCK_OFFSET = 36  # col 36=country, 37=region

    geo_rows = 0
    for row in ws_alloc.iter_rows(min_row=8, values_only=True):
        vals = list(row) + [None] * 10  # padding

        # Stock ETFs – col layout: country, etf_weight, porto, target, None, None
        for ticker, offset in STOCK_ETFS:
            country    = vals[offset]
            etf_weight = vals[offset + 1]
            if country and etf_weight is not None:
                try:
                    w = float(etf_weight)
                    if w > 0:
                        conn.execute(
                            'INSERT OR REPLACE INTO geo_allocation VALUES (?,?,?)',
                            (ticker, str(country).strip(), w)
                        )
                        geo_rows += 1
                except (ValueError, TypeError):
                    pass

        # Bond ETFs – same layout but 5-col block
        for ticker, offset in BOND_ETFS:
            country    = vals[offset]
            etf_weight = vals[offset + 1]
            if country and etf_weight is not None:
                try:
                    w = float(etf_weight)
                    if w > 0:
                        conn.execute(
                            'INSERT OR REPLACE INTO geo_allocation VALUES (?,?,?)',
                            (ticker, str(country).strip(), w)
                        )
                        geo_rows += 1
                except (ValueError, TypeError):
                    pass

        # Country → Region mapping from OVERALL AZIONARIO
        country_name = vals[OVERALL_STOCK_OFFSET]
        region_name  = vals[OVERALL_STOCK_OFFSET + 1]
        if country_name and region_name:
            conn.execute(
                'INSERT OR IGNORE INTO country_region VALUES (?,?)',
                (str(country_name).strip(), str(region_name).strip())
            )

    # Self-mapping for bond region labels
    for r in ["Eurozona", "Sviluppati extra UE", "Emergenti", "Stati Uniti d'America"]:
        conn.execute(
            'INSERT OR IGNORE INTO country_region VALUES (?,?)', (r, r)
        )

    # Extra mappings for countries that may differ from OVERALL table
    extra = {
        'Malaysia':     'Emergenti',
        'Malesia':      'Emergenti',
        'Bermuda':      'Sviluppati extra UE',
        'Lussemburgo':  'Eurozona',
        'Lussenburgo':  'Eurozona',
        'Altri':        'Altro',
        'Giappone':     'Sviluppati extra UE',
    }
    for country, region in extra.items():
        conn.execute(
            'INSERT OR IGNORE INTO country_region VALUES (?,?)', (country, region)
        )

    print(f'Imported {geo_rows} geographic allocation entries.')

    conn.commit()
    conn.close()
    print('Done! Database ready.')


if __name__ == '__main__':
    main()
