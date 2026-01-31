import asyncio
import websockets
import json
import telegram
import httpx
import pandas as pd
import pandas_ta as ta
import logging
import sys

# ==================== LOGGING SETUP ====================
class RailwayJSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        })

logger = logging.getLogger("BotEngine")
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(RailwayJSONFormatter())
logger.addHandler(console_handler)

# ==================== CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 20    # Updated per request
WMA_PERIOD = 13    # Updated per request

TELEGRAM_TOKEN = '8349229275:AAGNWV2A0_Pf9LhlwZCczeBoMcUaJL2shFg'
CHAT_ID = '1950462171'

stats = {
    "balance": 59.91, 
    "risk_percent": 0.02,
    "total_trades": 93,
    "wins_final_target": 9,   
    "wins_trailed": 21,        
    "losses": 63                
}

active_trade = None  
http_client = httpx.AsyncClient()

# ==================== DATA ENGINE ====================

async def fetch_indicators():
    """Calculates RSI(20) and its WMA(13) signal line."""
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': '5m', 'limit': 200}
        resp = await http_client.get(url, params=params, timeout=10)
        df = pd.DataFrame(resp.json(), columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df['close'] = df['c'].astype(float)
        
        # Calculate RSI 20
        rsi = ta.rsi(df['close'], length=RSI_PERIOD)
        # Calculate WMA 13 of that RSI
        wma = ta.wma(rsi, length=WMA_PERIOD)
        
        if rsi is None or wma is None: return None, None, None, None
        return rsi.iloc[-1], wma.iloc[-1], rsi.iloc[-2], wma.iloc[-2]
    except Exception as e:
        logger.error(f"FETCH_ERROR: {str(e)}")
        return None, None, None, None

# ==================== TRADE MANAGEMENT ====================

async def monitor_trade(price, bot):
    global active_trade
    if not active_trade: return

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    reward_dist = price - active_trade['entry']
    rr_ratio = reward_dist / risk_dist if risk_dist > 0 else 0

    # STAGE 0: Hit 1.0R -> Trail SL to -0.3R (Logic Fix: This is still a Loss)
    if not active_trade.get('stage0_hit') and rr_ratio >= 1.0 and not active_trade.get('stage1_hit'):
        active_trade['sl'] = active_trade['entry'] - (risk_dist * 0.3)
        active_trade['stage0_hit'] = True
        msg = (f"🛡️ *STAGE 0 REACHED (1.0R)*\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"📉 Risk reduced! SL moved to -0.3R: `${active_trade['sl']:.2f}`")
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')

    # STAGE 1: Hit 1.5R -> Trail SL to +0.8R (Now a Win)
    elif not active_trade.get('stage1_hit') and rr_ratio >= 1.5:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.8)
        active_trade['stage1_hit'] = True
        msg = (f"⚡ *STAGE 1 REACHED (1.5R)*\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"🛡️ SL moved to +0.8R: `${active_trade['sl']:.2f}`")
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')

    # STAGE 2: Hit 2.2R -> Trail SL to +1.3R
    elif not active_trade.get('stage2_hit') and rr_ratio >= 2.2:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 1.3)
        active_trade['stage2_hit'] = True
        msg = (f"🚀 *STAGE 2 REACHED (2.2R)*\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"🛡️ SL moved to +1.3R: `${active_trade['sl']:.2f}`")
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='Markdown')

    # STAGE 3: Final Target 3.0R
    if rr_ratio >= 3.0:
        await close_trade(price, "🎯 FINAL TARGET (3.0R)", bot)
    
    # EXIT ON STOP LOSS
    elif price <= active_trade['sl']:
        reason = "🛡️ TRAILED SL HIT" if active_trade.get('stage0_hit') else "🛑 INITIAL SL HIT"
        await close_trade(price, reason, bot)

async def close_trade(exit_price, reason, bot):
    global active_trade, stats
    risk_usd = active_trade['risk_usd']
    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    reward_dist = exit_price - active_trade['entry']
    actual_rr = reward_dist / risk_dist
    pnl = risk_usd * actual_rr
    
    # --- Correct Stat Tracking ---
    if "FINAL TARGET" in reason:
        stats['wins_final_target'] += 1
    elif "TRAILED" in reason and pnl > 0:
        stats['wins_trailed'] += 1
    else:
        stats['losses'] += 1 # Stage 0 hits (-0.3R) are correctly counted as losses
    
    stats['balance'] += pnl
    stats['total_trades'] += 1
    win_rate = ((stats['wins_final_target'] + stats['wins_trailed']) / stats['total_trades']) * 100
    
    exit_msg = (
        f"🏁 *TRADE CLOSED: {reason}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 *PnL:* `{pnl:+.2f} USDT` ({actual_rr:.2f}R)\n"
        f"🏦 *New Balance:* `${stats['balance']:.2f}`\n\n"
        f"📊 *Lifetime Stats:*\n"
        f"🎯 Final Targets: `{stats['wins_final_target']}`\n"
        f"🛡️ Trailed Wins: `{stats['wins_trailed']}`\n"
        f"🛑 Total Losses: `{stats['losses']}`\n"
        f"📈 Win Rate: `{win_rate:.1f}%`"
    )
    await bot.send_message(chat_id=CHAT_ID, text=exit_msg, parse_mode='Markdown')
    active_trade = None

# ==================== MAIN EXECUTION ====================

async def main():
    global active_trade
    logger.info("SYSTEM_BOOT: Bot Online.")
    
    async with telegram.Bot(token=TELEGRAM_TOKEN) as bot:
        uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
        
        while True:
            try:
                async with websockets.connect(uri) as ws:
                    while True:
                        data = json.loads(await ws.recv())
                        if 'k' in data:
                            price = float(data['k']['c'])
                            if active_trade: await monitor_trade(price, bot)
                            
                            if data['k']['x']: # Candle Close
                                rsi, wma, prsi, pwma = await fetch_indicators()
                                
                                # SIGNAL LOGIC: RSI(20) crosses ABOVE WMA(13)
                                if rsi and not active_trade:
                                    if prsi <= pwma and rsi > wma:
                                        # Fetch 5m candle for SL
                                        api_res = await http_client.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=5m&limit=1")
                                        low_price = float(api_res.json()[0][3]) * 0.9995 
                                        
                                        active_trade = {
                                            'entry': price, 'initial_sl': low_price, 'sl': low_price,
                                            'risk_usd': stats['balance'] * stats['risk_percent'], 
                                            'stage0_hit': False, 'stage1_hit': False, 'stage2_hit': False
                                        }
                                        
                                        entry_msg = (f"🚀 *LONG SIGNAL: {SYMBOL}*\n"
                                                     f"━━━━━━━━━━━━━━━━━━\n"
                                                     f"💰 *Entry:* `${price:.2f}`\n"
                                                     f"🛑 *Stop:* `${low_price:.2f}`\n"
                                                     f"📉 *RSI-WMA Cross:* `{rsi:.2f} > {wma:.2f}`")
                                        await bot.send_message(chat_id=CHAT_ID, text=entry_msg, parse_mode='Markdown')

            except Exception as e:
                logger.error(f"RECONNECTING: {str(e)}")
                await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
