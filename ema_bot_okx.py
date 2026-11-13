#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GSCSI - Bot EMA 3/21 4H OKX
Colaborador y Protector del Operador

ADVERTENCIA CRÍTICA:
- Este script puede perder dinero real.
- Por defecto usa SANDBOX/DEMO.
- Nunca habilites permisos de retiro en la API.
- Prueba siempre primero en demo.
- "Entrada ya mismo" fuerza orden de mercado sin esperar cruce.
- USDT vetado → Solo USD / USDC
"""

import os
import sys
import time
import yaml
from datetime import datetime, timezone

try:
    import ccxt
except ImportError:
    print("ERROR: Instala ccxt →  pip install ccxt pyyaml")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("ERROR: Instala pandas →  pip install pandas")
    sys.exit(1)


def load_config(path="config.yml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def create_exchange(cfg):
    ex_cfg = cfg["exchange"]
    exchange_class = getattr(ccxt, ex_cfg["id"])
    params = {
        "apiKey": ex_cfg["apiKey"],
        "secret": ex_cfg["secret"],
        "password": ex_cfg["password"],
        "enableRateLimit": True,
        "options": {
            "defaultType": cfg.get("default_type", "spot"),
        },
    }
    exchange = exchange_class(params)
    if ex_cfg.get("sandbox", True):
        exchange.set_sandbox_mode(True)
        print("[PROTECTOR] Modo SANDBOX/DEMO activado. No se usa dinero real.")
    else:
        print("[ALERTA] ⚠️  MODO REAL ACTIVADO. Riesgo de pérdida real.")
    return exchange


def fetch_ohlcv(exchange, symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def calculate_ema(df, period):
    return df["close"].ewm(span=period, adjust=False).mean()


def get_signal(df, ema_fast, ema_slow):
    """Devuelve 'long', 'short' o None según cruce EMA."""
    if len(df) < ema_slow + 2:
        return None
    df = df.copy()
    df["ema_f"] = calculate_ema(df, ema_fast)
    df["ema_s"] = calculate_ema(df, ema_slow)
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if prev["ema_f"] <= prev["ema_s"] and curr["ema_f"] > curr["ema_s"]:
        return "long"
    if prev["ema_f"] >= prev["ema_s"] and curr["ema_f"] < curr["ema_s"]:
        return "short"
    return None


def place_market_order_with_sl_tp(exchange, cfg, side):
    """Coloca orden de mercado + SL/TP."""
    symbol = cfg["symbol"]
    amount_usdc = float(cfg.get("position_size_usdc", 50))
    sl_pct = float(cfg["stop_loss_pct"])
    tp_pct = float(cfg["take_profit_pct"])
    td_mode = cfg.get("td_mode", "cash")

    ticker = exchange.fetch_ticker(symbol)
    price = ticker["last"]
    if price is None or price <= 0:
        raise ValueError("No se pudo obtener precio actual")

    amount = amount_usdc / price
    amount = float(exchange.amount_to_precision(symbol, amount))

    print(f"[INFO] Precio actual: {price}")
    print(f"[INFO] Cantidad calculada: {amount} ({amount_usdc} USDC)")

    order_params = {}
    if td_mode != "cash":
        order_params["tdMode"] = td_mode

    print(f"[EJECUTANDO] Orden de mercado {side.upper()} {amount} {symbol} ...")
    order = exchange.create_order(
        symbol=symbol,
        type="market",
        side=side,
        amount=amount,
        params=order_params,
    )
    print(f"[OK] Orden principal colocada: {order.get('id')}")

    if side == "buy":
        sl_price = price * (1 - sl_pct)
        tp_price = price * (1 + tp_pct)
    else:
        sl_price = price * (1 + sl_pct)
        tp_price = price * (1 - tp_pct)

    sl_price = float(exchange.price_to_precision(symbol, sl_price))
    tp_price = float(exchange.price_to_precision(symbol, tp_price))

    print(f"[PROTECTOR] SL = {sl_price} ({sl_pct*100}%) | TP = {tp_price} ({tp_pct*100}%)")
    print("[AVISO] Si el exchange no adjunta SL/TP automáticamente,")
    print("        colócalos manualmente en la interfaz de OKX.")
    print(f"        SL trigger ≈ {sl_price} | TP trigger ≈ {tp_price}")

    return order


def main():
    print("=" * 60)
    print("GSCSI · EMA 3/21 4H · OKX · Protector del Operador")
    print("USDT vetado → Solo USD / USDC")
    print("=" * 60)

    cfg = load_config("config.yml")

    if not cfg["exchange"].get("sandbox", True):
        confirm = input("⚠️  MODO REAL. Escribe 'SI QUIERO PERDER DINERO' para continuar: ")
        if confirm.strip() != "SI QUIERO PERDER DINERO":
            print("Abortado por seguridad. Usa sandbox=true.")
            return

    exchange = create_exchange(cfg)
    symbol = cfg["symbol"]
    timeframe = cfg["timeframe"]

    print(f"Símbolo: {symbol} | TF: {timeframe}")
    print(f"EMA {cfg['ema_fast']}/{cfg['ema_slow']} | SL {cfg['stop_loss_pct']*100}% | TP {cfg['take_profit_pct']*100}%")
    print(f"Leverage: {cfg.get('leverage', 1)}x | Force entry: {cfg.get('force_entry_now')}")

    exchange.load_markets()

    if cfg.get("force_entry_now", False):
        side = cfg.get("side", "buy").lower()
        print(f"\n[ENTRADA YA MISMO] Forzando orden de mercado {side.upper()}...")
        try:
            place_market_order_with_sl_tp(exchange, cfg, side)
            print("\n[PROTECTOR] Orden enviada. Vigila la posición y coloca SL/TP si no se adjuntaron.")
        except Exception as e:
            print(f"[ERROR] Falló la entrada: {e}")
            print("Revisa API keys, permisos de Trade, saldo en USDC y símbolo.")
        return

    print("\nEsperando cruce EMA... (Ctrl+C para salir)")
    while True:
        try:
            df = fetch_ohlcv(exchange, symbol, timeframe, limit=50)
            signal = get_signal(df, cfg["ema_fast"], cfg["ema_slow"])
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            last_close = df["close"].iloc[-1]
            print(f"[{now}] Close={last_close:.4f} | Señal={signal or 'ninguna'}")

            if signal:
                side = "buy" if signal == "long" else "sell"
                print(f"[SEÑAL] {signal.upper()} detectada. Ejecutando...")
                place_market_order_with_sl_tp(exchange, cfg, side)
                print("Orden ejecutada. Saliendo para evitar re-entrada inmediata.")
                break

            time.sleep(cfg.get("check_interval_seconds", 300))
        except KeyboardInterrupt:
            print("\nDetenido por el operador.")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
