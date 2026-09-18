#!/usr/bin/env python3
"""Telegram fleet bot. Talks to local hub; only the configured chat can control it."""
from __future__ import annotations

import os
import re
import sys
import time
import traceback
import urllib.error
from typing import Any, Optional

import formatters
import report
import util

COMMANDS = [
    {"command": "all", "description": "全部服务器总览"},
    {"command": "nodes", "description": "覆盖范围内的节点列表"},
    {"command": "go", "description": "查看指定节点，例如 /go sg"},
    {"command": "traffic", "description": "流量：/traffic 或 /traffic 名字"},
    {"command": "today", "description": "今日流量"},
    {"command": "cpu", "description": "CPU：/cpu 或 /cpu all"},
    {"command": "mem", "description": "内存"},
    {"command": "disk", "description": "磁盘"},
    {"command": "net", "description": "瞬时网速"},
    {"command": "bw", "description": "实时带宽：/bw 名字 [秒]"},
    {"command": "xray", "description": "Xray / 443"},
    {"command": "uptime", "description": "开机时长"},
    {"command": "add", "description": "纳入节点：/add 名字 cap=2T"},
    {"command": "cap", "description": "改额度：/cap 名字 500G|2T|unlimited"},
    {"command": "kick", "description": "踢出覆盖：/kick 名字"},
    {"command": "off", "description": "停用但仍保留：/off 名字"},
    {"command": "on", "description": "重新启用：/on 名字"},
    {"command": "help", "description": "命令说明"},
]

ALIASES = {
    "/start": "help",
    "/help": "help",
    "/all": "all",
    "/host": "all",
    "/status": "all",
    "/nodes": "nodes",
    "/list": "nodes",
    "/go": "go",
    "/get": "go",
    "/traffic": "traffic",
    "/today": "today",
    "/cpu": "cpu",
    "/mem": "mem",
    "/memory": "mem",
    "/disk": "disk",
    "/net": "net",
    "/bw": "bw",
    "/speed": "bw",
    "/xray": "xray",
    "/uptime": "uptime",
    "/add": "add",
    "/cap": "cap",
    "/kick": "kick",
    "/remove": "kick",
    "/on": "on",
    "/enable": "on",
    "/off": "off",
    "/disable": "off",
    "帮助": "help",
    "总览": "all",
    "节点": "nodes",
    "带宽": "bw",
    "流量": "traffic",
}

HELP = (
    "🤖 <b>机群监控</b>\n\n"
    "<b>查看</b>\n"
    "/all 全部汇总\n"
    "/nodes 覆盖列表\n"
    "/go 名字  一台详情\n"
    "/cpu /mem /disk /today /traffic /net /xray /uptime\n"
    "可加名字或 all，例如 <code>/cpu all</code>\n\n"
    "<b>实时带宽</b>（被动采样，不主动打流）\n"
    "<code>/bw sg</code>  默认 3 秒\n"
    "<code>/bw sg 8</code>  1–15 秒\n"
    "<code>/bw all</code>\n\n"
    "<b>覆盖范围</b>\n"
    "<code>/add hk cap=2T reset=27</code>\n"
    "<code>/add home cap=unlimited</code>\n"
    "<code>/cap hk 500G</code>\n"
    "<code>/off hk</code>  暂时不看\n"
    "<code>/on hk</code>\n"
    "<code>/kick hk</code>  踢出，需再 /add 才会重新纳入\n\n"
    "额度支持 500G / 1T / 2T / unlimited。日报仍会自动发。"
)


def hub_base() -> str:
    return util.env_opt("HUB_URL", "http://127.0.0.1:8788").rstrip("/")


def fleet_token() -> str:
    return util.env("FLEET_TOKEN")


def public_hub() -> str:
    return util.env_opt("FLEET_PUBLIC_URL") or f"http://{report.host_label()}:{util.env_int('HUB_PORT', 8788)}"


def hub_call(
    method: str,
    path: str,
    payload: Optional[dict[str, Any]] = None,
    timeout: int = 20,
) -> dict[str, Any]:
    return util.http_json(method, hub_base() + path, fleet_token(), payload, timeout)


def nodes(include_disabled: bool = True) -> list[dict[str, Any]]:
    rows = hub_call("GET", "/v1/nodes").get("nodes") or []
    if include_disabled:
        return rows
    return [row for row in rows if row.get("enabled", True)]


def node_named(name: str) -> dict[str, Any]:
    name = util.normalize_node_name(name)
    try:
        return hub_call("GET", f"/v1/nodes/{name}")["node"]
    except RuntimeError as exc:
        raise KeyError(name) from exc


def default_target(args: list[str], rows: Optional[list[dict[str, Any]]] = None) -> str:
    if args and args[0].lower() != "all":
        return util.normalize_node_name(args[0])
    rows = rows if rows is not None else nodes(False)
    enabled = [row["name"] for row in rows if row.get("enabled", True)]
    if len(enabled) == 1:
        return enabled[0]
    raise ValueError("有多台机器，请写名字，例如 /cpu sg 或 /cpu all")


