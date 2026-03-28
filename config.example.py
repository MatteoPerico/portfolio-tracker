SECRET_KEY = 'change-this-to-a-long-random-string'
PASSWORD = 'your-password-here'   # password for the web app login
DB_PATH = 'portfolio.db'

# Yahoo Finance tickers for price updates (always EUR-denominated listings)
# Edit these if a ticker stops working (try .MI, .DE, .PA, .AS suffixes)
YAHOO_TICKERS = {
    'CSSPX': 'CSSPX.MI',
    'EXUS':  'EXUS.MI',
    'LCJP':  'LCJP.MI',
    'MEUD':  'MEUD.PA',
    'EIMI':  'EIMI.MI',
    'IWVL':  'IWVL.MI',
    'EM15':  'EM15.MI',
    'VECA':  'VECA.MI',
    'GGOV':  'GGOV.MI',
    'EM57':  'EM57.MI',
}
