"""Run a safe Polymarket CLOB credential smoke test."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _load_env_file(path: Path, overwrite: bool = False) -> None:
    """Merge key=value pairs from path into process environment."""
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and (overwrite or key not in os.environ):
            os.environ[key] = val


def _load_runtime_env() -> None:
    """Load runtime env from hft_bot config and local .env files."""
    root = Path(__file__).resolve().parents[1]
    _load_env_file(root / "config" / "runtime.env", overwrite=False)
    _load_env_file(root / ".env", overwrite=True)


@dataclass
class SmokeResult:
    """Store smoke-test result payload for JSON output."""

    ok: bool
    step: str
    details: str
    payload: dict[str, Any] | None = None


def _resolve_credentials() -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Resolve wallet and optional API credentials from env."""
    private_key = os.getenv("PRIVATE_KEY") or os.getenv("CLOB_PRIVATE_KEY")
    funder = (
        os.getenv("FUNDER")
        or os.getenv("POLY_FUNDER_ADDRESS")
        or os.getenv("WALLET")
        or os.getenv("WALLET_ADDRESS")
        or os.getenv("CLOB_FUNDER")
    )
    api_key = (
        os.getenv("POLY_API_KEY")
        or os.getenv("POLIMARKET_API_KEY")
        or os.getenv("POLYMARKET_API_KEY")
        or os.getenv("CLOB_API_KEY")
    )
    api_secret = (
        os.getenv("POLY_API_SECRET")
        or os.getenv("POLIMARKET_API_SECRET")
        or os.getenv("POLYMARKET_API_SECRET")
        or os.getenv("CLOB_API_SECRET")
    )
    api_passphrase = (
        os.getenv("POLY_API_PASSPHRASE")
        or os.getenv("POLIMARKET_API_PASSPHRASE")
        or os.getenv("POLYMARKET_API_PASSPHRASE")
        or os.getenv("CLOB_API_PASSPHRASE")
    )
    return private_key, funder, api_key, api_secret, api_passphrase


def _call_balance_allowance(client: Any) -> SmokeResult:
    """Try non-trading allowance/deposit calls using known client APIs."""
    if not hasattr(client, "get_balance_allowance"):
        return SmokeResult(False, "balance_allowance", "Client has no get_balance_allowance method.")

    method = getattr(client, "get_balance_allowance")
    errors: list[str] = []

    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        payload = method(params=params)
        return SmokeResult(
            True,
            "balance_allowance",
            "Balance allowance fetched for asset_type=COLLATERAL.",
            {"response": str(payload)},
        )
    except Exception as exc:
        errors.append(f"BalanceAllowanceParams(COLLATERAL): {exc}")

    try:
        payload = method()
        return SmokeResult(
            True,
            "balance_allowance",
            "Balance allowance fetched with empty args.",
            {"response": str(payload)},
        )
    except Exception as exc:
        errors.append(f"empty_args: {exc}")

    return SmokeResult(False, "balance_allowance", "All allowance call variants failed.", {"errors": errors})


def _smoke_test() -> SmokeResult:
    """Run auth and non-trading balance smoke test against CLOB API."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except Exception as exc:
        return SmokeResult(False, "import", f"py_clob_client import failed: {exc}")

    private_key, funder, api_key, api_secret, api_passphrase = _resolve_credentials()
    if not private_key or not funder:
        return SmokeResult(
            False,
            "env",
            "Missing PRIVATE_KEY/CLOB_PRIVATE_KEY or FUNDER/WALLET/WALLET_ADDRESS/CLOB_FUNDER.",
        )

    signature_type = int(
        os.getenv("HFT_CLOB_SIGNATURE_TYPE")
        or os.getenv("POLY_SIGNATURE_TYPE")
        or "1"
    )

    attempts: list[tuple[str, Any]] = []
    if api_key and api_secret and api_passphrase:
        attempts.append(
            (
                "explicit_api_keys",
                ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                ),
            )
        )
    attempts.append(("derived_api_keys", None))

    attempt_errors: list[dict[str, Any]] = []
    for auth_mode, creds in attempts:
        try:
            if auth_mode == "explicit_api_keys":
                client = ClobClient(
                    "https://clob.polymarket.com",
                    key=private_key,
                    chain_id=137,
                    signature_type=signature_type,
                    funder=funder,
                    creds=creds,
                )
            else:
                client = ClobClient(
                    "https://clob.polymarket.com",
                    key=private_key,
                    chain_id=137,
                    signature_type=signature_type,
                    funder=funder,
                )
                client.set_api_creds(client.create_or_derive_api_creds())
        except Exception as exc:
            attempt_errors.append({"auth_mode": auth_mode, "error": f"client/auth init failed: {exc}"})
            continue
        try:
            allowance_result = _call_balance_allowance(client)
        except Exception as exc:
            attempt_errors.append({"auth_mode": auth_mode, "error": f"allowance call failed: {exc}"})
            continue
        if allowance_result.ok:
            allowance_result.payload = {
                **(allowance_result.payload or {}),
                "auth_mode": auth_mode,
                "funder": funder,
                "signature_type": signature_type,
            }
            return allowance_result
        attempt_errors.append(
            {
                "auth_mode": auth_mode,
                "error": "balance/allowance call failed",
                "allowance_errors": (allowance_result.payload or {}).get("errors", []),
            }
        )

    return SmokeResult(
        False,
        "balance_allowance",
        "All authentication modes failed for deposit/allowance query.",
        {
            "funder": funder,
            "attempts": attempt_errors,
        },
    )


def main() -> int:
    """Execute smoke test and print machine-readable result."""
    parser = argparse.ArgumentParser(description="Polymarket CLOB key smoke test.")
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    args = parser.parse_args()

    _load_runtime_env()
    result = _smoke_test()
    output = {
        "ok": result.ok,
        "step": result.step,
        "details": result.details,
        "payload": result.payload or {},
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False))
    else:
        print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

