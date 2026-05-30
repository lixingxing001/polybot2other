from __future__ import annotations

import argparse
import getpass
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

from .config import LIVE_CREDENTIAL_ENV_KEYS, Settings
from .live import PolymarketLiveClient, _is_address_like, _is_private_key_like, _mask_address


LIVE_ENV_TEMPLATE = """# Copy to .env.live, run chmod 600 .env.live, and fill the private values locally.
# Do not commit .env.live.

POLYBOT2OTHER_LIVE_TRADING_DB_PATH=data/live/single_fak_real.sqlite3
POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH=data/live/live-settings.json
POLYBOT2OTHER_LIVE_CHAIN_ID=137
POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=true
POLYBOT2OTHER_LIVE_DEFAULT_INITIAL_BALANCE=20
POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS=2
POLYBOT2OTHER_LIVE_DEFAULT_MAX_DAILY_LOSS=6
POLYBOT2OTHER_LIVE_DEFAULT_MAX_TOTAL_DRAWDOWN=12
POLYBOT2OTHER_LIVE_DEFAULT_RETRY_COUNT=2
POLYBOT2OTHER_LIVE_DEFAULT_RETRY_DELAY_MS=250
POLYBOT2OTHER_LLM_SUPER_AGENT_ENABLED=true
POLYBOT2OTHER_LLM_API_KEY=
POLYBOT2OTHER_LLM_BASE_URL=https://api.hao.ai/v1
POLYBOT2OTHER_LLM_MODEL=openai/gpt-5.4-mini
POLYBOT2OTHER_LLM_TIMEOUT_SECONDS=1.2
POLYBOT2OTHER_LLM_MIN_INTERVAL_SECONDS=12

POLYBOT2OTHER_LIVE_PRIVATE_KEY=
POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=
POLYBOT2OTHER_LIVE_FUNDER_ADDRESS=
POLYBOT2OTHER_LIVE_API_KEY=
POLYBOT2OTHER_LIVE_API_SECRET=
POLYBOT2OTHER_LIVE_API_PASSPHRASE=
"""


@dataclass(frozen=True)
class LiveEnvSetupValues:
    private_key: str
    signature_type: str
    funder_address: str
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""


def write_live_env_file(path: Path, values: LiveEnvSetupValues, *, base_text: str | None = None) -> dict[str, Any]:
    errors = validate_live_env_values(values)
    if errors:
        raise ValueError("; ".join(errors))
    source_text = base_text if base_text is not None else _base_env_text(path)
    updates = {
        "POLYBOT2OTHER_LIVE_PRIVATE_KEY": values.private_key,
        "POLYBOT2OTHER_LIVE_SIGNATURE_TYPE": values.signature_type,
        "POLYBOT2OTHER_LIVE_FUNDER_ADDRESS": values.funder_address,
        "POLYBOT2OTHER_LIVE_API_KEY": values.api_key,
        "POLYBOT2OTHER_LIVE_API_SECRET": values.api_secret,
        "POLYBOT2OTHER_LIVE_API_PASSPHRASE": values.api_passphrase,
    }
    path.write_text(_upsert_env_values(source_text, updates), encoding="utf-8")
    path.chmod(0o600)
    return {
        "path": str(path),
        "mode": "0o600",
        "updated_keys": sorted(updates),
        "credential_keys": sorted(LIVE_CREDENTIAL_ENV_KEYS),
        "api_credentials_mode": "env_api_creds" if values.api_key else "derive_api_creds_with_private_key",
    }


