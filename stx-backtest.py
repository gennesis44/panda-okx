import ccxt
import pandas as pd
import numpy as np
import sys

# CONFIGURACIÓN DEL BUQUE STX (AX2)
SYMBOL = 'STX/USD:USD'  # OKX Perpetuo USD (settlement USD/USDC, veto USDT)
TIMEFRAME = '5m'
EMA_FAST = 4
EMA_SLOW = 22
SL_PCT = 0.03   # 3% Stop Loss
TP_PCT = 0.04   # 4% Take Profit
MAX_CONTRACTS = 4
MAX_EXPOSURE_USD = 9.50

def fetch_data():
    exchange = ccxt.okx({'enableRateLimit': True})
    # Descargar suficientes velas de 5m para cubrir las 3 ventanas de test
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=1500)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def run_backtest(df):
    # Cálculo de EMAs sobre velas cerradas
    df['ema_fast'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
    
    # Señal de cruce (vela anterior y actual para detectar el cruce exacto)
    df['signal'] = 0
    # Long: fast cruza por encima de slow
    cond_long = (df['ema_fast'].shift(1) <= df['ema_slow'].shift(1)) & (df['ema_fast'] > df['ema_slow'])
    # Short: fast cruza por debajo de slow
    cond_short = (df['ema_fast'].shift(1) >= df['ema_slow'].shift(1)) & (df['ema_fast'] < df['ema_slow'])
    
    df.loc[cond_long, 'signal'] = 1
    df.loc[cond_short, 'signal'] = -1

    # División en 3 Ventanas Deslizantes (Estándar de Flota)
    total_rows = len(df)
    chunk = total_rows // 3
    windows = {
        'V1 (Antigua)': df.iloc[0:chunk].copy(),
        'V2 (Media)': df.iloc[chunk:chunk*2].copy(),
        'V3 (Reciente)': df.iloc[chunk*2:].copy()
    }

    results = {}
    for name, w_df in windows.items():
        res = simulate_trades(w_df)
        results[name] = res
        
    return results

def simulate_trades(df):
    trades = []
    in_position = False
    entry_price = 0
    side = 0
    contracts = 1
    
    for i in range(1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i-1]
        price = row['close']
        
        # Dimensión de contratos según precio actual (máx 4 contratos, máx $9.50 exposición)
        if price > 0:
            calc_contracts = int(MAX_EXPOSURE_USD / price)
            contracts = max(1, min(MAX_CONTRACTS, calc_contracts))
        
        # Gestión de posición abierta (SL / TP atómico)
        if in_position:
            if side == 1: # LONG
                hit_tp = row['high'] >= entry_price * (1 + TP_PCT)
                hit_sl = row['low'] <= entry_price * (1 - SL_PCT)
                if hit_tp and hit_sl:
                    # Conflicto en misma vela: conservador asumimos SL
                    pnl_pct = -SL_PCT
                    in_position = False
                elif hit_tp:
                    pnl_pct = TP_PCT
                    in_position = False
                elif hit_sl:
                    pnl_pct = -SL_PCT
                    in_position = False
            elif side == -1: # SHORT
                hit_tp = row['low'] <= entry_price * (1 - TP_PCT)
                hit_sl = row['high'] >= entry_price * (1 + SL_PCT)
                if hit_tp and hit_sl:
                    pnl_pct = -SL_PCT
                    in_position = False
                elif hit_tp:
                    pnl_pct = TP_PCT
                    in_position = False
                elif hit_sl:
                    pnl_pct = -SL_PCT
                    in_position = False
            
            if not in_position:
                trades.append(pnl_pct * 100)
                
        # Regla de posición única: solo buscar entrada si NO hay posición activa
        if not in_position:
            sig = prev_row['signal']
            if sig != 0:
                in_position = True
                side = sig
                entry_price = price
                
    # Métricas de salida
    total_trades = len(trades)
    if total_trades == 0:
        return {'trades': 0, 'win_rate': 0.0, 'return_pct': 0.0}
    
    wins = [t for t in trades if t > 0]
    wr = (len(wins) / total_trades) * 100
    ret_pct = sum(trades)
    
    return {
        'trades': total_trades,
        'win_rate': round(wr, 2),
        'return_pct': round(ret_pct, 2)
    }

if __name__ == '__main__':
    try:
        df_data = fetch_data()
        window_results = run_backtest(df_data)
        
        print("=== RESULTADOS BACKTEST STX (AX2) ===")
        all_positive = True
        for w_name, metrics in window_results.items():
            print(f"{w_name} -> Trades: {metrics['trades']} | WR: {metrics['win_rate']}% | Retorno: {metrics['return_pct']}%")
            if metrics['return_pct'] <= 0:
                all_positive = False
                
        print("-------------------------------------")
        if all_positive:
            print("VEREDICTO: APTO PARA FLOTA (Positivo en 3 ventanas)")
        else:
            print("VEREDICTO: RECHAZADO / REGIMEN-DEPENDIENTE (No pasa las 3 ventanas)")
    except Exception as e:
        print(f"ERROR EN EJECUCIÓN: {str(e)}")
        sys.exit(1)
