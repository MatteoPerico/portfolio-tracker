from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3
from datetime import datetime
from functools import wraps
import urllib.request
import json as _json
import math as _math
import config

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def get_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == config.PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Password errata.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
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

    return jsonify({
        'etfs': result,
        'total_value': round(total_value, 2),
        'total_cost': round(total_cost, 2),
        'total_pnl': total_pnl,
        'total_pnl_pct': total_pnl_pct,
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


if __name__ == '__main__':
    app.run(debug=True, port=5001)
