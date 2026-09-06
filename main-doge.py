# main-doge.py
import os
import logging
import ccxt
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Configurado para MyOKX (my.okx.com) y par DOGE/USD
SYMBOL = 'DOGE/USD:USD'
LEVERAGE = 1
SL_PCT = 0.01
TP_PCT = 0.015
MIN_CONTRACTS = 100.0

def calculate_indicators(df):
    df['ema7'] = df['close'].ewm(span=7, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=21).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=21).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    return df

def run_bot():
    # Usamos ccxt.myokx para apuntar automáticamente a my.okx.com (EEA)
    exchange = ccxt.myokx({
        'apiKey': os.environ.get('OKX_API_KEY'),
        'secret': os.environ.get('OKX_API_SECRET') or os.environ.get('OKX_SECRET_KEY'),
        'password': os.environ.get('OKX_PASSPHRASE') or os.environ.get('OKX_PASSWORD'),
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })

    balance = exchange.fetch_balance()
    logging.info("Autenticacion OK en my.okx.com | Cuenta unificada / Margen Multidivisa conectado.")

    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        logging.warning(f"Apalancamiento: {e}")

    ohlcv_1h = exchange.fetch_ohlcv(SYMBOL, timeframe='1h', limit=50)
    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_1h = calculate_indicators(df_1h)

    ohlcv_4h = exchange.fetch_ohlcv(SYMBOL, timeframe='4h', limit=30)
    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_4h = calculate_indicators(df_4h)

    current_price = df_1h['close'].iloc[-2]
    curr_7 = df_1h['ema7'].iloc[-2]
    curr_21 = df_1h['ema21'].iloc[-2]
    rsi_1h = df_1h['rsi'].iloc[-2]
    macdh = df_1h['macd_hist'].iloc[-2]
    h4_alcista = df_4h['ema7'].iloc[-2] > df_4h['ema21'].iloc[-2]

    logging.info(f"DOGE/USD - Precio: {current_price} | 1H EMA7/21: {curr_7:.5f}/{curr_21:.5f} | RSI: {rsi_1h:.1f} | MACDh: {macdh:.5f} | 4H alcista: {h4_alcista}")

    positions = exchange.fetch_positions([SYMBOL])
    active_pos = [p for p in positions if float(p['contracts']) > 0]

    if not active_pos:
        if curr_7 > curr_21 and 30 < rsi_1h < 80 and h4_alcista:
            logging.info("Senal LONG confirmada para DOGE/USD. Ejecutando...")
            amount = MIN_CONTRACTS
            sl_price = current_price * (1 - SL_PCT)
            tp_price = current_price * (1 + TP_PCT)

            order = exchange.create_order(SYMBOL, 'market', 'buy', amount)
            logging.info(f"Orden market ejecutada: {order}")

            exchange.create_order(SYMBOL, 'stop_market', 'sell', amount, None, {'stopPrice': sl_price, 'reduceOnly': True})
            exchange.create_order(SYMBOL, 'take_profit_market', 'sell', amount, None, {'stopPrice': tp_price, 'reduceOnly': True})
            logging.info(f"SL: {sl_price:.5f} | TP: {tp_price:.5f}")
        else:
            logging.info("Sin condiciones de entrada LONG validas para DOGE/USD.")
    else:
        logging.info("Posicion activa de DOGE/USD detectada.")

if __name__ == '__main__':
    run_bot()