def parse_message(text: str) -> tuple[str, list[str], dict[str, str]]:
    raw = (text or "").strip()
    if not raw:
        return "", [], {}
    bits = raw.split()
    first = re.sub(r"@\w+$", "", bits[0])
    key = first.lower() if first.startswith("/") else first
    cmd = ALIASES.get(first.lower() if first.startswith("/") else first, ALIASES.get(key, ""))
    args: list[str] = []
    flags: dict[str, str] = {}
    for tok in bits[1:]:
        if "=" in tok[1:]:
            name, value = tok.split("=", 1)
            flags[name.lower()] = value
        else:
            args.append(tok)
    return cmd, args, flags


def send_text(token: str, chat_id: str, text: str) -> None:
    if len(text) > 3900:
        text = text[:3890] + "\n…"
    report.telegram_call(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )


def cmd_help(_: list[str], __: dict[str, str]) -> str:
    return HELP


def cmd_all(_: list[str], __: dict[str, str]) -> str:
    return formatters.fleet_overview(nodes(False))


def cmd_nodes(_: list[str], __: dict[str, str]) -> str:
    return formatters.node_list(nodes(True))


def cmd_go(args: list[str], _: dict[str, str]) -> str:
    name = default_target(args)
    return formatters.node_detail(node_named(name))


def _one_or_all(args: list[str], kind: str) -> str:
    rows = nodes(False)
    if args and args[0].lower() == "all":
        chunks = [formatters.metric_block(row, kind) for row in rows]
        return "\n\n".join(chunks) if chunks else "没有在线覆盖的节点。"
    name = default_target(args, rows)
    return formatters.metric_block(node_named(name), kind)


def cmd_traffic(args: list[str], flags: dict[str, str]) -> str:
    if args and args[0].lower() == "all":
        return cmd_all(args, flags)
    return cmd_go(args, flags)


def cmd_cpu(args: list[str], _: dict[str, str]) -> str:
    return _one_or_all(args, "cpu")


def cmd_mem(args: list[str], _: dict[str, str]) -> str:
    return _one_or_all(args, "mem")


def cmd_disk(args: list[str], _: dict[str, str]) -> str:
    return _one_or_all(args, "disk")


def cmd_net(args: list[str], _: dict[str, str]) -> str:
    return _one_or_all(args, "net")


def cmd_today(args: list[str], _: dict[str, str]) -> str:
    return _one_or_all(args, "today")


def cmd_xray(args: list[str], _: dict[str, str]) -> str:
    return _one_or_all(args, "xray")


def cmd_uptime(args: list[str], _: dict[str, str]) -> str:
    return _one_or_all(args, "uptime")


def _run_bw(name: str, seconds: float) -> str:
    created = hub_call("POST", "/v1/jobs", {"node": name, "type": "bw", "params": {"seconds": seconds}})
    job_id = created["id"]
    job = hub_call("GET", f"/v1/jobs/{job_id}?wait=1", timeout=40).get("job") or {}
    if job.get("status") != "ok":
        err = job.get("error") or job.get("status") or "timeout"
        return f"{report.h(name)} 带宽测试失败：{report.h(err)}"
    return formatters.bw_result(name, job.get("result") or {})


def cmd_bw(args: list[str], _: dict[str, str]) -> str:
    seconds = 3.0
    rest = list(args)
    if rest and re.fullmatch(r"\d+(?:\.\d+)?", rest[-1]):
        seconds = float(rest.pop())
    seconds = max(1.0, min(15.0, seconds))
    if rest and rest[0].lower() == "all":
        rows = [row for row in nodes(False) if row.get("enabled", True)]
        if not rows:
            return "没有节点。"
        return "\n\n".join(_run_bw(row["name"], seconds) for row in rows)
    name = default_target(rest)
    return _run_bw(name, seconds)


def cmd_add(args: list[str], flags: dict[str, str]) -> str:
    if not args:
        return "用法：<code>/add hk cap=2T reset=27</code>"
    name = args[0]
    cap_text = flags.get("cap", "unlimited")
    cap = util.parse_cap(cap_text)
    reset_day = int(flags.get("reset") or flags.get("reset_day") or 1)
    iface = flags.get("iface") or "eth0"
    hub_call(
        "POST",
        "/v1/nodes",
        {
            "action": "add",
            "name": name,
            "cap_bytes": cap,
            "reset_day": reset_day,
            "iface": iface,
        },
    )
    return formatters.add_help(util.normalize_node_name(name), public_hub(), util.format_cap(cap), reset_day)


def cmd_cap(args: list[str], flags: dict[str, str]) -> str:
    if len(args) < 2:
        return "用法：<code>/cap hk 2T</code> 或 <code>/cap hk unlimited</code>"
    cap = util.parse_cap(args[1])
    hub_call("POST", "/v1/nodes", {"action": "cap", "name": args[0], "cap_bytes": cap})
    return f"{report.h(util.normalize_node_name(args[0]))} 额度已改为 {report.h(util.format_cap(cap))}"


