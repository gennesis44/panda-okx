import os
import ccxt
import pandas as pd

exchange_public = ccxt.okx({
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

symbol = 'DOGE/USD:DOGE'
timeframe = '15m'
amount = 136  # Tus 136 contratos de DOGE

def run_bot():
    print(f"Analizando {symbol} en {timeframe}...")
    
    try:
        ohlcv = exchange_public.fetch_ohlcv(symbol, timeframe, limit=100)
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
            print("¡Golden Cross detectado! Lanzando orden de ataque...")
            
            exchange_trade = ccxt.okx({
                'apiKey': os.getenv('OKX_API_KEY'),
                'secret': os.getenv('OKX_SECRET_KEY'),
                'password': os.getenv('OKX_PASSWORD'),
                'enableRateLimit': True,
                'options': {'defaultType': 'swap'}
            })
            
            # 1. Ejecutar compra a mercado
            order = exchange_trade.create_order(symbol, 'market', 'buy', amount)
            print(f"¡Posición abierta! ID: {order['id']}")
            
            entry_price = order['average'] if 'average' in order and order['average'] else current_price
            sl_price = entry_price * 0.99
            tp_price = entry_price * 1.015
            
            print(f"Objetivos fijados -> Entrada: {entry_price} | SL: {sl_price:.5f} | TP: {tp_price:.5f}")
            
            # 2. Anclar Stop-Loss condicional en OKX
            exchange_trade.create_order(
                symbol=symbol,
                type='conditional',
                side='sell',
                amount=amount,
                price=sl_price,
                params={
                    'triggerPrice': sl_price,
                    'stopLoss': True
                }
            )
            print("Stop-Loss anclado en el exchange.")

            # 3. Anclar Take-Profit condicional en OKX
            exchange_trade.create_order(
                symbol=symbol,
                type='conditional',
                side='sell',
                amount=amount,
                price=tp_price,
                params={
                    'triggerPrice': tp_price,
                    'takeProfit': True
                }
            )
            print("Take-Profit anclado en el exchange. Ciclo de riesgo completo.")
            
        elif prev_ema9 >= prev_ema21 and curr_ema9 < curr_ema21:
            print("Death Cross detectado. Margen en observación.")
        else:
            print("Sin cruces nuevos en este ciclo. Vigilando.")
            
    except Exception as e:
        print(f"Error crítico durante la ejecución: {e}")
        raise e

if __name__ == "__main__":
    run_bot()
