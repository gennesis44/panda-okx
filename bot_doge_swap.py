import os
import ccxt
import pandas as pd

exchange = ccxt.okx({
    'apiKey': os.getenv('OKX_API_KEY'),
    'secret': os.getenv('OKX_SECRET_KEY'),
    'password': os.getenv('OKX_PASSWORD'),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

symbol = 'DOGE/USD:DOGE'
timeframe = '15m'

def run_bot():
    print(f"Conectando a OKX para analizar {symbol} en el marco temporal de {timeframe}...")
    
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
        
        prev_ema9 = df['ema9'].iloc[-2]
        prev_ema21 = df['ema21'].iloc[-2]
        curr_ema9 = df['ema9'].iloc[-1]
        curr_ema21 = df['ema21'].iloc[-1]
        
        current_price = df['close'].iloc[-1]
        print(f"Precio actual: {current_price} | EMA9: {curr_ema9:.5f} | EMA21: {curr_ema21:.5f}")
        
        if prev_ema9 <= prev_ema21 and curr_ema9 > curr_ema21:
            print("¡Cruce alcista (Golden Cross) detectado!")
            sl_price = current_price * 0.99
            tp_price = current_price * 1.015
            print(f"Parámetros calculados -> SL: {sl_price:.5f} | TP: {tp_price:.5f}")
        elif prev_ema9 >= prev_ema21 and curr_ema9 < curr_ema21:
            print("Cruce bajista (Death Cross) detectado. Sin entradas en largo.")
        else:
            print("Sin cruce nuevo en esta ejecución. Esperando siguiente ciclo.")
            
    except Exception as e:
        print(f"Error crítico durante la ejecución del bot: {e}")
        raise e

if __name__ == "__main__":
    run_bot()