def cmd_kick(args: list[str], _: dict[str, str]) -> str:
    if not args:
        return "用法：<code>/kick hk</code>"
    hub_call("POST", "/v1/nodes", {"action": "kick", "name": args[0]})
    return f"已踢出 <code>{report.h(util.normalize_node_name(args[0]))}</code>。再接入需要重新 /add。"


def cmd_on(args: list[str], _: dict[str, str]) -> str:
    if not args:
        return "用法：<code>/on hk</code>"
    hub_call("POST", "/v1/nodes", {"action": "enable", "name": args[0]})
    return f"已启用 <code>{report.h(util.normalize_node_name(args[0]))}</code>"


def cmd_off(args: list[str], _: dict[str, str]) -> str:
    if not args:
        return "用法：<code>/off hk</code>"
    hub_call("POST", "/v1/nodes", {"action": "disable", "name": args[0]})
    return f"已停用 <code>{report.h(util.normalize_node_name(args[0]))}</code>（机器还在，只是不汇总）"


HANDLERS = {
    "help": cmd_help,
    "all": cmd_all,
    "nodes": cmd_nodes,
    "go": cmd_go,
    "traffic": cmd_traffic,
    "today": cmd_today,
    "cpu": cmd_cpu,
    "mem": cmd_mem,
    "disk": cmd_disk,
    "net": cmd_net,
    "bw": cmd_bw,
    "xray": cmd_xray,
    "uptime": cmd_uptime,
    "add": cmd_add,
    "cap": cmd_cap,
    "kick": cmd_kick,
    "on": cmd_on,
    "off": cmd_off,
}


def handle(text: str) -> str:
    cmd, args, flags = parse_message(text)
    if not cmd:
        stripped = text.strip()
        rows = nodes(True)
        names = {row["name"]: row for row in rows}
        key = util.normalize_node_name(stripped)
        if key in names:
            return formatters.node_detail(names[key])
        if stripped.startswith("/") or (stripped and " " not in stripped and len(stripped) <= 16):
            return "没看懂。发 /help"
        return ""
    return HANDLERS[cmd](args, flags)


def register_bot(token: str) -> None:
    report.telegram_call(token, "deleteWebhook", {"drop_pending_updates": False}, timeout=15)
    report.telegram_call(token, "setMyCommands", {"commands": COMMANDS, "language_code": "zh"}, timeout=15)
    report.telegram_call(token, "setMyCommands", {"commands": COMMANDS}, timeout=15)


def load_offset() -> int:
    path = util.state_dir() / "telegram.offset"
    if not path.is_file():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return 0


def save_offset(value: int) -> None:
    path = util.state_dir() / "telegram.offset"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".offset.tmp")
    tmp.write_text(str(value) + "\n", encoding="utf-8")
    tmp.replace(path)


def skip_backlog(token: str) -> int:
    latest = 0
    while True:
        payload: dict[str, Any] = {"timeout": 0, "limit": 100, "allowed_updates": ["message"]}
        if latest:
            payload["offset"] = latest
        body = report.telegram_call(token, "getUpdates", payload, timeout=15)
        results = body.get("result") or []
        if not results:
            return latest
        latest = int(results[-1]["update_id"]) + 1


def poll(token: str, offset: int) -> tuple[int, list[dict[str, Any]]]:
    payload: dict[str, Any] = {"timeout": 50, "limit": 20, "allowed_updates": ["message"]}
    if offset:
        payload["offset"] = offset
    try:
        body = report.telegram_call(token, "getUpdates", payload, timeout=60)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"poll retry: {exc}", flush=True)
        time.sleep(2)
        return offset, []
    results = body.get("result") or []
    next_offset = offset
    if results:
        next_offset = int(results[-1]["update_id"]) + 1
    return next_offset, results


def main() -> int:
    token = util.env("TELEGRAM_BOT_TOKEN")
    chat_id = util.env("TELEGRAM_CHAT_ID")
    register_bot(token)
    offset = load_offset()
    if offset <= 0:
        offset = skip_backlog(token)
        if offset:
            save_offset(offset)
    state_path = util.state_dir() / "state.json"
    state = util.load_json(state_path)
    if not state.get("fleet_hello_sent"):
        send_text(token, chat_id, HELP + "\n\n机群版已启用，先发 /all 看当前这台。")
        state["fleet_hello_sent"] = True
        util.save_json(state_path, state)
    print(f"traffic-bot fleet hub={hub_base()}", flush=True)
    last_cmd = 0.0
    while True:
        offset, updates = poll(token, offset)
        if offset:
            save_offset(offset)
        for update in updates:
            message = update.get("message") or {}
            chat = str((message.get("chat") or {}).get("id") or "")
            if chat != str(chat_id):
                continue
            now = time.monotonic()
            if now - last_cmd < 1.0:
                continue
            last_cmd = now
            text = message.get("text") or ""
            try:
                reply = handle(text)
            except KeyError:
                reply = "没有这个节点。先 /nodes 看覆盖范围。"
            except ValueError as exc:
                reply = str(exc)
            except Exception:
                traceback.print_exc()
                reply = "查询失败，看了一下 Hub 日志。"
            if reply:
                send_text(token, chat_id, reply)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
