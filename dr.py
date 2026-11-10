#!/usr/bin/env python3
import os
import random
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionNotFound

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")


BASE_CHAIN_ID = 8453
MIN_INCOMING_ETH = "0.0001"       
GAS_REFRESH_SECONDS = 3.0        
GAS_STALE_SECONDS = 15.0          
MIN_CHECK_INTERVAL = 0.05         
REPORT_SECONDS = 2.0              
RPC_TIMEOUT = 10                  
PENDING_POLL_SECONDS = 0.5        
RECEIPT_WARN_SECONDS = 120        
DROP_CHECK_AFTER = 30             
DROP_CONFIRM_SECONDS = 15         
GUARD_SECONDS = 10                
L1_FEE_MARGIN_PERCENT = 150       
L1_FEE_FALLBACK_ETH = "0.00002"   
MAX_BACKOFF = 2.0                 
ERROR_REPEAT_SECONDS = 60.0       
MAX_NONCE_EXTRA = 10              

GAS_PRICE_ORACLE_ADDRESS = "0x420000000000000000000000000000000000000F"  
GAS_PRICE_ORACLE_ABI = [{
    "inputs": [{"internalType": "bytes", "name": "_data", "type": "bytes"}],
    "name": "getL1Fee",
    "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
    "stateMutability": "view", "type": "function",
}]
ZERO_ADDRESSES = {"0x" + "0" * 40, "0x" + "0" * 39 + "1", "0x000000000000000000000000000000000000dEaD"}


_SECRETS = []


def register_secret(value):
    value = (value or "").strip()
    if len(value) >= 6 and value not in _SECRETS:
        _SECRETS.append(value)


def register_url_secrets(url: str):
    register_secret(url)
    try:
        p = urlparse(url)
        register_secret(p.password)
        for seg in p.path.split("/"):
            register_secret(seg)
        for _, v in parse_qsl(p.query):
            register_secret(v)
    except Exception:
        pass


def sanitize(text) -> str:
    text = str(text)
    for s in _SECRETS:
        text = text.replace(s, "***")
    return text


_file_lock = threading.Lock()
_err_lock = threading.Lock()
_err_state = {"text": None, "last": 0.0, "suppressed": 0}


def log_dir() -> Path:
    raw = (os.getenv("LOG_DIR") or "").strip().strip('"').strip("'")
    path = Path(raw) if raw else Path("logs")
    return path if path.is_absolute() else BASE_DIR / path


def file_log(level: str, msg: str):
    now = datetime.now()
    line = f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d} | {level:<5} | {sanitize(msg)}\n"
    try:
        with _file_lock:
            directory = log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / f"forwarder_{now:%Y-%m-%d}.log", "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] WARN cannot write log file: {sanitize(short(e))}", flush=True)


def file_error(msg: str):
    msg = sanitize(msg)
    with _err_lock:
        st, now = _err_state, time.monotonic()
        if msg == st["text"]:
            if now - st["last"] < ERROR_REPEAT_SECONDS:
                st["suppressed"] += 1
                return
            extra = f" [+{st['suppressed']} identical errors suppressed]" if st["suppressed"] else ""
            st["last"], st["suppressed"] = now, 0
            file_log("ERROR", msg + extra)
            return
        if st["suppressed"]:
            file_log("ERROR", f"previous error repeated {st['suppressed']} more times (suppressed)")
        st["text"], st["last"], st["suppressed"] = msg, now, 0
        file_log("ERROR", msg)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {sanitize(msg)}", flush=True)


def event_error(msg: str):
    log(f"ERROR {msg}")
    file_log("ERROR", msg)


def fatal(msg: str):
    event_error(f"FATAL {msg}")
    sys.exit(1)


def short(exc, limit: int = 90) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def env_decimal(name: str, default: str) -> Decimal:
    raw = (os.getenv(name) or "").strip().replace(",", ".").rstrip("%").strip() or default
    try:
        value = Decimal(raw)
    except InvalidOperation:
        fatal(f"{name}={raw!r} in .env is not a number")
    if not value.is_finite():
        fatal(f"{name}={raw!r} in .env is not a finite number")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(n in msg for n in ("429", "rate limit", "rate-limit", "too many requests", "throttl"))


