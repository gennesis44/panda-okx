# main-doge.py
import os
import time
import logging
import ccxt
import pandas as pd

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ==================== CONFIGURACIÓN ====================
SYMBOL      = 'DOGE/USDT:USDT'  # USDT-margined perpetual (compatible con saldo EUR/USDT)
AMOUNT      = 1.0               # 1 contrato
SL_PCT      = 0.010             # 1.0%
TP_PCT      = 0.015             # 1.5%
TD_MODE     = 'cross'
HOST        = 'https://my.okx.com'     # Instancia EEA real

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY') or os.getenv('OKX_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY') or os.getenv('OKX_API_SECRET', ''),
    'password':  os.getenv('OKX_PASSWORD') or os.getenv('OKX_PASSPHRASE', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== INDICADORES ====================
def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()

def rsi(s: pd.Series, length: int = 21) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean()
    rs = gain / loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))

def macd(s: pd.Series, fast=12, slow=26, signal=9):
    line = ema(s, fast) - ema(s, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig

def fetch_data(symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

def calculate_indicators(df):
    df['EMA_7']  = ema(df['close'], 7)
    df['EMA_21'] = ema(df['close'], 21)
    df['RSI_21'] = rsi(df['close'], 21)
    df['MACD'], df['MACD_signal'], df['MACD_hist'] = macd(df['close'])
    return df

# ==================== POSICIÓN Y EJECUCIÓN ====================
def get_open_position(symbol):
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get('symbol') == symbol and float(pos.get('contracts') or 0) > 0:
                return pos
    except Exception as e:
        log.error(f"Error consultando posiciones: {e}")
    return None

def close_position(symbol):
    pos = get_open_position(symbol)
    if not pos:
        return
    side, contracts = pos['side'], pos['contracts']
    amount = exchange.amount_to_precision(symbol, contracts)
    close_side = 'sell' if side == 'long' else 'buy'
    try:
        exchange.create_order(symbol, 'market', close_side, amount,
                              params={'tdMode': TD_MODE, 'reduceOnly': True})
        log.warning(f"Posición {side} cerrada.")
    except Exception as e:
        log.error(f"Fallo cerrando posición: {e}")

def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def execute_order(side: str, symbol: str, ref_price: float, amount: float):
    market = exchange.market(symbol)
    if side == 'LONG':
        oside, sl_raw, tp_raw = 'buy', ref_price * (1 - SL_PCT), ref_price * (1 + TP_PCT)
    else:
        oside, sl_raw, tp_raw = 'sell', ref_price * (1 + SL_PCT), ref_price * (1 - TP_PCT)

    sl = exchange.price_to_precision(symbol, sl_raw)
    tp = exchange.price_to_precision(symbol, tp_raw)
    sz = exchange.amount_to_precision(symbol, amount)

    req = {
        'instId': market['id'],
        'tdMode': TD_MODE,
        'side': oside,
        'ordType': 'optimal_limit_ioc',
        'sz': sz,
        'attachAlgoOrds': [{
            'tpTriggerPx': tp, 'tpOrdPx': '-1',
            'slTriggerPx': sl, 'slOrdPx': '-1',
        }],
    }
    resp = _post_trade_order(req)
    data = resp.get('data') or []
    d0 = data[0] if isinstance(data, list) and data else {}
    if str(d0.get('sCode', resp.get('code', '1'))) != '0':
        raise ccxt.ExchangeError(f"OKX rechazó la entrada: {d0.get('sMsg') or resp.get('msg')}")
    log.info(f"DOGE {side} ejecutada con SL/TP atómicos. ordId: {d0.get('ordId')} | SL: {sl} | TP: {tp}")
    return True

# ==================== LÓGICA PRINCIPAL ====================
def run_bot():
    bal = exchange.fetch_balance()
    log.info(f"Autenticacion OK en my.okx.com | Balance general conectado.")

    symbol = SYMBOL
    exchange.load_markets()
    log.info(f"Instrumento seleccionado: {symbol}")

    try:
        exchange.set_leverage(1, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning(f"Apalancamiento: {e}")

    df_1h = calculate_indicators(fetch_data(symbol, '1h', limit=100))
    df_4h = calculate_indicators(fetch_data(symbol, '4h', limit=100))

    curr_7 = df_1h['EMA_7'].iloc[-2]
    curr_21 = df_1h['EMA_21'].iloc[-2]
    rsi_1h = df_1h['RSI_21'].iloc[-2]
    macdh = df_1h['MACD_hist'].iloc[-2]
    price = exchange.fetch_ticker(symbol).get('last') or df_1h['close'].iloc[-1]
    trend_4h = df_4h['EMA_7'].iloc[-2] > df_4h['EMA_21'].iloc[-2]

    log.info(f"DOGE - Precio: {price} | 1H EMA7/21: {curr_7:.5f}/{curr_21:.5f} | RSI: {rsi_1h:.1f} | MACDh: {macdh:.5f} | 4H alcista: {trend_4h}")

    pos = get_open_position(symbol)
    if not pos:
        if curr_7 > curr_21 and 30 < rsi_1h < 80 and trend_4h:
            log.info("Señal LONG confirmada para DOGE. Ejecutando entrada atómica de 1 contrato...")
            execute_order('LONG', symbol, price, AMOUNT)
        else:
            log.info("Sin condiciones de entrada LONG válidas para DOGE.")
    else:
        log.info("Posición activa de DOGE detectada. Esperando gestión de SL/TP.")

if __name__ == '__main__':
    run_bot()
