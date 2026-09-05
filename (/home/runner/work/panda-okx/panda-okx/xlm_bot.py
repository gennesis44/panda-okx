import os
import sys
import time
import logging
import ccxt
import pandas as pd
import pandas_ta as ta

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Lectura de credenciales desde las variables de entorno del sistema / GitHub Secrets
API_KEY = os.getenv('OKX_API_KEY', '')
SECRET_KEY = os.getenv('OKX_SECRET_KEY', '')
PASSWORD = os.getenv('OKX_PASSWORD', '')

# Indicadores de entorno para CI/CD (GitHub Actions)
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE', '0') == '1'
TEST_MODE = os.getenv('TEST_MODE', '0') == '1'

# Inicialización de OKX (Ajustado a USDC-SWAP para EEE y modo sandbox si TEST_MODE está activo)
exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

if TEST_MODE:
    exchange.set_sandbox_mode(True)
    logging.info("Modo de prueba (Sandbox/Testnet) activado.")

SYMBOL = 'XLM/USDC:USDC'
SL_PCT = 0.01   # 1% Stop Loss
TP_PCT = 0.015  # 1.5% Take Profit

def fetch_data(symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def calculate_indicators(df):
    df['EMA_7'] = ta.ema(df['close'], length=7)
    df['EMA_21'] = ta.ema(df['close'], length=21)
    df['RSI_21'] = ta.rsi(df['close'], length=21)
    
    macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df['MACD'] = macd['MACD_12_26_9']
    df['MACD_signal'] = macd['MACDs_12_26_9']
    df['MACD_hist'] = macd['MACDh_12_26_9']
    return df

def get_open_position(symbol):
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            if pos['symbol'] == symbol and float(pos['contracts']) > 0:
                return pos
    except Exception as e:
        logging.error(f"Error consultando posiciones: {e}")
    return None

def evaluate_multi_timeframe():
    try:
        df_5m = calculate_indicators(fetch_data(SYMBOL, '5m', limit=100))
        df_15m = calculate_indicators(fetch_data(SYMBOL, '15m', limit=100))
        df_1h = calculate_indicators(fetch_data(SYMBOL, '1h', limit=100))
        df_4h = calculate_indicators(fetch_data(SYMBOL, '4h', limit=100))
        
        latest_1h = df_1h.iloc[-2]
        current_price = df_1h.iloc[-1]['close']
        
        ema_7_1h = latest_1h['EMA_7']
        ema_21_1h = latest_1h['EMA_21']
        rsi_1h = latest_1h['RSI_21']
        macd_hist_1h = latest_1h['MACD_hist']
        
        trend_4h = df_4h.iloc[-2]['EMA_7'] > df_4h.iloc[-2]['EMA_21']
        
        logging.info(f"Precio: {current_price} | 1H EMA7: {ema_7_1h:.4f} | 1H EMA21: {ema_21_1h:.4f} | 1H RSI(21): {rsi_1h:.2f} | 1H MACD Hist: {macd_hist_1h:.4f}")
        
        is_long = (ema_7_1h > ema_21_1h) and (macd_hist_1h > 0) and (45 < rsi_1h < 75) and trend_4h
        is_short = (ema_7_1h < ema_21_1h) and (macd_hist_1h < 0) and (25 < rsi_1h < 55) and (not trend_4h)
        
        if is_long:
            return 'LONG'
        elif is_short:
            return 'SHORT'
            
    except Exception as e:
        logging.error(f"Error en evaluación multi-temporalidad: {e}")
    return None

def execute_order(side, price):
    try:
        amount = 10  # Tamaño del contrato
        if side == 'LONG':
            sl_price = price * (1 - SL_PCT)
            tp_price = price * (1 + TP_PCT)
            order = exchange.create_market_buy_order(SYMBOL, amount)
            logging.info(f"LONG ejecutado: {order.get('id')}")
            exchange.create_order(SYMBOL, 'stop_market', 'sell', amount, params={'stopPrice': sl_price, 'reduceOnly': True})
            exchange.create_order(SYMBOL, 'take_profit_market', 'sell', amount, params={'stopPrice': tp_price, 'reduceOnly': True})
            
        elif side == 'SHORT':
            sl_price = price * (1 + SL_PCT)
            tp_price = price * (1 - TP_PCT)
            order = exchange.create_market_sell_order(SYMBOL, amount)
            logging.info(f"SHORT ejecutado: {order.get('id')}")
            exchange.create_order(SYMBOL, 'stop_market', 'buy', amount, params={'stopPrice': sl_price, 'reduceOnly': True})
            exchange.create_order(SYMBOL, 'take_profit_market', 'buy', amount, params={'stopPrice': tp_price, 'reduceOnly': True})
            
    except Exception as e:
        logging.error(f"Error ejecutando orden o rangos SL/TP: {e}")

def main():
    logging.info("Iniciando ejecución para XLM-USDC en OKX...")
    
    while True:
        try:
            position = get_open_position(SYMBOL)
            if position:
                logging.info(f"Posición activa detectada ({position['side']} - {position['contracts']} contratos).")
            else:
                logging.info("Ventana de observación de 2 minutos...")
                time.sleep(120 if not SINGLE_CYCLE else 1) # Acorta espera en tests unitarios si se desea
                
                signal = evaluate_multi_timeframe()
                if signal:
                    ticker = exchange.fetch_ticker(SYMBOL)
                    current_price = ticker['last']
                    logging.info(f"Señal confirmada: {signal}. Abriendo posición...")
                    execute_order(signal, current_price)
                else:
                    logging.info("Sin señales claras en este ciclo.")
            
            if SINGLE_CYCLE:
                logging.info("SINGLE_CYCLE activo. Finalizando ejecución de prueba.")
                sys.exit(0)
                
            time.sleep(480) # Resto del ciclo de 10 minutos
            
        except Exception as e:
            logging.error(f"Error en el ciclo principal: {e}")
            if SINGLE_CYCLE:
                sys.exit(1)
            time.sleep(60)

if __name__ == "__main__":
    main()