class Forwarder:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

        pk = (os.getenv("PRIVATE_KEY") or "").strip().strip('"').strip("'")
        if not pk:
            fatal("PRIVATE_KEY is empty. Create .env next to the script (copy .env.example)")
        register_secret(pk)
        if not pk.startswith("0x"):
            pk = "0x" + pk
        register_secret(pk)
        register_secret(pk[2:])
        try:
            self.account = Account.from_key(pk)
        except Exception:
            fatal("PRIVATE_KEY in .env is not a valid private key")
        self.address = self.account.address

        recipient_raw = (os.getenv("RECIPIENT_ADDRESS") or "").strip()
        if not Web3.is_address(recipient_raw):
            fatal("RECIPIENT_ADDRESS in .env is missing or invalid")
        self.recipient = Web3.to_checksum_address(recipient_raw)
        if self.recipient == self.address:
            fatal("RECIPIENT_ADDRESS equals the monitored address")
        if self.recipient.lower() in {a.lower() for a in ZERO_ADDRESSES}:
            fatal("RECIPIENT_ADDRESS is a burn/zero address, funds would be lost")

        self.rpc_url = (os.getenv("BASE_RPC") or "").strip()
        if not self.rpc_url:
            fatal("BASE_RPC is empty in .env")
        register_url_secrets(self.rpc_url)

        interval = float(env_decimal("CHECK_INTERVAL", "0.1"))
        if interval < MIN_CHECK_INTERVAL:
            log(f"WARN CHECK_INTERVAL {interval} is too low, using {MIN_CHECK_INTERVAL}")
            interval = MIN_CHECK_INTERVAL
        self.check_interval = interval

        keep_pct = env_decimal("KEEP_PERCENT", "1")
        if not (Decimal(0) <= keep_pct < Decimal(100)):
            fatal("KEEP_PERCENT must be >= 0 and < 100")
        premium_pct = env_decimal("GAS_PREMIUM_PERCENT", "20")
        if premium_pct < 0:
            fatal("GAS_PREMIUM_PERCENT must be >= 0")
        self.keep_pct, self.premium_pct = keep_pct, premium_pct
        self.keep_ppm = int(keep_pct * 10000)
        self.premium_bp = int(premium_pct * 100)
        self.sweep_existing = env_bool("SWEEP_EXISTING_ON_START", False)
        self.balance_block = (os.getenv("BALANCE_BLOCK") or "latest").strip().lower()
        if self.balance_block not in ("latest", "pending"):
            fatal("BALANCE_BLOCK must be latest or pending")

        nonce_extra = env_decimal("NONCE_EXTRA", "0")
        if nonce_extra != nonce_extra.to_integral_value() or not (0 <= nonce_extra <= MAX_NONCE_EXTRA):
            fatal(f"NONCE_EXTRA must be a whole number from 0 to {MAX_NONCE_EXTRA}")
        self.nonce_extra = int(nonce_extra)

        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": RPC_TIMEOUT}))
        try:
            connected = self.w3.is_connected()
        except Exception:
            connected = False
        if not connected:
            fatal("cannot connect to BASE_RPC, check the URL and internet")
        self.chain_id = self.w3.eth.chain_id
        if self.chain_id != BASE_CHAIN_ID:
            fatal(f"RPC is chain {self.chain_id}, expected BASE {BASE_CHAIN_ID}")

        self.min_incoming_wei = self.w3.to_wei(MIN_INCOMING_ETH, "ether")
        self.l1_fallback_wei = self.w3.to_wei(L1_FEE_FALLBACK_ETH, "ether")
        self.oracle = self.w3.eth.contract(
            address=Web3.to_checksum_address(GAS_PRICE_ORACLE_ADDRESS), abi=GAS_PRICE_ORACLE_ABI)
        self.gas_limit = self._detect_gas_limit()

        self.stop_event = threading.Event()
        self.fees = None
        self.gas_at = 0.0
        self.nonce = None
        self.nonce_ver = 0
        self.skip_extra = False
        self._nonce_lock = threading.Lock()
        self.pending = None
        self.guard = None
        self._last_dust = 0
        self._gas_warned = False
        self._l1_warned = False
        self.win = self._new_window()
        self.win_end = time.monotonic() + REPORT_SECONDS

        self.baseline_wei = 0
        if not self.sweep_existing:
            try:
                self.baseline_wei = self.w3.eth.get_balance(self.address)
            except Exception as e:
                fatal(f"cannot read start balance: {short(e)}")
        self.refresh_gas()
        try:
            self._refresh_nonce()
        except Exception:
            pass

    def _fmt(self, wei) -> str:
        return "n/a" if wei is None else f"{self.w3.from_wei(wei, 'ether'):.8f} ETH"

    @staticmethod
    def _new_window() -> dict:
        return {"checks": 0, "balance": None, "incoming": None, "stage": None,
                "sent": None, "errors": 0, "last_error": ""}

    def _detect_gas_limit(self) -> int:
        try:
            code = self.w3.eth.get_code(self.recipient)
        except Exception:
            return 21000
        if len(code) == 0:
            return 21000
        try:
            limit = int(self.w3.eth.estimate_gas(
                {"from": self.address, "to": self.recipient, "value": 1}) * 1.3)
        except Exception:
            limit = 100_000
        log(f"WARN recipient is a contract, gas limit {limit}")
        return limit

    def _build_tx(self, value: int, gas_price: int, nonce: int) -> dict:
        return {"nonce": nonce, "to": self.recipient, "value": value, "gas": self.gas_limit,
                "gasPrice": gas_price, "chainId": self.chain_id}

    def _tx_known(self, tx_hash) -> bool:
        try:
            self.w3.eth.get_transaction(tx_hash)
            return True
        except Exception:
            return False

    @staticmethod
    def _receipt_fee(receipt):
        try:
            fee = int(receipt["gasUsed"]) * int(receipt["effectiveGasPrice"])
        except Exception:
            return None
        raw = receipt.get("l1Fee")
        try:
            if raw is not None:
                fee += int(raw, 16) if isinstance(raw, str) else int(raw)
        except Exception:
            pass
        return fee

    def refresh_gas(self):
        try:
            base_gas = self.w3.eth.gas_price
            gas_price = max(base_gas * (10_000 + self.premium_bp) // 10_000, base_gas)
            self._gas_warned = False
        except Exception as e:
            if not self._gas_warned:
                self._gas_warned = True
                log(f"WARN gas price request failed: {short(e)}")
                file_error(f"gas price request failed: {type(e).__name__}: {short(e, 300)}")
            return
        try:
            probe = self.account.sign_transaction(self._build_tx(10 ** 21, gas_price, 10 ** 6))
            fee = self.oracle.functions.getL1Fee(bytes(probe.raw_transaction)).call()
            l1_fee = int(fee) * L1_FEE_MARGIN_PERCENT // 100
            self._l1_warned = False
        except Exception:
            l1_fee = self.l1_fallback_wei
            if not self._l1_warned:
                self._l1_warned = True
                log(f"WARN L1 fee oracle unavailable, reserve {self._fmt(self.l1_fallback_wei)}")
        self.fees = (gas_price, l1_fee)
        self.gas_at = time.monotonic()

    def _reset_nonce(self):
        with self._nonce_lock:
            self.nonce = None
            self.nonce_ver += 1
        self.skip_extra = True

    def _set_nonce(self, value: int):
        with self._nonce_lock:
            self.nonce = value
            self.nonce_ver += 1

    def _refresh_nonce(self):
        if self.pending is not None:
            return
        with self._nonce_lock:
            ver = self.nonce_ver
        n = int(self.w3.eth.get_transaction_count(self.address, "pending"))
        with self._nonce_lock:
            if self.nonce_ver == ver and self.pending is None:
                self.nonce = n if self.nonce is None else max(self.nonce, n)

    def _next_nonce(self) -> int:
        with self._nonce_lock:
            nonce = self.nonce
        if nonce is None:
            nonce = int(self.w3.eth.get_transaction_count(self.address, "pending"))
        return nonce

    def _background(self):
        while not self.stop_event.wait(GAS_REFRESH_SECONDS):
            try:
                self.refresh_gas()
                if not self.nonce_extra:
                    self._refresh_nonce()
            except Exception:
                pass

    def tick(self):
        w = self.win
        now = time.monotonic()

        if self.pending is not None:
            self._poll_pending(now)
            return

        balance = self.w3.eth.get_balance(self.address, self.balance_block)
        w["checks"] += 1
        w["balance"] = balance

        if self.guard is not None:
            if now >= self.guard["until"] or balance != self.guard["balance"]:
                self.guard = None
            else:
                w["stage"] = "syncing after send"
                return

        if balance < self.baseline_wei:
            self.baseline_wei = balance
        incoming = balance - self.baseline_wei

        if incoming <= self.min_incoming_wei:
            if incoming > 0 and incoming != self._last_dust:
                self._last_dust = incoming
                w["incoming"] = incoming
                w["stage"] = f"below minimum {MIN_INCOMING_ETH} ETH, ignored"
            elif incoming == 0:
                self._last_dust = 0
            return

        w["incoming"] = incoming
        fees = self.fees
        if fees is None or now - self.gas_at > GAS_STALE_SECONDS:
            self.refresh_gas()
            fees = self.fees
            if fees is None:
                raise RuntimeError("gas price unavailable")
        gas_price, l1_fee = fees

        gas_cost = self.gas_limit * gas_price
        value = incoming - incoming * self.keep_ppm // 1_000_000
        value = min(value, balance - gas_cost - l1_fee)
        if value <= 0:
            w["stage"] = "not enough to cover fees, skipped"
            return
        if self.dry_run:
            w["stage"] = f"dry run, would send {self._fmt(value)}"
            return

        w["stage"] = "sending"
        nonce = self._next_nonce() + (0 if self.skip_extra else self.nonce_extra)
        signed = self.account.sign_transaction(self._build_tx(value, gas_price, nonce))
        try:
            self.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception:
            if not self._tx_known(signed.hash):
                self._reset_nonce()
                raise
        self._set_nonce(nonce + 1)
        self.skip_extra = False
        self.baseline_wei = balance - value
        tx_hex = Web3.to_hex(signed.hash)
        self.pending = {"hash": signed.hash, "hex": tx_hex, "nonce": nonce, "value": value,
                        "incoming": incoming, "balance_before": balance, "t0": now,
                        "next_poll": now + PENDING_POLL_SECONDS, "missing_since": None,
                        "warned": False}
        w["stage"] = f"sent, waiting confirmation | tx {tx_hex}"

    def _poll_pending(self, now: float):
        p, w = self.pending, self.win
        age = now - p["t0"]
        w["stage"] = f"PENDING | tx {p['hex']} | {int(age)}s"
        if now < p["next_poll"]:
            return
        p["next_poll"] = now + PENDING_POLL_SECONDS

        try:
            receipt = self.w3.eth.get_transaction_receipt(p["hash"])
        except TransactionNotFound:
            receipt = None
        if receipt is not None:
            self._finish(p, receipt)
            return

        if age >= DROP_CHECK_AFTER:
            try:
                self.w3.eth.get_transaction(p["hash"])
                p["missing_since"] = None
            except TransactionNotFound:
                if p["missing_since"] is None:
                    p["missing_since"] = now
                elif now - p["missing_since"] >= DROP_CONFIRM_SECONDS:
                    self.pending = None
                    self._reset_nonce()
                    event_error(f"tx DROPPED from mempool, will retry: tx {p['hex']}")
                    return
        if age >= RECEIPT_WARN_SECONDS and not p["warned"]:
            p["warned"] = True
            gap = (f" | NONCE_EXTRA={self.nonce_extra} may be too high (nonce gap), "
                   f"check nonce {p['nonce']} and restart" if self.nonce_extra else "")
            event_error(f"tx NOT CONFIRMED in {RECEIPT_WARN_SECONDS}s, still waiting: tx {p['hex']}{gap}")

    def _finish(self, p: dict, receipt):
        w = self.win
        self.pending = None
        self.guard = {"balance": p["balance_before"], "until": time.monotonic() + GUARD_SECONDS}
        block = receipt["blockNumber"]
        w["incoming"] = p["incoming"]
        if receipt.get("status") == 1:
            fee = self._receipt_fee(receipt)
            w["sent"] = {"value": p["value"], "tx": p["hex"], "result": f"CONFIRMED block {block}"}
            file_log("OK", f"SENT {self._fmt(p['value'])} to {self.recipient} | tx {p['hex']} | "
                           f"nonce {p['nonce']} | block {block} | fee {self._fmt(fee)} | https://basescan.org/tx/{p['hex']}")
        else:
            self.baseline_wei = p["balance_before"]
            w["sent"] = {"value": p["value"], "tx": p["hex"], "result": "REVERTED"}
            file_log("ERROR", f"tx REVERTED, funds NOT forwarded, no retry | tx {p['hex']} | "
                              f"block {block} | https://basescan.org/tx/{p['hex']}")

    def report(self):
        w, self.win = self.win, self._new_window()
        if w["sent"]:
            s = w["sent"]
            text = f"INCOMING {self._fmt(w['incoming'])} | SENT {self._fmt(s['value'])} | {s['result']} | tx {s['tx']}"
        elif w["incoming"] is not None:
            text = f"INCOMING {self._fmt(w['incoming'])} | {w['stage']}"
        elif w["stage"]:
            text = w["stage"]
        else:
            text = f"WAITING | checks {w['checks']} | balance {self._fmt(w['balance'])}"
        if w["errors"]:
            text += f" | ERROR x{w['errors']}: {w['last_error']}"
        log(text)

    def run(self):
        log("BASE FORWARDER" + (" [DRY RUN]" if self.dry_run else ""))
        log(f"Watch     {self.address}")
        log(f"Send to   {self.recipient}")
        log(f"Interval {int(self.check_interval * 1000)} ms | trigger > {MIN_INCOMING_ETH} ETH | "
            f"keep {self.keep_pct}% | gas +{self.premium_pct}%")
        log(f"Existing balance at start: {'will be forwarded' if self.sweep_existing else 'ignored'} | "
            f"log dir {log_dir()}")
        log(f"Balance read at block tag: {self.balance_block} | nonce extra: +{self.nonce_extra}"
            + (" (nonce read once at start, counted locally)" if self.nonce_extra else ""))
        log("Ctrl+C to stop")

        threading.Thread(target=self._background, name="gas-nonce", daemon=True).start()
        errors = 0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.tick()
                errors = 0
                wait = max(0.0, self.check_interval - (time.monotonic() - started))
            except Exception as e:
                errors = min(errors + 1, 10)
                self.win["errors"] += 1
                self.win["last_error"] = short(e)
                file_error(f"{type(e).__name__}: {short(e, 300)}")
                wait = min(self.check_interval * 2 ** errors, MAX_BACKOFF)
                if is_rate_limit_error(e):
                    wait *= random.uniform(0.7, 1.3)
            if time.monotonic() >= self.win_end:
                self.report()
                self.win_end = time.monotonic() + REPORT_SECONDS
            self.stop_event.wait(wait)

        if self.pending is not None:
            event_error(f"INTERRUPTED while a transaction was unconfirmed, check it manually: "
                        f"tx {self.pending['hex']} | https://basescan.org/tx/{self.pending['hex']}")
        log("Stopped")


def acquire_instance_lock(address: str):
    port = 40000 + int(address[-6:], 16) % 20000
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        fatal(f"another copy for {address} seems to be running (local port {port} is busy)")
    return sock


def main():
    forwarder = Forwarder(dry_run="--dry-run" in sys.argv)
    lock = None if forwarder.dry_run else acquire_instance_lock(forwarder.address)

    def _stop(signum, frame):
        forwarder.stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)
    try:
        forwarder.run()
    except Exception as e:
        event_error(f"CRASH {type(e).__name__}: {short(e, 300)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
