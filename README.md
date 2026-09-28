# pump.fun paper-trading bot (v1)

Watches every new pump.fun launch live, throws out the obvious traps, scores the
rest, and trades **fake SOL**. It has no wallet and no private key, so it cannot
spend real money.

## Setup (Windows, one time)

1. Install Python 3.11+ from python.org. On the first installer screen, tick
   **"Add python.exe to PATH"**.
2. Put this folder somewhere, like `C:\pumpbot`.
3. Open **Command Prompt** in that folder (type `cmd` in the File Explorer address bar).
4. Run:
   ```
   pip install -r requirements.txt
   ```

## Run

```
python bot.py --sim      (fake offline market - just proves everything works)
python bot.py            (LIVE pump.fun data, paper money)
```

Stop it with **Ctrl + C**. Leave it running for hours or days; the longer it
runs, the more the results mean.

### Optional: on-chain holder check
A free Helius account (helius.dev) gives an API key. With it, before every paper
buy the bot checks the real top-10 holders on chain, which catches bundle wallets
the live feed misses.
```
set HELIUS_API_KEY=your-key-here
python bot.py
```

## What it does with each launch

1. **Watches** it for up to 4 minutes (checks at 30s, 60s, 2m, 4m).
2. **Rejects** it if any of these hit:
   - Dev sold, or dev holds more than 8%
   - Bundled: 4+ wallets bought in the first 2 seconds and own 12%+, or 3+ SOL
     went in during the launch block before anyone could see it
   - Top 10 wallets own more than 35%
   - No traction (fewer than 3 buyers after a minute)
   - Market cap already above $60k (too late)
3. **Scores** survivors out of 100: unique buyers (25), buy/sell pressure (20),
   net SOL flowing in (15), market cap momentum (15), socials (15), narrative
   match (10). It buys at **55+** once there are 12+ buyers and $6k+ market cap.
4. **Paper-buys** 10% of the balance (max 0.5 SOL). Fills use the real bonding
   curve math plus fees and a latency haircut, so results aren't flattering.
5. **Exits** on whichever comes first:
   - +100%: sells half, then rides the rest with a 30% trailing stop
   - -35% stop loss
   - Dev sells
   - 20 minutes held
   - 3 minutes with no trades
   - Token migrates off the curve

## Reading results (the `logs` folder)

- `summary.json`: the scorecard, with equity, return, win rate, best and worst
  trade, and why tokens got rejected.
- `trades.csv`: every paper buy and sell with P&L.
- `candidates.csv`: every token it judged, with all its numbers and a pump.fun
  link. **This is the file that makes the bot better.** Open it in Excel, sort by
  what happened to those tokens, and see which numbers separated winners from
  rugs. Then change `config.json`.

## Cloud shifts (GitHub Actions)

`.github/workflows/paper-run.yml` runs the bot every hour for 20 minutes
(starting at :07 past the hour) and commits `logs/` back
to the repo. The paper account carries over between shifts (`logs/state.json`).
No new buys in the last 3 minutes of a shift; anything still open at the end is
closed at market. To run one by hand: Actions, then Paper trading shift, then Run workflow.

## Tuning

- `config.json` holds every threshold. Change one thing at a time and give it a
  day, or you won't know what helped.
- `narratives.txt` is the "Twitter" dial. Words you add above the AUTO line stay
  forever; the AUTO section is refreshed every morning from DexScreener and
  CoinGecko trending data by `update_narratives.py`. Matching tokens get +10.

## Honest limits of v1

- It does not read Twitter directly. The X API is paid; trending data from
  DexScreener and CoinGecko stands in for it. The X API is paid; that's a v2 decision.
- Bundle detection is a heuristic built from on-chain trades and top holders.
- Paper fills are an estimate. Real trades land later and can fill worse.
- The `--sim` market is made up. Its profits mean nothing. Only live paper
  results count.
- Only consider real money after **weeks** of live paper trading that stay
  profitable after fees. Even then, use a separate burner wallet.
