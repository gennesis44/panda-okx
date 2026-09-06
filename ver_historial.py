import os
import ccxt

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': 'https://my.okx.com'}},
})

try:
    # Ajusta el símbolo al que usaste en DOGE (por ejemplo, 'DOGE/USD:USD')
    symbol = 'DOGE/USD:USD' 
    trades = exchange.fetch_my_trades(symbol, limit=10)
    print(f"--- HISTORIAL DE OPERACIONES: {symbol} ---")
    for t in trades:
        pnl = t.get('info', {}).get('pnl', 'No disponible')
        print(f"Fecha: {t['datetime']} | Lado: {t['side']} | Precio: {t['price']} | PnL: {pnl}")
except Exception as e:
    print(f"Error consultando el historial: {e}")

