import os
import sys
import ccxt
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def execute_strategy():
    api_key = os.getenv('OKX_API_KEY') or os.getenv('API_KEY') or os.getenv('OKX_KEY')
    secret_key = os.getenv('OKX_SECRET_KEY') or os.getenv('SECRET_KEY') or os.getenv('OKX_SECRET')
    password = os.getenv('OKX_PASSWORD') or os.getenv('PASSWORD') or os.getenv('PASSPHRASE') or os.getenv('OKX_PASSPHRASE')

    if not api_key or not secret_key or not password:
        print("Error: Faltan las credenciales de OKX en las variables de entorno.")
        print(f"Estado de lectura -> API_KEY: {bool(api_key)}, SECRET_KEY: {bool(secret_key)}, PASSWORD: {bool(password)}")
        sys.exit(1)

    exchange = ccxt.okx({
        'apiKey': api_key,
        'secret': secret_key,
        'password': password,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
        }
    })

    symbol = 'DOGE/USD:DOGE'
    timeframe = '15m'
    
    SL_PCT = 0.01   
    TP_PCT = 0.015  

    try:
        exchange.load_markets()
        
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        fast_period = 9
        slow_period = 21
        
        df['sma_fast'] = df['close'].rolling(window=fast_period).mean()
        df['sma_slow'] = df['close'].rolling(window=slow_period).mean()
        
        prev_fast = df['sma_fast'].iloc[-2]
        prev_slow = df['sma_slow'].iloc[-2]
        curr_fast = df['sma_fast'].iloc[-1]
        curr_slow = df['sma_slow'].iloc[-1]
        
        current_price = df['close'].iloc[-1]
        print(f"[{symbol}] Precio actual: {current_price} | MA Rápida: {curr_fast:.4f} | MA Lenta: {curr_slow:.4f}")

        positions = exchange.fetch_positions([symbol])
        active_position = None
        for pos in positions:
            if pos['symbol'] == symbol and float(pos['contracts']) > 0:
                active_position = pos
                break

        if prev_fast <= prev_slow and curr_fast > curr_slow:
            print("Señal detectada: Cruce Alcista (COMPRA)")
            if not active_position or active_position['side'] == 'short':
                amount = 1  
                sl_price = current_price * (1 - SL_PCT)
                tp_price = current_price * (1 + TP_PCT)
                params = {
                    'slTriggerPx': sl_price,
                    'tpTriggerPx': tp_price,
                }
                order = exchange.create_market_buy_order(symbol, amount, params)
                print(f"Orden LONG ejecutada con SL a {sl_price:.4f} y TP a {tp_price:.4f}: {order}")
            else:
                print("Ya existe una posición larga activa.")

        elif prev_fast >= prev_slow and curr_fast < curr_slow:
            print("Señal detectada: Cruce Bajista (VENTA)")
            if not active_position or active_position['side'] == 'long':
                amount = 1
                sl_price = current_price * (1 + SL_PCT)
                tp_price = current_price * (1 - TP_PCT)
                params = {
                    'slTriggerPx': sl_price,
                    'tpTriggerPx': tp_price,
                }
                order = exchange.create_market_sell_order(symbol, amount, params)
                print(f"Orden SHORT ejecutada con SL a {sl_price:.4f} y TP a {tp_price:.4f}: {order}")
            else:
                print("Ya existe una posición corta activa.")
        else:
            print("Sin cruce de medias móviles en este ciclo.")

    except Exception as e:
        print(f"Error crítico durante la ejecución del bot: {e}")
        sys.exit(1)

if __name__ == "__main__":
    execute_strategy()
