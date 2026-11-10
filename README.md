# Simple On-chain Forwarder

Simple On-chain Forwarder watches a wallet on the **Base** network and automatically forwards any new ETH it receives to a wallet of your choice. It works with regular transfers and with payouts from smart contracts alike.

The script is intentionally written in Python for flexibility and easy adaptation to a server-side stack. It has been tested only on the Base network with an Alchemy RPC.

## Use case

In simple cases, the forwarder can help recover ETH that is staked in smart contracts and is paid out to an address whose private key has been compromised by an attacker. Base has no public mempool, so the script does not scan for incoming pending transactions. Instead, it polls the address balance with minimal delay and moves incoming ETH to your safe wallet the moment it arrives, ahead of the attacker where possible.

## How it works

The script checks the balance of the watched address 10 times per second by default (up to 20). When the balance increases by more than 0.0001 ETH, the difference is treated as new ETH and sent to the recipient address, optionally leaving a small percentage behind. Network fees are taken from the forwarded amount, so the address does not need extra ETH for gas. Once the transaction is confirmed, the result is written to a log file.

## Quick start

You need Python 3 and a Base RPC URL (for example, from Alchemy).

```
pip install -r requirements.txt
copy .env.example .env
```

Fill in the `.env` file:

```
PRIVATE_KEY=your_private_key
RECIPIENT_ADDRESS=0xYourMainWallet
BASE_RPC=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY
```

Run a test without sending anything, then start for real:

```
python dr.py --dry-run
python dr.py
```

Stop the script with `Ctrl+C`.

## Settings

All options are set in `.env`.

| Setting | Description |
|---|---|
| `SWEEP_EXISTING_ON_START` | `false` (default) forwards only ETH that arrives after start; `true` also forwards the current balance |
| `KEEP_PERCENT` | Share of each deposit left on the address; default `1` keeps 1%, `0` keeps nothing |
| `GAS_PREMIUM_PERCENT` | Extra gas paid for faster inclusion; default `20` pays 20% more |
| `CHECK_INTERVAL` | Seconds between balance checks; default `0.1`, minimum `0.05` |
| `BALANCE_BLOCK` | Block used to read the balance: `latest` (default) or `pending` |
| `NONCE_EXTRA` | Nonce offset from `0` to `10` for contract payouts (see below) |
| `LOG_DIR` | Folder for log files (default `logs`) |

## Contract payouts and nonce

When the watched address calls a contract that pays ETH back, the address sends its own transaction just before the payout arrives. `NONCE_EXTRA` lets the forwarder fit its transaction right after it.

- `0` (default): the nonce is refreshed from the network every few seconds.
- `1` or more: the nonce is read once at start and then counted locally. Each forward uses the counter plus this number, so it follows your own transactions. The address should make exactly that many transactions before each payout.

If a send fails, the script fetches the real nonce from the network and retries.

## Logs

Each day gets its own file in the log folder, such as `forwarder_2026-11-10.log`. It records every confirmed forward with the amount, transaction link, and fee, along with any errors. The console shows a short status line every couple of seconds.

## No guarantees

This script does not guarantee a successful rescue. If the attacker runs their own bot on the same address, the outcome depends on gas priority, network latency, and timing.

For more severe cases, I recommend the atomic approach: the payout and the forward happen in one transaction, so there is no gap for an attacker. It works only if the payout contract allows it. For batch transactions with several execution scenarios, including forwarding, I recommend a custom setup.

Base produces a block every 200 ms, so checking more than 5 times per second gives no extra benefit. What matters beyond that is latency to the RPC: a nearby endpoint or your own node helps more than extra requests. The script caps the rate at 20 checks per second (a constant in the code, sized for typical RPC plan limits), and your RPC provider plan may limit it further.
