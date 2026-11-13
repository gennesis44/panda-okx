import os
import time
import logging
import ccxt
import pandas as pd

# Configure Production Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("GSCSI-TradingCore-INJ")

class OKXINJProductionBot:
    def __init__(self):
        self.exchange = ccxt.okx({
            'apiKey': os.getenv('OKX_API_KEY', ''),
            'secret': os.getenv('OKX_SECRET_KEY', ''),
            'password': os.getenv('OKX_PASSWORD', ''),
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'} # Perpetual swap execution
        })
        self.timeframe = '1h'
        self.banned_asset = 'USDT'
        self.symbol = 'INJ/USDC:USDC'  # Target pair INJ settled in USDC (USDT strictly banned)
        
        # Risk Management Parameters
        self.take_profit_pct = 0.025     # 2.5% Take Profit Target
        self.stop_loss_pct = 0.015       # 1.5% Stop Loss Limit

    def fetch_market_data(self):
        try:
            # Strict safety check against USDT usage
            if self.banned_asset in self.symbol:
                raise ValueError("Security Violation: USDT is strictly banned in this trading core.")
                
            ohlcv = self.exchange.fetch_ohlcv(self.symbol, timeframe=self.timeframe, limit=50)
            if len(ohlcv) < 25:
                return None
            
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            return df
        except Exception as e:
            logger.error(f"Error fetching market data for {self.symbol}: {e}")
            return None

    def analyze_and_execute(self):
        df = self.fetch_market_data()
        if df is None:
            return

        # Calculate EMA 3 and EMA 21
        df['ema3'] = df['close'].ewm(span=3, adjust=False).mean()
        df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

        # Candle evaluation: Previous candles (-3, -2) and current forming candle (-1)
        prev_prev_ema3 = df['ema3'].iloc[-3]
        prev_prev_ema21 = df['ema21'].iloc[-3]
        
        prev_ema3 = df['ema3'].iloc[-2]
        prev_ema21 = df['ema21'].iloc[-2]
        
        curr_ema3 = df['ema3'].iloc[-1]
        curr_ema21 = df['ema21'].iloc[-1]

        current_price = df['close'].iloc[-1]

        # EMA 3/21 Crossover Logic
        bullish_cross = (prev_ema3 <= prev_ema21) and (curr_ema3 > curr_ema21)
        bearish_cross = (prev_ema3 >= prev_ema21) and (curr_ema3 < curr_ema21)

        if bullish_cross:
            logger.info(f"[BULLISH SIGNAL DETECTED] Pair: {self.symbol} | Price: {current_price}")
            self.execute_order(side='buy', entry_price=current_price)
            
        elif bearish_cross:
            logger.info(f"[BEARISH SIGNAL DETECTED] Pair: {self.symbol} | Price: {current_price}")
            self.execute_order(side='sell', entry_price=current_price)

    def execute_order(self, side, entry_price):
        try:
            if side == 'buy':
                take_profit_price = entry_price * (1 + self.take_profit_pct)
                stop_loss_price = entry_price * (1 - self.stop_loss_pct)
            else:
                take_profit_price = entry_price * (1 - self.take_profit_pct)
                stop_loss_price = entry_price * (1 + self.stop_loss_pct)

            logger.info(f"--- EXECUTING ORDER ({side.upper()}) ---")
            logger.info(f"Asset: {self.symbol} | Entry Price: {entry_price}")
            logger.info(f"Take Profit (+{self.take_profit_pct*100}%): {take_profit_price:.4f}")
            logger.info(f"Stop Loss (-{self.stop_loss_pct*100}%): {stop_loss_price:.4f}")

            # Integration point for broker / OKX API order execution wrapper
            
        except Exception as e:
            logger.error(f"Error during order execution and risk management calculation: {e}")

    def run(self):
        logger.info(f"Starting GSCSI engine on OKX [{self.symbol} | 1H] with Active Risk Management...")
        while True:
            self.analyze_and_execute()
            # 30-minute sleep cycle synchronized with 1H charting intervals
            time.sleep(1800)

if __name__ == "__main__":
    bot = OKXINJProductionBot()
    bot.run()
