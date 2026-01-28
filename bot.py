import asyncio, websockets, json, telegram, requests, logging, sys
import pandas as pd
import pandas_ta as ta

# ==================== CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD, EMA_RSI_PERIOD = 14, 9
RSI_MAX_ENTRY = 60 

TELEGRAM_TOKEN = '8392707199:AAHjWHGLoZ3Udm4rS5JlgSaPLez1qZbHMOo'
CHAT_ID = '1950462171'

# Multi-State Tracking
stats = {
    "balance": 100, 
    "total_trades": 0,
    "wins_final": 0,    # Hit 6.0R
    "wins_trailed": 0,  # Closed in profit via trail
    "losses": 0         # Closed in negative
}

active_trade = None  
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ==================== DATA ENGINE ====================

async def fetch_indicators():
    """Fetches 1m data and calculates RSI signals"""
    try:
        url = "https://api.binance.com/api/v3/klines"
        # Only fetching 1m data now
        r1m = requests.get(url, params={'symbol': SYMBOL, 'interval': '1m', 'limit': 50}).json()
        df1 = pd.DataFrame(r1m, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df1[['close', 'low']] = df1[['c', 'l']].astype(float)
        
        # Calculations
        rsi = ta.rsi(df1['close'], length=RSI_PERIOD)
        rsi_ema = ta.ema(rsi, length=EMA_RSI_PERIOD)
        
        return {
            "rsi": rsi.iloc[-1], "rsi_ema": rsi_ema.iloc[-1],
            "prsi": rsi.iloc[-2], "pema": rsi_ema.iloc[-2],
            "prev_low": df1['low'].iloc[-2],
            "curr_price": df1['close'].iloc[-1]
        }
    except Exception as e:
        print(f"Error fetching data: {e}")
        return None

# ==================== TRADE & STATS ENGINE ====================

async def monitor_trade(price):
    global active_trade
    if not active_trade: return

    risk = active_trade['entry'] - active_trade['initial_sl']
    rr = (price - active_trade['entry']) / risk if risk > 0 else 0

    # 6-STAGE TRAILING
    new_sl = None
    if rr >= 6.0: await close_trade(price, "🎯 6.0R TARGET")
    elif rr >= 5.0 and not active_trade.get('st5'): new_sl, active_trade['st5'] = active_trade['entry'] + (risk * 4.0), True
    elif rr >= 4.0 and not active_trade.get('st4'): new_sl, active_trade['st4'] = active_trade['entry'] + (risk * 3.0), True
    elif rr >= 3.0 and not active_trade.get('st3'): new_sl, active_trade['st3'] = active_trade['entry'] + (risk * 1.2), True
    elif rr >= 2.2 and not active_trade.get('st2'): new_sl, active_trade['st2'] = active_trade['entry'] + (risk * 0.8), True
    elif rr >= 1.5 and not active_trade.get('st1'): new_sl, active_trade['st1'] = active_trade['entry'] + (risk * 0.5), True

    if new_sl: 
        active_trade['sl'] = new_sl
        # Optional: Add notification for trail update here
        
    if price <= active_trade['sl']: 
        await close_trade(price, "🛡️ SL/TRAIL HIT")

async def close_trade(exit_price, reason):
    global active_trade, stats
    net_rr = (exit_price - active_trade['entry']) / (active_trade['entry'] - active_trade['initial_sl'])
    usd_pnl = active_trade['risk_usd'] * net_rr
    
    # Track exact state
    if "6.0R" in reason: stats['wins_final'] += 1
    elif net_rr > 0: stats['wins_trailed'] += 1
    else: stats['losses'] += 1
    
    stats['balance'] += usd_pnl
    stats['total_trades'] += 1
    
    msg = (f"🏁 *TRADE CLOSED*\n"
           f"━━━━━━━━━━━━━━\n"
           f"💵 PnL: `{usd_pnl:+.2f} USDT`\n"
           f"📈 Total Wins: `{stats['wins_final']}`\n"
           f"🛡️ Trail Wins: `{stats['wins_trailed']}`\n"
           f"🛑 Losses: `{stats['losses']}`\n"
           f"🏦 Balance: `${stats['balance']:.2f}`")
    await bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
    active_trade = None

async def main():
    global active_trade
    print("Bot started. Monitoring 1m data...")
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    async with websockets.connect(uri) as ws:
        while True:
            data = json.loads(await ws.recv())
            price = float(data['k']['c'])
            if active_trade: await monitor_trade(price)
            
            if data['k']['x']: # Candle Close
                ind = await fetch_indicators()
                if ind and not active_trade:
                    # Logic: Cross + RSI Filter (EMA Filter Removed)
                    if ind['prsi'] <= ind['pema'] and ind['rsi'] > ind['rsi_ema'] \
                       and ind['rsi'] < RSI_MAX_ENTRY:
                        
                        active_trade = {
                            'entry': price, 
                            'initial_sl': ind['prev_low'], 
                            'sl': ind['prev_low'], 
                            'risk_usd': stats['balance'] * 0.02
                        }
                        await bot.send_message(CHAT_ID, f"🚀 *LONG @ {price}*\nRSI: `{ind['rsi']:.1f}`")

asyncio.run(main())
