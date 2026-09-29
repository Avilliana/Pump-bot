# Tuning log

One line per change: date, setting, old -> new, reason, evidence.

- 2026-09-29 | min_buy_age_seconds: 0 -> 60 | Stop buying the launch spike | 33 trades, 22 hit the stop-loss (median hold ~2.5 min, several under 60 s); buys were at the 30 s check. Winners and losers had the same median score (65), so the score wasn't protecting entries.
- 2026-09-29 | BUG FIX (not tuning): dev launch buy was counted twice with the on-chain feed | Dev holdings read ~2x too high, so the "dev holds too much" filter rejected more launches than intended. Expect fewer "dev" rejections from here on.
- 2026-09-29 | NEW, shadow mode only (enforce_* = false): same-slot bundle buyers, identical-size launch buys, fresh wallets among top 5 holders, recycled Twitter accounts, Twitter links to famous accounts | Recorded in candidates.csv "shadow_flags"; switch one on only after the data shows flagged buys lose more than unflagged ones.