def validate_live_env_values(values: LiveEnvSetupValues) -> list[str]:
    errors: list[str] = []
    private_key = values.private_key.strip()
    signature_type = values.signature_type.strip()
    funder_address = values.funder_address.strip()
    api_parts = [values.api_key.strip(), values.api_secret.strip(), values.api_passphrase.strip()]
    if not _is_private_key_like(private_key):
        errors.append("POLYBOT2OTHER_LIVE_PRIVATE_KEY must be a 0x-prefixed 32-byte hex private key")
    if signature_type not in {"0", "1", "2", "3"}:
        errors.append("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE must be one of 0, 1, 2, 3")
    if not _is_address_like(funder_address):
        errors.append("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS must be a 0x-prefixed EVM address")
    if any(api_parts) and not all(api_parts):
        errors.append("CLOB API credentials must be all filled, or all left blank for SDK derivation")
    if signature_type == "0" and _is_private_key_like(private_key) and _is_address_like(funder_address):
        try:
            signer = PolymarketLiveClient(Settings())._signer_address_from_private_key(  # noqa: SLF001
                PolymarketLiveClient(Settings())._sdk(),  # noqa: SLF001
                private_key,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Could not derive signer address from private key: {type(exc).__name__}: {exc}")
        else:
            if signer.lower() != funder_address.lower():
                errors.append(
                    "SIGNATURE_TYPE=0 requires POLYBOT2OTHER_LIVE_FUNDER_ADDRESS to equal "
                    f"the private-key signer address {_mask_address(signer)}"
                )
    return errors


def _base_env_text(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    example = path.with_name(f"{path.name}.example")
    if example.exists():
        return example.read_text(encoding="utf-8")
    project_example = Path(".env.live.example")
    if project_example.exists():
        return project_example.read_text(encoding="utf-8")
    return LIVE_ENV_TEMPLATE


def _upsert_env_values(text: str, updates: dict[str, str]) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        candidate = stripped.removeprefix("export ").strip() if stripped.startswith("export ") else stripped
        if not candidate or candidate.startswith("#") or "=" not in candidate:
            output.append(raw_line)
            continue
        key = candidate.split("=", 1)[0].strip()
        if key not in updates:
            output.append(raw_line)
            continue
        output.append(f"{key}={_single_line_env_value(updates[key])}")
        seen.add(key)
    missing = [key for key in updates if key not in seen]
    if missing and output and output[-1].strip():
        output.append("")
    for key in missing:
        output.append(f"{key}={_single_line_env_value(updates[key])}")
    return "\n".join(output).rstrip() + "\n"


def _single_line_env_value(value: str) -> str:
    text = str(value or "").strip()
    if "\n" in text or "\r" in text:
        raise ValueError("env values must be single-line")
    return text


def _prompt_values() -> LiveEnvSetupValues:
    print("This writes live credentials to .env.live locally. Values are not echoed.")
    private_key = getpass.getpass("POLYBOT2OTHER_LIVE_PRIVATE_KEY: ").strip()
    signature_type = input("POLYBOT2OTHER_LIVE_SIGNATURE_TYPE [3]: ").strip() or "3"
    funder_address = input("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS: ").strip()
    fill_api = input("Fill optional CLOB API credentials? [y/N]: ").strip().lower()
    if fill_api in {"y", "yes"}:
        api_key = getpass.getpass("POLYBOT2OTHER_LIVE_API_KEY: ").strip()
        api_secret = getpass.getpass("POLYBOT2OTHER_LIVE_API_SECRET: ").strip()
        api_passphrase = getpass.getpass("POLYBOT2OTHER_LIVE_API_PASSPHRASE: ").strip()
    else:
        api_key = api_secret = api_passphrase = ""
    return LiveEnvSetupValues(
        private_key=private_key,
        signature_type=signature_type,
        funder_address=funder_address,
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )


def _post_reload(service_url: str, timeout: float) -> dict[str, Any]:
    url = service_url.rstrip("/") + "/api/live-reload-credentials"
    req = request.Request(url, data=b"{}", method="POST", headers={"content-type": "application/json"})
    with request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("service reload returned a non-object payload")
    return payload


def _summary_after_reload(payload: dict[str, Any]) -> dict[str, Any]:
    live = payload.get("live_trading") if isinstance(payload.get("live_trading"), dict) else {}
    readiness = live.get("readiness") if isinstance(live.get("readiness"), dict) else {}
    addresses = readiness.get("credential_addresses") if isinstance(readiness.get("credential_addresses"), dict) else {}
    return {
        "enabled": live.get("enabled"),
        "readiness_ready": readiness.get("ready"),
        "readiness_errors": list(readiness.get("errors") or []),
        "credential_presence": readiness.get("credential_presence") or {},
        "credential_addresses": {
            "signature_type": addresses.get("signature_type"),
            "signer_address_masked": addresses.get("signer_address_masked"),
            "funder_address_masked": addresses.get("funder_address_masked"),
            "signer_matches_funder": addresses.get("signer_matches_funder"),
            "warnings": list(addresses.get("warnings") or []),
            "errors": list(addresses.get("errors") or []),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely create or update local .env.live credentials")
    parser.add_argument("--env-file", default=".env.live", help="Local env file to update")
    parser.add_argument("--service-url", default="", help="Optional running dashboard URL to reload after writing")
    parser.add_argument("--service-timeout", type=float, default=20.0)
    parser.add_argument("--dry-run", action="store_true", help="Validate prompts but do not write the file")
    args = parser.parse_args(argv)
    path = Path(args.env_file)
    try:
        values = _prompt_values()
        errors = validate_live_env_values(values)
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
            return 2
        if args.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, "path": str(path)}, ensure_ascii=False, indent=2))
            return 0
        result = write_live_env_file(path, values)
        payload: dict[str, Any] = {"ok": True, "env_file": result}
        if args.service_url:
            try:
                payload["service_reload"] = _summary_after_reload(_post_reload(args.service_url, args.service_timeout))
            except (OSError, error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
                payload["service_reload"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
