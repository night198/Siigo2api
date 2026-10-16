"""Local native Responses and Chat Completions bridge command line."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import math
import re
import sys
import time
from urllib.parse import quote

from .auth import AuthManager, BridgeError, CredentialStore, intent_expiry


def _utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _machine(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{24}", value) is None:
        raise argparse.ArgumentTypeError("machine must be 24 lowercase hexadecimal characters")
    return value


def _port(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Windows Siigo bridge with readable model IDs, native APIs and LOCAL OMP tools.",
        epilog=("Listens only on 127.0.0.1; credentials use current-user Windows DPAPI. "
                "No protocol conversion, cloud tool execution, or automatic startup installation. "
                "Uses Siigo internal APIs, which may change; account permissions, machine availability, "
                "quotas and site billing still apply. Site cost units are not dollars; zero OMP costs "
                "mean uncalibrated, not free. Never pass cloud tokens on the command line."),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the loopback-only native model service",
        description="Run in the foreground on 127.0.0.1. Ctrl-C stops the local service; no autostart is installed.")
    serve.add_argument("--port", type=_port, default=8766, help="local TCP port (default: 8766)")
    login = commands.add_parser("login", help="authorize with the official QR login and save with DPAPI",
        description="Display the official authorization URL and QR. Poll actual authorization every two seconds. "
                    "Only approved login is claimed and saved; existing local API key is preserved. "
                    "A storage failure permits local-save retry without reclaiming the login. "
                    "Ctrl-C stops local waiting only; the remote authorization intent expires normally.")
    login.add_argument("--machine", type=_machine, required=True,
        help="your own 24hex Siigo cloud machine ID")
    commands.add_parser("status", help="show local credential presence, machine and access expiry only",
        description="Read local DPAPI credentials without network access or token refresh. No secrets are printed.")
    commands.add_parser("key", help="print ONLY the local API key for OMP command-secret resolution",
        description="Print the current-user DPAPI local bridge API key and a newline only. "
                    "This is a secret for the loopback service, not a cloud access or refresh token.")
    commands.add_parser("catalog", help="show public model IDs and manufacturer context budgets",
        description="Print readable IDs, manufacturer context budgets with sources, and separate site declarations. "
                    "Gateway capacity and model identity remain unverified. May refresh access; never exports workload credentials.")
    commands.add_parser("refresh", help="rotate and persist the real access/refresh pair",
        description="Perform one real pair rotation and atomically persist it. "
                    "An uncertain prior refresh requires login; inference is never replayed.")
    return parser


def _expiry_text(access: str) -> str | None:
    try:
        segment = access.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        expiry = payload.get("exp")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
            return None
        return datetime.fromtimestamp(expiry, timezone.utc).isoformat()
    except (ValueError, IndexError, TypeError, OverflowError, OSError):
        return None


def _login(manager: AuthManager, machine: str) -> None:
    import qrcode

    intent = manager.start_login()
    expiry = intent_expiry(intent)
    url = "https://www.siigo.cn/login/wechat/miniprogram/" + quote(intent["authorizationCapability"], safe="")
    print("Official Siigo authorization URL (share only with the person authorizing this device):")
    print(url)
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=True)
    print("Expires (UTC): " + datetime.fromtimestamp(expiry, timezone.utc).isoformat(), flush=True)
    previous = None
    while True:
        if time.time() >= expiry:
            raise BridgeError("Login expired", "login_expired", 401)
        state = manager.observe_login(intent)
        if state == "已授权":
            while True:
                try:
                    manager.claim_login(intent, machine_id=machine)
                    break
                except BridgeError as error:
                    if error.code != "store_failed":
                        raise
                    print("Login approved, but encrypted storage failed. Fix local storage, then press Enter "
                          "to retry saving only. Leaving now requires a new login.", file=sys.stderr, flush=True)
                    input()
            print("Login approved and credentials saved.")
            return
        if state not in ("待授权", "待确认"):
            raise BridgeError("Login denied or no longer pending", "login_not_approved", 401)
        if state != previous:
            print("Authorization state: " + state, flush=True)
            previous = state
        time.sleep(min(2, max(0, expiry - time.time())))


def _catalog(manager: AuthManager) -> None:
    from .transport import Forward

    credentials = manager.credentials(refresh=True)
    forward = Forward(credentials.access, credentials.machine_id, {"action": "models"})
    try:
        if not 200 <= forward.status < 300:
            raise BridgeError("Catalog request failed", "catalog_upstream", forward.status)
        body = bytearray()
        for chunk in forward.iter_bytes():
            body.extend(chunk)
            if len(body) > 16 * 1024 * 1024:
                raise BridgeError("Catalog too large", "catalog_invalid", 502)
        try:
            catalog = json.loads(body)
        except (ValueError, UnicodeError):
            raise BridgeError("Invalid catalog", "catalog_invalid", 502) from None
        if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
            raise BridgeError("Invalid catalog", "catalog_invalid", 502)
        data = []
        for item in catalog["data"]:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or len(item["id"]) > 100 or re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", item["id"]) is None
                    or item.get("api") not in ("openai-responses", "openai-completions")):
                raise BridgeError("Invalid catalog", "catalog_invalid", 502)
            safe = {"id": item["id"], "object": "model", "owned_by": "siigo", "api": item["api"]}
            if isinstance(item.get("name"), str):
                safe["name"] = item["name"]
            if isinstance(item.get("reasoning"), bool):
                safe["reasoning"] = item["reasoning"]
            if isinstance(item.get("input"), list):
                safe["input"] = [value for value in item["input"] if value in ("text", "image")]
            for field in ("contextWindow", "maxTokens", "upstreamDeclaredContextWindow"):
                if type(item.get(field)) is int and item[field] > 0:
                    safe[field] = item[field]
            if item.get("contextWindowSource") == "manufacturer":
                safe["contextWindowSource"] = "manufacturer"
            source_url = item.get("contextWindowSourceUrl")
            if isinstance(source_url, str) and source_url.startswith("https://"):
                safe["contextWindowSourceUrl"] = source_url
            thinking = item.get("thinkingLevelMap")
            if isinstance(thinking, dict):
                safe["thinkingLevelMap"] = {
                    key: value for key, value in thinking.items()
                    if key in ("off", "minimal", "low", "medium", "high", "xhigh", "max")
                    and (value is None or isinstance(value, str))
                }
            compat = item.get("compat")
            if isinstance(compat, dict):
                boolean_flags = {
                    "supportsStore", "supportsDeveloperRole", "supportsStrictMode",
                    "supportsReasoningEffort", "supportsReasoningParams",
                    "requiresReasoningContentOnAssistantMessages",
                }
                safe_compat = {key: value for key, value in compat.items()
                               if key in boolean_flags and type(value) is bool}
                for key, allowed in (
                    ("maxTokensField", ("max_tokens", "max_completion_tokens")),
                    ("thinkingFormat", ("qwen", "deepseek", "openai")),
                ):
                    if compat.get(key) in allowed:
                        safe_compat[key] = compat[key]
                safe["compat"] = safe_compat
            data.append(safe)
        print(json.dumps({"object": "list", "data": data}, ensure_ascii=True, indent=2))
    finally:
        forward.close()


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            import uvicorn
            from .server import create_app

            uvicorn.run(create_app(), host="127.0.0.1", port=args.port, access_log=False)
            return 0
        manager = AuthManager(CredentialStore())
        if args.command == "login":
            _login(manager, args.machine)
        elif args.command == "key":
            print(manager.credentials().api_key)
        elif args.command == "refresh":
            manager.refresh()
            print("Credentials rotated and saved.")
        elif args.command == "catalog":
            _catalog(manager)
        elif args.command == "status":
            try:
                credentials = manager.credentials()
            except BridgeError as error:
                if error.code != "credential_missing":
                    raise
                print(json.dumps({"credential_present": False}))
            else:
                print(json.dumps({"credential_present": True, "machine_id": credentials.machine_id,
                                  "access_expires_at": _expiry_text(credentials.access),
                                  "login_required": credentials.refresh_uncertain}, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Stopped locally; no remote authorization cancellation was requested.", file=sys.stderr)
        return 130
    except BridgeError as error:
        code = error.code if isinstance(error.code, str) and re.fullmatch(r"[a-z_]{1,64}", error.code) else "bridge_error"
        status = error.http_status if type(error.http_status) is int and 100 <= error.http_status <= 599 else 502
        print(f"Siigo bridge error: {code} (status {status}).", file=sys.stderr)
        return 1
    except Exception:
        print("Siigo bridge error: operation_failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
