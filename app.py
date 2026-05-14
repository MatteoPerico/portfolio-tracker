from flask import Flask, render_template, request, jsonify
import sqlite3
import os
from datetime import datetime
import urllib.request
import json as _json
import math as _math
import config

app = Flask(__name__)


def login_required(f):
    """No-op decorator — auth disabled."""
    return f


def get_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── API: transactions ─────────────────────────────────────────────────────────

@app.route('/api/transactions', methods=['GET'])
@login_required
def get_transactions():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM transactions WHERE ticker != 'VWCE' ORDER BY date DESC, time DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/transactions', methods=['POST'])
@login_required
def add_transaction():
    d = request.json
    db = get_db()
    quantity = float(d.get('quantity') or 0)
    amount   = float(d.get('amount_euro') or 0)
    price    = float(d.get('price_euro') or 0)
    fees     = float(d.get('fees_euro') or 0)
    if not quantity and price:
        quantity = amount / price
    db.execute(
        '''INSERT INTO transactions
           (date, time, ticker, op_type, amount_euro, price_euro, fees_euro, quantity, isin, note)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (d['date'], d.get('time', ''), d['ticker'].upper(), d['op_type'],
         amount, price, fees, quantity, d.get('isin', ''), d.get('note', ''))
    )
    # upsert etf_info if missing
    db.execute(
        '''INSERT OR IGNORE INTO etf_info (ticker) VALUES (?)''',
        (d['ticker'].upper(),)
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/transactions/<int:tid>', methods=['DELETE'])
@login_required
def delete_transaction(tid):
    db = get_db()
    db.execute('DELETE FROM transactions WHERE id = ?', (tid,))
    db.commit()
    return jsonify({'ok': True})


# ── API: analytics ────────────────────────────────────────────────────────────

@app.route('/api/analytics', methods=['GET'])
@login_required
def get_analytics():
    db = get_db()
    rows = db.execute(
        '''SELECT
               t.ticker,
               e.name,
               e.type,
               e.currency,
               e.current_price_euro,
               e.last_price_update,
               e.target_weight,
               e.isin,
               SUM(CASE WHEN t.op_type='BUY'  THEN  t.quantity ELSE -t.quantity END) AS holding,
               SUM(CASE WHEN t.op_type='BUY'  THEN  t.amount_euro - t.fees_euro
                        ELSE -t.amount_euro END) AS cost_basis,
               SUM(t.fees_euro) AS total_fees
           FROM transactions t
           LEFT JOIN etf_info e ON t.ticker = e.ticker
           GROUP BY t.ticker
           HAVING holding > 0.00001 AND t.ticker != 'VWCE'
           ORDER BY e.type, t.ticker'''
    ).fetchall()

    result = []
    total_value = 0.0
    for r in rows:
        item = dict(r)
        price = item['current_price_euro']
        holding = item['holding'] or 0
        cost = item['cost_basis'] or 0
        item['holding_value'] = round(holding * price, 2) if price else None
        item['avg_buy_price']  = round(cost / holding, 4) if holding else None
        item['since_buy_euro'] = round(item['holding_value'] - cost, 2) if price else None
        item['since_buy_pct']  = round((item['holding_value'] - cost) / cost * 100, 2) if (price and cost) else None
        if item['holding_value']:
            total_value += item['holding_value']
        result.append(item)

    stock_val = sum(r['holding_value'] or 0 for r in result if r['type'] == 'STOCK')
    bond_val  = sum(r['holding_value'] or 0 for r in result if r['type'] == 'BOND')

    for item in result:
        if item['holding_value'] and total_value:
            item['portfolio_weight'] = round(item['holding_value'] / total_value * 100, 2)
        else:
            item['portfolio_weight'] = None
        cls_val = stock_val if item['type'] == 'STOCK' else bond_val
        item['class_weight'] = round(item['holding_value'] / cls_val * 100, 2) if (item['holding_value'] and cls_val) else None

    total_cost = sum(r['cost_basis'] or 0 for r in result)
    total_pnl  = round(total_value - total_cost, 2)
    total_pnl_pct = round(total_pnl / total_cost * 100, 2) if total_cost else None

    stock_value = sum(r['holding_value'] or 0 for r in result if r['type'] == 'STOCK')
    bond_value  = sum(r['holding_value'] or 0 for r in result if r['type'] == 'BOND')

    # ── Period P&L (3 months / 12 months) from price_cache ──────────────────
    try:
        db.execute('SELECT 1 FROM price_cache LIMIT 1')
        cache_ok = True
    except Exception:
        cache_ok = False

    date_3m  = (datetime.utcnow() - __import__('datetime').timedelta(days=91)).strftime('%Y-%m-%d')
    date_12m = (datetime.utcnow() - __import__('datetime').timedelta(days=365)).strftime('%Y-%m-%d')

    val_3m_ago  = 0.0
    val_12m_ago = 0.0
    has_3m = has_12m = False

    if cache_ok:
        for item in result:
            ticker  = item['ticker']
            holding = item['holding'] or 0
            cur_p   = item['current_price_euro']
            if not cur_p or not holding:
                item['pnl_3m_pct'] = item['pnl_12m_pct'] = None
                continue

            r3 = db.execute(
                'SELECT price FROM price_cache WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 1',
                (ticker, date_3m)
            ).fetchone()
            r12 = db.execute(
                'SELECT price FROM price_cache WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 1',
                (ticker, date_12m)
            ).fetchone()

            if r3:
                p3 = r3['price']
                item['pnl_3m_pct'] = round((cur_p - p3) / p3 * 100, 2) if p3 else None
                val_3m_ago += holding * p3
                has_3m = True
            else:
                item['pnl_3m_pct'] = None

            if r12:
                p12 = r12['price']
                item['pnl_12m_pct'] = round((cur_p - p12) / p12 * 100, 2) if p12 else None
                val_12m_ago += holding * p12
                has_12m = True
            else:
                item['pnl_12m_pct'] = None
    else:
        for item in result:
            item['pnl_3m_pct'] = item['pnl_12m_pct'] = None

    total_pnl_3m      = round(total_value - val_3m_ago,  2) if has_3m  else None
    total_pnl_3m_pct  = round(total_pnl_3m  / val_3m_ago  * 100, 2) if (has_3m  and val_3m_ago)  else None
    total_pnl_12m     = round(total_value - val_12m_ago, 2) if has_12m else None
    total_pnl_12m_pct = round(total_pnl_12m / val_12m_ago * 100, 2) if (has_12m and val_12m_ago) else None

    return jsonify({
        'etfs': result,
        'total_value': round(total_value, 2),
        'total_cost': round(total_cost, 2),
        'total_pnl': total_pnl,
        'total_pnl_pct': total_pnl_pct,
        'total_pnl_3m': total_pnl_3m,
        'total_pnl_3m_pct': total_pnl_3m_pct,
        'total_pnl_12m': total_pnl_12m,
        'total_pnl_12m_pct': total_pnl_12m_pct,
        'stock_value': round(stock_value, 2),
        'bond_value': round(bond_value, 2),
    })


# ── API: ETF info / prices ────────────────────────────────────────────────────

@app.route('/api/etf_info', methods=['GET'])
@login_required
def get_etf_info():
    db = get_db()
    rows = db.execute('SELECT * FROM etf_info ORDER BY ticker').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/etf_info/<ticker>', methods=['PUT'])
@login_required
def update_etf_info(ticker):
    d = request.json
    db = get_db()
    db.execute(
        '''INSERT INTO etf_info (ticker, name, isin, type, currency, ter, current_price_euro, target_weight, last_price_update)
           VALUES (:ticker,:name,:isin,:type,:currency,:ter,:price,:target,CURRENT_TIMESTAMP)
           ON CONFLICT(ticker) DO UPDATE SET
               name=excluded.name, isin=excluded.isin, type=excluded.type,
               currency=excluded.currency, ter=excluded.ter,
               current_price_euro=excluded.current_price_euro,
               target_weight=excluded.target_weight,
               last_price_update=CURRENT_TIMESTAMP''',
        {
            'ticker': ticker.upper(),
            'name': d.get('name', ''),
            'isin': d.get('isin', ''),
            'type': d.get('type', ''),
            'currency': d.get('currency', ''),
            'ter': d.get('ter'),
            'price': d.get('current_price_euro'),
            'target': d.get('target_weight'),
        }
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/update_price', methods=['POST'])
@login_required
def update_price():
    d = request.json
    ticker = d['ticker'].upper()
    price  = float(d['price'])
    db = get_db()
    db.execute(
        '''UPDATE etf_info SET current_price_euro=?, last_price_update=CURRENT_TIMESTAMP WHERE ticker=?''',
        (price, ticker)
    )
    if db.execute('SELECT changes()').fetchone()[0] == 0:
        db.execute(
            'INSERT INTO etf_info (ticker, current_price_euro, last_price_update) VALUES (?,?,CURRENT_TIMESTAMP)',
            (ticker, price)
        )
    db.commit()
    return jsonify({'ok': True})


# ── API: geographic allocation ────────────────────────────────────────────────

@app.route('/api/geo_allocation', methods=['GET'])
@login_required
def get_geo_allocation():
    db = get_db()

    # ETF metadata (exclude VWCE)
    etf_meta = {r['ticker']: dict(r) for r in db.execute(
        "SELECT ticker, type, current_price_euro, target_weight FROM etf_info WHERE ticker != 'VWCE'"
    ).fetchall()}

    # Current holdings
    holdings = db.execute(
        '''SELECT t.ticker,
               SUM(CASE WHEN t.op_type='BUY' THEN t.quantity ELSE -t.quantity END) AS holding
           FROM transactions t WHERE t.ticker != 'VWCE'
           GROUP BY t.ticker HAVING holding > 0.00001'''
    ).fetchall()

    etf_values = {}
    for r in holdings:
        info = etf_meta.get(r['ticker'], {})
        price = info.get('current_price_euro')
        if price:
            etf_values[r['ticker']] = r['holding'] * price

    stock_total = sum(v for t, v in etf_values.items() if etf_meta.get(t, {}).get('type') == 'STOCK')
    bond_total  = sum(v for t, v in etf_values.items() if etf_meta.get(t, {}).get('type') == 'BOND')

    # Geo weights per ETF
    geo_rows = db.execute(
        '''SELECT g.ticker, g.country, g.etf_weight,
               COALESCE(cr.region, g.country) AS region
           FROM geo_allocation g
           LEFT JOIN country_region cr ON g.country = cr.country
           WHERE g.ticker != 'VWCE'
           ORDER BY g.ticker, g.etf_weight DESC'''
    ).fetchall()

    # Build per_etf dict (used also for client-side target recalc)
    per_etf = {}
    for r in geo_rows:
        t = r['ticker']
        if t not in per_etf:
            info  = etf_meta.get(t, {})
            cls   = info.get('type')
            cls_t = stock_total if cls == 'STOCK' else bond_total
            per_etf[t] = {
                'type':        cls,
                'current_pct': round(etf_values.get(t, 0) / cls_t * 100, 4) if cls_t else 0,
                'target_pct':  round((info.get('target_weight') or 0) * 100, 4),
                'countries':   [],
            }
        per_etf[t]['countries'].append({
            'country':    r['country'],
            'etf_weight': round(r['etf_weight'] * 100, 4),
            'region':     r['region'],
        })

    def agg_geo(asset_class, use_target):
        class_etfs = {t: d for t, d in per_etf.items() if d['type'] == asset_class}
        total_w    = sum(d['target_pct' if use_target else 'current_pct'] for d in class_etfs.values())
        country_exp = {}
        for t, d in class_etfs.items():
            w = (d['target_pct' if use_target else 'current_pct'] / total_w) if total_w else 0
            for c in d['countries']:
                key = c['country']
                contrib = c['etf_weight'] / 100 * w
                if key not in country_exp:
                    country_exp[key] = {'exposure': 0.0, 'region': c['region']}
                country_exp[key]['exposure'] += contrib
        countries = sorted(
            [{'country': k, 'region': v['region'], 'exposure_pct': round(v['exposure'] * 100, 2)}
             for k, v in country_exp.items() if v['exposure'] > 0.0001],
            key=lambda x: -x['exposure_pct']
        )
        regions = {}
        for c in countries:
            rg = c['region'] or 'Altro'
            regions[rg] = round(regions.get(rg, 0) + c['exposure_pct'], 2)
        return {'countries': countries, 'regions': regions}

    return jsonify({
        'per_etf':     per_etf,
        'stock': {'current': agg_geo('STOCK', False), 'target': agg_geo('STOCK', True)},
        'bond':  {'current': agg_geo('BOND',  False), 'target': agg_geo('BOND',  True)},
        'stock_value': round(stock_total, 2),
        'bond_value':  round(bond_total, 2),
    })


@app.route('/api/update_target', methods=['POST'])
@login_required
def update_target():
    targets = request.json  # {ticker: pct_within_class (0-100)}
    db = get_db()
    for ticker, pct in targets.items():
        if ticker == 'VWCE':
            continue
        db.execute('UPDATE etf_info SET target_weight=? WHERE ticker=?',
                   (float(pct) / 100, ticker))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/geo_to_etf', methods=['POST'])
@login_required
def geo_to_etf():
    """Optimise STOCK ETF weights to best match a given country target allocation."""
    country_targets = request.json or {}
    if not country_targets:
        return jsonify({'error': 'Nessun target fornito'}), 400

    db = get_db()
    stock_etfs = [r['ticker'] for r in db.execute(
        "SELECT ticker FROM etf_info WHERE type='STOCK' AND ticker NOT IN ('VWCE','IVWL')"
    ).fetchall()]
    if not stock_etfs:
        return jsonify({'error': 'Nessun ETF azionario trovato'}), 400

    geo_rows = db.execute(
        'SELECT ticker, country, etf_weight FROM geo_allocation WHERE ticker IN ({})'.format(
            ','.join('?' * len(stock_etfs))
        ), stock_etfs
    ).fetchall()

    geo_matrix = {}
    for r in geo_rows:
        geo_matrix.setdefault(r['ticker'], {})[r['country']] = r['etf_weight']

    weights = _solve_geo_to_etf(geo_matrix, country_targets)

    etf_info = {r['ticker']: dict(r) for r in db.execute(
        "SELECT ticker, name, target_weight FROM etf_info WHERE type='STOCK' AND ticker NOT IN ('VWCE','IVWL')"
    ).fetchall()}

    result = sorted(
        [{'ticker': t,
          'name': etf_info.get(t, {}).get('name', t),
          'proposed_weight': round(w * 100, 2),
          'current_target':  round((etf_info.get(t, {}).get('target_weight') or 0) * 100, 2)}
         for t, w in weights.items()],
        key=lambda x: -x['proposed_weight']
    )

    # Implied geographic exposure from proposed weights
    implied_geo: dict = {}
    for t, w in weights.items():
        for country, ef in geo_matrix.get(t, {}).items():
            implied_geo[country] = implied_geo.get(country, 0.0) + w * ef
    implied = sorted(
        [{'country': c, 'pct': round(v * 100, 2)}
         for c, v in implied_geo.items() if v > 0.0001],
        key=lambda x: -x['pct']
    )

    return jsonify({'weights': result, 'implied_geo': implied})


# ── API: timeseries ───────────────────────────────────────────────────────────

@app.route('/api/timeseries', methods=['GET'])
@login_required
def get_timeseries():
    db = get_db()
    rows = db.execute(
        '''SELECT date,
               SUM(CASE WHEN op_type='BUY' THEN amount_euro ELSE 0 END) AS invested,
               SUM(CASE WHEN op_type='SELL' THEN amount_euro ELSE 0 END) AS withdrawn
           FROM transactions
           GROUP BY date ORDER BY date'''
    ).fetchall()
    cumulative = 0.0
    result = []
    for r in rows:
        cumulative += (r['invested'] or 0) - (r['withdrawn'] or 0)
        result.append({'date': r['date'], 'invested': round(cumulative, 2)})
    return jsonify(result)


# ── Helpers: price history ────────────────────────────────────────────────────

def _ensure_cache_table(db):
    db.execute('''
        CREATE TABLE IF NOT EXISTS price_cache (
            ticker    TEXT,
            date      TEXT,
            price     REAL,
            cached_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, date)
        )
    ''')
    db.commit()


def _fetch_history_yahoo(yahoo_ticker, range_str='2y', interval='1wk'):
    """Fetch weekly close prices from Yahoo Finance chart API. Returns ([(date, price)], currency)."""
    url = (f'https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}'
           f'?interval={interval}&range={range_str}')
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        data = _json.loads(r.read())
    result   = data['chart']['result'][0]
    timestamps = result['timestamp']
    closes   = result['indicators']['adjclose'][0]['adjclose']
    currency = result['meta'].get('currency', 'EUR')
    pairs = []
    for ts, close in zip(timestamps, closes):
        if close is not None:
            date = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
            pairs.append((date, close))
    return pairs, currency


def _load_prices(db, tickers):
    """Return {ticker: {date: price_eur}}, fetching from cache or Yahoo."""
    from datetime import datetime as _dt, timedelta as _td
    _ensure_cache_table(db)
    cutoff = (_dt.utcnow() - _td(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    all_prices = {}
    for ticker in tickers:
        yahoo_ticker = config.YAHOO_TICKERS.get(ticker)
        if not yahoo_ticker:
            continue
        cached = db.execute(
            'SELECT date, price FROM price_cache WHERE ticker=? AND cached_at>? ORDER BY date',
            (ticker, cutoff)
        ).fetchall()
        if cached:
            all_prices[ticker] = {r['date']: r['price'] for r in cached}
        else:
            try:
                history, currency = _fetch_history_yahoo(yahoo_ticker)
                if currency != 'EUR':
                    rate = _fx_rate(currency)
                    history = [(d, p * rate) for d, p in history]
                db.execute('DELETE FROM price_cache WHERE ticker=?', (ticker,))
                db.executemany(
                    'INSERT OR REPLACE INTO price_cache (ticker, date, price) VALUES (?,?,?)',
                    [(ticker, d, p) for d, p in history]
                )
                db.commit()
                all_prices[ticker] = dict(history)
            except Exception as e:
                app.logger.warning(f'Price history fetch failed for {ticker}: {e}')
    return all_prices


def _pearson(x, y):
    n = min(len(x), len(y))
    if n < 6:
        return None
    x, y = x[:n], y[:n]
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    sx  = _math.sqrt(sum((xi - mx)**2 for xi in x))
    sy  = _math.sqrt(sum((yi - my)**2 for yi in y))
    if sx < 1e-12 or sy < 1e-12:
        return None
    return round(num / (sx * sy), 3)


def _log_returns(prices):
    out = []
    for i in range(1, len(prices)):
        p0, p1 = prices[i-1], prices[i]
        if p0 and p1 and p0 > 0 and p1 > 0:
            out.append(_math.log(p1 / p0))
    return out


def _proj_simplex(v):
    """Project vector v onto the probability simplex {w: sum=1, w>=0}."""
    n = len(v)
    u = sorted(v, reverse=True)
    cumsum = 0.0
    rho = 0
    for i in range(n):
        cumsum += u[i]
        if u[i] - (cumsum - 1.0) / (i + 1) > 0:
            rho = i + 1
    theta = (sum(u[:rho]) - 1.0) / rho
    return [max(vi - theta, 0.0) for vi in v]


def _solve_geo_to_etf(geo_matrix, country_targets, n_iter=3000):
    """
    Solve  min ||G·w - t||²  s.t.  sum(w)=1, w≥0  (projected gradient descent).

    geo_matrix     : {ticker: {country: fraction}}  (fractions sum to 1 per ETF)
    country_targets: {country: pct}  (any scale; normalised to sum=1 internally)
    Returns        : {ticker: weight in [0,1], summing to 1}
    """
    tickers  = sorted(geo_matrix.keys())
    # Only include countries that appear in at least one ETF
    countries = [c for c in country_targets
                 if any(c in geo_matrix[t] for t in tickers)]
    n_e, n_c = len(tickers), len(countries)
    if n_e == 0 or n_c == 0:
        uni = 1.0 / max(n_e, 1)
        return {t: uni for t in tickers}
    # Build G matrix [n_c × n_e]
    G = [[geo_matrix[t].get(c, 0.0) for t in tickers] for c in countries]
    # Normalise target to probability vector
    t_sum = sum(country_targets.get(c, 0.0) for c in countries)
    if t_sum <= 0:
        uni = 1.0 / n_e
        return {t: uni for t in tickers}
    T = [country_targets.get(c, 0.0) / t_sum for c in countries]
    # Lipschitz constant L = 2·||G||²_F  →  step size lr = 1/L
    L  = 2.0 * sum(G[c][e] ** 2 for c in range(n_c) for e in range(n_e)) + 1e-9
    lr = 1.0 / L
    w  = [1.0 / n_e] * n_e
    for _ in range(n_iter):
        Gw   = [sum(G[c][e] * w[e] for e in range(n_e)) for c in range(n_c)]
        r    = [Gw[c] - T[c]        for c in range(n_c)]
        grad = [2.0 * sum(G[c][e] * r[c] for c in range(n_c)) for e in range(n_e)]
        w    = _proj_simplex([w[e] - lr * grad[e] for e in range(n_e)])
    return {tickers[e]: round(w[e], 4) for e in range(n_e)}


# ── API: refresh prices from Yahoo Finance ────────────────────────────────────

def _yahoo_price(yahoo_ticker):
    """Fetch latest price and currency via Yahoo Finance chart API (no dependencies)."""
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}?interval=1d&range=1d'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        data = _json.loads(r.read())
    meta = data['chart']['result'][0]['meta']
    return meta['regularMarketPrice'], meta.get('currency', 'EUR')


def _fx_rate(from_currency):
    """Return EUR value of 1 unit of from_currency."""
    if from_currency == 'EUR':
        return 1.0
    pair = f'{from_currency}EUR=X'
    price, _ = _yahoo_price(pair)
    if from_currency == 'GBp':   # pence → pounds → EUR
        return price / 100
    return price


@app.route('/api/refresh_prices', methods=['POST'])
@login_required
def refresh_prices():
    db = get_db()
    tickers = [r['ticker'] for r in db.execute(
        "SELECT DISTINCT ticker FROM etf_info WHERE ticker != 'VWCE'"
    ).fetchall()]

    results = {}
    for ticker in tickers:
        yahoo_ticker = config.YAHOO_TICKERS.get(ticker)
        if not yahoo_ticker:
            results[ticker] = {'ok': False, 'error': 'no Yahoo ticker configured'}
            continue
        try:
            price, currency = _yahoo_price(yahoo_ticker)
            if currency != 'EUR':
                rate = _fx_rate(currency)
                price = price * rate
            price = round(price, 4)
            db.execute(
                'UPDATE etf_info SET current_price_euro=?, last_price_update=CURRENT_TIMESTAMP WHERE ticker=?',
                (price, ticker)
            )
            results[ticker] = {'ok': True, 'price': price, 'source': yahoo_ticker}
        except Exception as e:
            results[ticker] = {'ok': False, 'error': str(e)}

    db.commit()
    ok_count = sum(1 for v in results.values() if v['ok'])
    return jsonify({'results': results, 'updated': ok_count, 'total': len(tickers)})


@app.route('/api/performance', methods=['GET'])
@login_required
def get_performance():
    db = get_db()
    tickers = [r['ticker'] for r in db.execute(
        "SELECT DISTINCT ticker FROM etf_info WHERE ticker != 'VWCE'"
    ).fetchall()]

    all_prices = _load_prices(db, tickers)
    if not all_prices:
        return jsonify({'timeseries': []})

    # Transactions sorted by date
    txns = db.execute(
        '''SELECT ticker, date, op_type, quantity, amount_euro, fees_euro
           FROM transactions WHERE ticker != 'VWCE' ORDER BY date'''
    ).fetchall()

    # Build sorted price lookup per ticker for forward-fill
    from bisect import bisect_right as _bsr
    price_arrays = {}
    for ticker, pd_ in all_prices.items():
        dates_sorted = sorted(pd_.keys())
        price_arrays[ticker] = (dates_sorted, [pd_[d] for d in dates_sorted])

    def price_at(ticker, date):
        if ticker not in price_arrays:
            return None
        dates, prices = price_arrays[ticker]
        idx = _bsr(dates, date) - 1
        return prices[idx] if idx >= 0 else None

    # Union of all price dates
    all_dates = sorted(set(d for pd_ in all_prices.values() for d in pd_.keys()))

    shares    = {}
    invested  = 0.0
    txn_list  = list(txns)
    txn_idx   = 0
    result    = []

    for date in all_dates:
        while txn_idx < len(txn_list) and txn_list[txn_idx]['date'] <= date:
            t = txn_list[txn_idx]
            tk = t['ticker']
            shares.setdefault(tk, 0.0)
            amt = (t['amount_euro'] or 0)
            fee = (t['fees_euro'] or 0)
            if t['op_type'] == 'BUY':
                shares[tk] += t['quantity']
                invested   += amt          # total cash out (incl fees)
            else:
                shares[tk] -= t['quantity']
                invested   -= amt
            txn_idx += 1

        value = 0.0
        for tk, qty in shares.items():
            if qty > 0:
                p = price_at(tk, date)
                if p:
                    value += qty * p

        if value > 0 or invested > 0:
            result.append({'date': date, 'value': round(value, 2), 'invested': round(invested, 2)})

    return jsonify({'timeseries': result})


@app.route('/api/risk', methods=['GET'])
@login_required
def get_risk():
    db = get_db()
    tickers = [r['ticker'] for r in db.execute(
        "SELECT DISTINCT ticker FROM etf_info WHERE ticker != 'VWCE'"
    ).fetchall()]

    all_prices = _load_prices(db, tickers)

    # ── Correlation ──────────────────────────────────────────────────────────
    # Common dates across all ETFs with prices
    if all_prices:
        common_dates = sorted(set.intersection(*[set(pd_.keys()) for pd_ in all_prices.values()]))
    else:
        common_dates = []

    return_series = {}
    for ticker, pd_ in all_prices.items():
        prices_seq = [pd_[d] for d in common_dates]
        return_series[ticker] = _log_returns(prices_seq)

    ticker_list = list(return_series.keys())
    correlation = {}
    for t1 in ticker_list:
        correlation[t1] = {}
        for t2 in ticker_list:
            if t1 == t2:
                correlation[t1][t2] = 1.0
            elif t2 in correlation and t1 in correlation.get(t2, {}):
                correlation[t1][t2] = correlation[t2][t1]
            else:
                correlation[t1][t2] = _pearson(return_series[t1], return_series[t2])

    # ── HHI ─────────────────────────────────────────────────────────────────
    holdings = db.execute(
        '''SELECT t.ticker, e.type, e.current_price_euro,
               SUM(CASE WHEN t.op_type='BUY' THEN t.quantity ELSE -t.quantity END) AS qty
           FROM transactions t LEFT JOIN etf_info e ON t.ticker=e.ticker
           WHERE t.ticker != 'VWCE' GROUP BY t.ticker HAVING qty > 0.00001'''
    ).fetchall()

    total_val = sum((r['qty'] * r['current_price_euro']) for r in holdings if r['current_price_euro'])
    hhi = 0.0
    if total_val > 0:
        for r in holdings:
            if r['current_price_euro']:
                w = r['qty'] * r['current_price_euro'] / total_val
                hhi += w * w
    hhi_score = round(hhi * 10000)

    # ── Currency exposure ────────────────────────────────────────────────────
    currency_rows = db.execute(
        "SELECT ticker, currency FROM etf_info WHERE ticker != 'VWCE'"
    ).fetchall()
    cur_map = {r['ticker']: (r['currency'] or 'EUR') for r in currency_rows}

    currency_val = {}
    for r in holdings:
        if r['current_price_euro']:
            cur = cur_map.get(r['ticker'], 'EUR')
            val = r['qty'] * r['current_price_euro']
            currency_val[cur] = currency_val.get(cur, 0.0) + val

    currency_pct = {}
    if total_val > 0:
        for cur, val in currency_val.items():
            currency_pct[cur] = round(val / total_val * 100, 2)

    return jsonify({
        'correlation': correlation,
        'tickers': ticker_list,
        'hhi': hhi_score,
        'currency_exposure': currency_pct,
        'data_points': len(common_dates),
    })


@app.route('/api/simulate_etf', methods=['POST'])
@login_required
def simulate_etf():
    d      = request.json or {}
    yticker = d.get('ticker', '').strip()
    amount  = float(d.get('amount', 0))
    if not yticker or amount <= 0:
        return jsonify({'error': 'Ticker e importo richiesti'}), 400

    # Fetch price history for the new ETF
    try:
        history, currency = _fetch_history_yahoo(yticker)
        if currency != 'EUR':
            rate = _fx_rate(currency)
            history = [(dt, p * rate) for dt, p in history]
    except Exception as e:
        return jsonify({'error': f'Ticker non trovato o non disponibile: {e}'}), 404

    new_prices = dict(history)

    # Load existing ETF price histories (from cache / Yahoo)
    db = get_db()
    existing_tickers = [r['ticker'] for r in db.execute(
        "SELECT DISTINCT ticker FROM etf_info WHERE ticker != 'VWCE'"
    ).fetchall()]
    existing_prices = _load_prices(db, existing_tickers)

    # Correlations between new ETF and each existing one
    correlations  = {}
    corr_values   = []
    most_corr_tkr = None
    most_corr_val = -2.0

    for tkr, pd_ in existing_prices.items():
        common = sorted(set(new_prices.keys()) & set(pd_.keys()))
        if len(common) < 8:
            correlations[tkr] = None
            continue
        r1 = _log_returns([new_prices[dt] for dt in common])
        r2 = _log_returns([pd_[dt]        for dt in common])
        c  = _pearson(r1, r2)
        correlations[tkr] = c
        if c is not None:
            corr_values.append(c)
            if c > most_corr_val:
                most_corr_val = c
                most_corr_tkr = tkr

    avg_corr = round(sum(corr_values) / len(corr_values), 3) if corr_values else None

    # Current HHI
    holdings = db.execute(
        '''SELECT t.ticker, e.current_price_euro,
               SUM(CASE WHEN t.op_type='BUY' THEN t.quantity ELSE -t.quantity END) AS qty
           FROM transactions t LEFT JOIN etf_info e ON t.ticker=e.ticker
           WHERE t.ticker != 'VWCE' GROUP BY t.ticker HAVING qty > 0.00001'''
    ).fetchall()

    total_val = sum(r['qty'] * r['current_price_euro'] for r in holdings if r['current_price_euro'])
    new_total = total_val + amount

    current_hhi = 0.0
    if total_val > 0:
        for r in holdings:
            if r['current_price_euro']:
                w = r['qty'] * r['current_price_euro'] / total_val
                current_hhi += w * w
    current_hhi = round(current_hhi * 10000)

    simulated_hhi = 0.0
    if new_total > 0:
        for r in holdings:
            if r['current_price_euro']:
                w = r['qty'] * r['current_price_euro'] / new_total
                simulated_hhi += w * w
        simulated_hhi += (amount / new_total) ** 2
    simulated_hhi = round(simulated_hhi * 10000)

    return jsonify({
        'correlations':    correlations,
        'avg_correlation': avg_corr,
        'most_correlated': most_corr_tkr,
        'current_hhi':     current_hhi,
        'simulated_hhi':   simulated_hhi,
        'hhi_delta':       simulated_hhi - current_hhi,
        'data_points':     len(history),
        'currency':        currency,
    })


# ── Google Drive backup ───────────────────────────────────────────────────────

_drive_flows: dict = {}   # {state: flow} — keeps PKCE verifier alive until callback


@app.route('/api/drive/status', methods=['GET'])
def drive_status():
    if not os.path.exists('credentials.json'):
        return jsonify({'status': 'no_credentials'})
    try:
        import drive_utils
        creds = drive_utils.get_creds()
        return jsonify({'status': 'connected' if creds else 'not_connected'})
    except ImportError:
        return jsonify({'status': 'not_installed',
                        'message': 'Esegui: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client'})


@app.route('/api/drive/auth', methods=['GET'])
def drive_auth():
    import drive_utils
    auth_url, state, flow = drive_utils.get_auth_url()
    _drive_flows[state] = flow          # store flow to preserve PKCE verifier
    return jsonify({'auth_url': auth_url})


@app.route('/api/drive/callback')
def drive_callback():
    import drive_utils
    code  = request.args.get('code', '')
    state = request.args.get('state', '')
    error = request.args.get('error', '')
    if error:
        return f'<p>Accesso negato: {error}</p>', 400
    flow = _drive_flows.pop(state, None)
    if flow is None:
        return '<p>Sessione OAuth scaduta. Riprova dalla pagina Backup.</p>', 400
    try:
        drive_utils.exchange_code(flow, code)   # reuse same flow → PKCE intact
    except Exception as e:
        return f'<p>Errore durante l\'autenticazione: {e}</p>', 500
    return '''<!DOCTYPE html><html><body style="font-family:sans-serif;background:#0b0e17;color:#c8d0e0;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
    <div style="text-align:center">
      <div style="font-size:2rem;margin-bottom:.5rem">✓</div>
      <div style="font-size:1.1rem;font-weight:600;color:#34d399">Google Drive connesso</div>
      <div style="font-size:.85rem;color:#64748b;margin-top:.4rem">Puoi chiudere questa finestra</div>
    </div>
    <script>setTimeout(()=>window.close(),1500);</script>
    </body></html>'''


@app.route('/api/drive/revoke', methods=['POST'])
def drive_revoke():
    import drive_utils
    drive_utils.revoke_creds()
    return jsonify({'ok': True})


@app.route('/api/drive/backup', methods=['POST'])
def drive_backup():
    import drive_utils
    creds = drive_utils.get_creds()
    if not creds:
        return jsonify({'error': 'Non autenticato con Google Drive'}), 401
    try:
        service   = drive_utils.build_service(creds)
        folder_id = drive_utils.get_or_create_folder(service)
        file_info = drive_utils.upload_backup(service, folder_id, config.DB_PATH)
        return jsonify({'ok': True, 'file': file_info})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/drive/backups', methods=['GET'])
def drive_list_backups():
    import drive_utils
    creds = drive_utils.get_creds()
    if not creds:
        return jsonify({'error': 'Non autenticato'}), 401
    try:
        service   = drive_utils.build_service(creds)
        folder_id = drive_utils.get_or_create_folder(service)
        files     = drive_utils.list_backups(service, folder_id)
        return jsonify({'files': files})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/drive/restore', methods=['POST'])
def drive_restore():
    file_id = (request.json or {}).get('file_id')
    if not file_id:
        return jsonify({'error': 'file_id richiesto'}), 400
    import drive_utils
    creds = drive_utils.get_creds()
    if not creds:
        return jsonify({'error': 'Non autenticato'}), 401
    try:
        service = drive_utils.build_service(creds)
        data    = drive_utils.download_backup(service, file_id)
    except Exception as e:
        return jsonify({'error': f'Download fallito: {e}'}), 500

    db = get_db()
    try:
        db.execute('BEGIN')
        db.execute('DELETE FROM transactions')
        db.execute('DELETE FROM etf_info')
        db.execute('DELETE FROM geo_allocation')
        db.execute('DELETE FROM country_region')

        for r in data.get('transactions', []):
            db.execute(
                '''INSERT INTO transactions
                   (id,date,time,ticker,op_type,amount_euro,price_euro,fees_euro,quantity,isin,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (r.get('id'), r['date'], r.get('time',''), r['ticker'], r['op_type'],
                 r.get('amount_euro'), r.get('price_euro'), r.get('fees_euro'),
                 r.get('quantity'), r.get('isin',''), r.get('note',''))
            )
        for r in data.get('etf_info', []):
            db.execute(
                '''INSERT OR REPLACE INTO etf_info
                   (ticker,name,isin,type,currency,ter,current_price_euro,target_weight,last_price_update)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                (r.get('ticker'), r.get('name'), r.get('isin'), r.get('type'),
                 r.get('currency'), r.get('ter'), r.get('current_price_euro'),
                 r.get('target_weight'), r.get('last_price_update'))
            )
        for r in data.get('geo_allocation', []):
            db.execute('INSERT OR REPLACE INTO geo_allocation (ticker,country,etf_weight) VALUES (?,?,?)',
                       (r['ticker'], r['country'], r['etf_weight']))
        for r in data.get('country_region', []):
            db.execute('INSERT OR REPLACE INTO country_region (country,region) VALUES (?,?)',
                       (r['country'], r['region']))
        db.commit()
    except Exception as e:
        db.execute('ROLLBACK')
        return jsonify({'error': f'Ripristino fallito: {e}'}), 500

    return jsonify({
        'ok': True,
        'transactions': len(data.get('transactions', [])),
        'exported_at':  data.get('exported_at', ''),
    })


if __name__ == '__main__':
    app.run(debug=True, port=5001)
