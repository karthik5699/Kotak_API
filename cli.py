#!/usr/bin/env python3
"""
CLI & Subprocess Entrypoint for Kotak Neo Option Chain & Greeks Engine.

Supports two modes:
1. Non-blocking Agent Mode (`--json` or `--whatsapp`):
   Never blocks on stdin. If permanent setup or daily TOTP is needed, outputs a structured
   JSON payload (`status: "SETUP_REQUIRED"` or `status: "TOTP_REQUIRED"`) so a WhatsApp
   LLM agent can ask the user over WhatsApp and invoke `setup` or `totp` next.

2. Interactive Terminal Mode (default when run in a terminal without `--json`):
   Automatically prompts once for any missing permanent credentials (`consumer_key`,
   `mobile_number`, `ucc`, `mpin`) and saves them permanently to `data/credentials.json`.
   Then checks if today's session is active; if expired, prompts for today's 6-digit TOTP.

Usage Examples:
  python3 cli.py status [--json]
  python3 cli.py setup --mobile +919876543210 --ucc ABC12 --mpin 123456 --consumer-key <TOKEN>
  python3 cli.py totp 123456 [--json]
  python3 cli.py expiries NIFTY [--exchange nse_fo] [--json]
  python3 cli.py chain NIFTY [--expiry 2026-11-12] [--count 20] [--depth] [--whatsapp] [--json]
  python3 cli.py strike NIFTY 24500 CE [--expiry 2026-11-12] [--json]
  python3 cli.py greeks --ltp 165.5 --underlying-price 24520 --strike 24500 --expiry 2026-11-12 --type CE
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from kotak_option_chain.agent_tools import TOOL_SCHEMAS, KotakWhatsAppAgentToolkit
from kotak_option_chain.formatters import format_whatsapp_option_chain_summary

console = Console()


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _interactive_ensure_authenticated(toolkit: KotakWhatsAppAgentToolkit) -> bool:
    """
    When run interactively in a terminal (without `--json`), guide the user through:
    1. Permanent credential setup (only for missing fields, saved permanently)
    2. Daily 6-digit TOTP login (only once per trading day)
    """
    status = toolkit.check_kotak_auth_status()

    if status["status"] == "SETUP_REQUIRED":
        missing = status.get("missing_fields", [])
        console.print(
            Panel(
                "[bold yellow]First-Time Permanent Setup[/bold yellow]\n"
                "These credentials will be saved permanently in [cyan]data/credentials.json[/cyan] (chmod 600)\n"
                "so you never have to enter them again.",
                title="Kotak Neo Setup",
            )
        )
        updates: dict[str, str] = {}
        prompts = {
            "consumer_key": "Enter Kotak Neo Consumer Key (Trade API Token): ",
            "mobile_number": "Enter Registered Mobile Number (+91XXXXXXXXXX): ",
            "ucc": "Enter Unique Client Code (UCC / User ID): ",
            "mpin": "Enter 6-digit Trading MPIN: ",
        }
        for field in missing:
            if field == "mpin":
                val = getpass.getpass(prompts[field]).strip()
            else:
                val = input(prompts[field]).strip()
            if not val:
                console.print(f"[red]Error: {field} cannot be empty.[/red]")
                return False
            updates[field] = val

        status = toolkit.save_kotak_permanent_credentials(**updates)
        console.print("[green]✓ Permanent credentials saved![/green]\n")

    if status["status"] == "TOTP_REQUIRED":
        console.print(
            Panel(
                f"[bold cyan]Daily TOTP Required ({status.get('trading_date_ist')})[/bold cyan]\n"
                "Your daily Kotak session resets every morning at 06:00 AM IST.\n"
                "Please enter the current 6-digit TOTP from your Authenticator app.",
                title="Daily 2FA Login",
            )
        )
        totp_code = input("Enter 6-digit TOTP: ").strip()
        login_res = toolkit.submit_daily_totp(totp_code)
        if login_res.get("status") != "AUTHENTICATED":
            console.print(f"[bold red]Login Failed:[/bold red] {json.dumps(login_res, indent=2)}")
            return False
        console.print(f"[bold green]✓ {login_res.get('message')}[/bold green]\n")
        return True

    return status.get("status") == "AUTHENTICATED"


def _render_rich_option_chain(data: dict[str, Any]) -> None:
    if data.get("status") != "OK":
        console.print(f"[bold red]{data.get('status')}[/bold red]: {data.get('message', '')}")
        if data.get("agent_instruction"):
            console.print(f"[yellow]{data['agent_instruction']}[/yellow]")
        return

    up = data.get("underlying_prices") or {}
    analytics = data.get("analytics") or {}
    straddle = analytics.get("atm_straddle") or {}
    pcr = analytics.get("pcr") or {}

    header_text = (
        f"[bold cyan]{data.get('underlying')}[/bold cyan] ({data.get('exchange')}) | "
        f"Expiry: [bold]{data.get('expiry')}[/bold] ({data.get('days_to_expiry')}d) | "
        f"Lot Size: [bold]{data.get('lot_size')}[/bold]\n"
        f"Ref Price ({up.get('reference_source')} / {up.get('pricing_model')}): "
        f"[bold green]{up.get('reference_price')}[/bold green] | "
        f"Spot: {up.get('spot_price') or '-'} | Futures: {up.get('futures_price') or '-'}\n"
        f"ATM: [bold yellow]{analytics.get('atm_strike')}[/bold yellow] | "
        f"ATM Straddle: ₹{straddle.get('combined_premium') or '-'} "
        f"(±{straddle.get('implied_move_pct') or '-'}%) | "
        f"ATM IV: {straddle.get('atm_avg_iv') or '-'}%\n"
        f"OI PCR: [bold]{pcr.get('oi_pcr') or '-'}[/bold] ({pcr.get('sentiment', '-')}) | "
        f"Vol PCR: {pcr.get('volume_pcr') or '-'} | "
        f"Max Pain: [bold magenta]{analytics.get('max_pain_strike') or '-'}[/bold magenta]"
    )
    console.print(Panel(header_text, title="Kotak Neo Option Chain & Greeks"))

    table = Table(show_header=True, header_style="bold white on dark_blue")
    table.add_column("CE OI (Chg)", justify="right")
    table.add_column("CE Vol", justify="right")
    table.add_column("CE IV%", justify="right", style="cyan")
    table.add_column("CE Δ", justify="right", style="green")
    table.add_column("CE Γ / ν / Θ", justify="right", style="dim")
    table.add_column("CE LTP", justify="right", style="bold green")
    table.add_column("STRIKE", justify="center", style="bold yellow")
    table.add_column("PE LTP", justify="right", style="bold red")
    table.add_column("PE Δ", justify="right", style="red")
    table.add_column("PE IV%", justify="right", style="cyan")
    table.add_column("PE Γ / ν / Θ", justify="right", style="dim")
    table.add_column("PE Vol", justify="right")
    table.add_column("PE OI (Chg)", justify="right")

    atm_strike = analytics.get("atm_strike")
    for row in data.get("strikes") or []:
        k = row["strike_price"]
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        ce_g = ce.get("greeks") or {}
        pe_g = pe.get("greeks") or {}

        is_atm = atm_strike is not None and abs(k - atm_strike) < 1e-4
        strike_str = f"★ {k:g}" if is_atm else f"{k:g}"

        table.add_row(
            f"{ce.get('oi_current', 0):,} ({ce.get('oi_change', 0):+,})",
            f"{ce.get('volume', 0):,}",
            f"{ce_g.get('iv') if ce_g.get('iv') is not None else '-'}",
            f"{ce_g.get('delta') if ce_g.get('delta') is not None else '-'}",
            f"{ce_g.get('gamma') or '-'} / {ce_g.get('vega') or '-'} / {ce_g.get('theta') or '-'}",
            f"{ce.get('ltp') if ce.get('ltp') is not None else '-'}",
            strike_str,
            f"{pe.get('ltp') if pe.get('ltp') is not None else '-'}",
            f"{pe_g.get('delta') if pe_g.get('delta') is not None else '-'}",
            f"{pe_g.get('iv') if pe_g.get('iv') is not None else '-'}",
            f"{pe_g.get('gamma') or '-'} / {pe_g.get('vega') or '-'} / {pe_g.get('theta') or '-'}",
            f"{pe.get('volume', 0):,}",
            f"{pe.get('oi_current', 0):,} ({pe.get('oi_change', 0):+,})",
            style="on grey15" if is_atm else None,
        )

    console.print(table)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kotak Neo Option Chain & Greeks CLI (WhatsApp LLM Agent Ready)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. status
    p_status = sub.add_parser("status", help="Check permanent credentials & daily TOTP session status")
    p_status.add_argument("--json", action="store_true", help="Output JSON")

    # 2. setup
    p_setup = sub.add_parser("setup", help="Save permanent credentials (mobile, UCC, MPIN, consumer_key)")
    p_setup.add_argument("--consumer-key", help="Kotak Neo Consumer Key token")
    p_setup.add_argument("--mobile", help="Registered mobile number (+91XXXXXXXXXX)")
    p_setup.add_argument("--ucc", help="Unique Client Code (UCC)")
    p_setup.add_argument("--mpin", help="6-digit Trading MPIN")
    p_setup.add_argument("--json", action="store_true", help="Output JSON")

    # 3. totp
    p_totp = sub.add_parser("totp", help="Authenticate for today using your 6-digit TOTP")
    p_totp.add_argument("code", nargs="?", help="6-digit TOTP code from Authenticator app")
    p_totp.add_argument("--json", action="store_true", help="Output JSON")

    # 4. expiries
    p_exp = sub.add_parser("expiries", help="Fetch active option expiry dates for an underlying")
    p_exp.add_argument("underlying", help="Underlying symbol (e.g. NIFTY, BANKNIFTY, RELIANCE)")
    p_exp.add_argument("--exchange", default="nse_fo", help="Exchange segment (default: nse_fo)")
    p_exp.add_argument("--json", action="store_true", help="Output JSON")

    # 5. chain
    p_chain = sub.add_parser("chain", help="Fetch full option chain with IV, Greeks, PCR, Max Pain")
    p_chain.add_argument("underlying", help="Underlying symbol (e.g. NIFTY, BANKNIFTY, RELIANCE)")
    p_chain.add_argument("--expiry", help="ISO expiry date YYYY-MM-DD (defaults to nearest expiry)")
    p_chain.add_argument("--exchange", default="nse_fo", help="Exchange segment (default: nse_fo)")
    p_chain.add_argument("--count", type=int, default=20, help="Number of strikes around ATM (default: 20)")
    p_chain.add_argument("--depth", action="store_true", help="Include 5-level Bid/Ask market depth")
    p_chain.add_argument(
        "--model",
        choices=["black76", "bsm"],
        default="black76",
        help="Greeks pricing model: black76 (Futures) or bsm (Spot)",
    )
    p_chain.add_argument("--whatsapp", action="store_true", help="Output WhatsApp formatted summary")
    p_chain.add_argument("--json", action="store_true", help="Output full JSON")

    # 6. strike
    p_strike = sub.add_parser("strike", help="Fetch single strike quote, depth, and Greeks")
    p_strike.add_argument("underlying", help="Underlying symbol (e.g. NIFTY)")
    p_strike.add_argument("strike_price", type=float, help="Strike price (e.g. 24500)")
    p_strike.add_argument("option_type", choices=["CE", "PE", "ce", "pe"], help="CE or PE")
    p_strike.add_argument("--expiry", help="ISO expiry date YYYY-MM-DD")
    p_strike.add_argument("--exchange", default="nse_fo", help="Exchange segment")
    p_strike.add_argument("--json", action="store_true", help="Output JSON")

    # 7. greeks (offline calculator)
    p_greeks = sub.add_parser("greeks", help="Calculate IV and Greeks directly from option parameters")
    p_greeks.add_argument("--ltp", type=float, required=True, help="Option LTP / market premium")
    p_greeks.add_argument("--underlying-price", type=float, required=True, help="Underlying Spot/Futures price")
    p_greeks.add_argument("--strike", type=float, required=True, help="Strike price")
    p_greeks.add_argument("--expiry", required=True, help="Expiry date (YYYY-MM-DD)")
    p_greeks.add_argument("--type", choices=["CE", "PE", "ce", "pe"], default="CE", help="CE or PE")
    p_greeks.add_argument("--model", choices=["black76", "bsm"], default="black76")
    p_greeks.add_argument("--rate", type=float, default=0.067, help="Risk-free interest rate (default 0.067)")

    # 8. tools (print LLM tool schemas)
    sub.add_parser("tools", help="Print LLM Agent Function-Calling JSON Schemas")

    args = parser.parse_args(argv)
    toolkit = KotakWhatsAppAgentToolkit()

    if args.command == "tools":
        _print_json(TOOL_SCHEMAS)
        return 0

    if args.command == "greeks":
        res = toolkit.calculate_custom_greeks(
            market_price=args.ltp,
            underlying_price=args.underlying_price,
            strike_price=args.strike,
            expiry=args.expiry,
            option_type=args.type.upper(),
            pricing_model=args.model,
            risk_free_rate=args.rate,
        )
        _print_json(res)
        return 0

    if args.command == "status":
        res = toolkit.check_kotak_auth_status()
        _print_json(res)
        return 0

    if args.command == "setup":
        if not any([args.consumer_key, args.mobile, args.ucc, args.mpin]) and not args.json:
            _interactive_ensure_authenticated(toolkit)
            _print_json(toolkit.check_kotak_auth_status())
            return 0
        res = toolkit.save_kotak_permanent_credentials(
            consumer_key=args.consumer_key,
            mobile_number=args.mobile,
            ucc=args.ucc,
            mpin=args.mpin,
        )
        _print_json(res)
        return 0

    if args.command == "totp":
        code = args.code
        if not code and not args.json:
            code = input("Enter today's 6-digit Kotak TOTP: ").strip()
        res = toolkit.submit_daily_totp(totp=code or "")
        _print_json(res)
        return 0 if res.get("status") == "AUTHENTICATED" else 1

    # Market data commands (expiries, chain, strike)
    is_non_interactive = getattr(args, "json", False) or getattr(args, "whatsapp", False)
    if not is_non_interactive:
        if not _interactive_ensure_authenticated(toolkit):
            return 1

    if args.command == "expiries":
        res = toolkit.get_option_expiries(underlying=args.underlying, exchange=args.exchange)
        _print_json(res)
        return 0

    if args.command == "chain":
        res = toolkit.get_option_chain_with_greeks(
            underlying=args.underlying,
            expiry=args.expiry,
            exchange=args.exchange,
            strikes_count=args.count,
            include_depth=args.depth,
            pricing_model=args.model,
        )
        if args.json:
            _print_json(res)
        elif args.whatsapp:
            print(format_whatsapp_option_chain_summary(res))
        else:
            _render_rich_option_chain(res)
        return 0

    if args.command == "strike":
        res = toolkit.get_strike_greeks_detail(
            underlying=args.underlying,
            strike_price=args.strike_price,
            option_type=args.option_type.upper(),
            expiry=args.expiry,
            exchange=args.exchange,
        )
        _print_json(res)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
