#!/usr/bin/env python3
"""Telegram fleet bot. Talks to local hub; only the configured chat can control it."""
from __future__ import annotations

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
    {"command": "net", "description": "网卡当前吞吐：/net 或 /net 名字"},
    {"command": "bw", "description": "公网测速：/bw 名字 [秒]"},
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

KIND_LABEL = {
    "cpu": "CPU",
    "mem": "内存",
    "disk": "磁盘",
    "net": "网速",
    "today": "今日流量",
    "xray": "Xray",
    "uptime": "运行时间",
    "traffic": "详情",
}

HELP = (
    "🤖 <b>机群监控</b>\n\n"
    "点消息下面的按钮就能看机器，不用打节点名。\n"
    "/all 总览，/nodes 列表，点一台进详情。\n\n"
    "<b>查看</b>（仍可手打）\n"
    "/go 名字  一台详情\n"
    "/cpu /mem /disk /today /traffic /net /xray /uptime\n"
    "可加名字或 all，例如 <code>/cpu all</code>\n\n"
    "<b>网速</b>（读网卡当前吞吐，不打流）\n"
    "<code>/net sg</code>  默认采 3 秒\n"
    "<code>/net all</code>\n\n"
    "<b>公网测速</b>（对 Cloudflare 拉流/推流）\n"
    "<code>/bw sg</code>  默认各 3 秒\n"
    "<code>/bw sg 8</code>  1–15 秒\n"
    "<code>/bw all</code>\n\n"
    "<b>覆盖范围</b>（手打，避免误触）\n"
    "<code>/add hk cap=2T reset=27</code>\n"
    "<code>/cap hk 500G</code>\n"
    "<code>/off hk</code>  暂时不看\n"
    "<code>/on hk</code>\n"
    "<code>/kick hk</code>  踢出，需再 /add 才会重新纳入\n\n"
    "额度支持 500G / 1T / 2T / unlimited。日报仍会自动发。"
)

ALLOWED_UPDATES = ["message", "callback_query"]
METRIC_KINDS = set(KIND_LABEL)


class Reply:
    def __init__(self, text: str, markup: Optional[dict[str, Any]] = None) -> None:
        self.text = text
        self.markup = markup


def hub_base() -> str:
    return util.env_opt("HUB_URL", "https://127.0.0.1:8788").rstrip("/")


def fleet_token() -> str:
    return util.env("FLEET_TOKEN")


def public_hub() -> str:
    return util.env_opt("FLEET_PUBLIC_URL") or f"https://{report.host_label()}:{util.env_int('HUB_PORT', 8788)}"


def hub_call(
    method: str,
    path: str,
    payload: Optional[dict[str, Any]] = None,
    timeout: int = 8,
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


def clip_text(text: str) -> str:
    if len(text) > 3900:
        return text[:3890] + "\n…"
    return text


def _btn(text: str, data: str) -> dict[str, str]:
    if len(data.encode("utf-8")) > 64:
        raise ValueError(f"callback_data too long: {data}")
    return {"text": text, "callback_data": data}


def _markup(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def _node_mark(row: dict[str, Any]) -> str:
    if not row.get("enabled", True):
        return "○"
    if row.get("online"):
        return "●"
    return "○"


def _node_button_label(row: dict[str, Any]) -> str:
    extra = " 停" if not row.get("enabled", True) else ""
    return f"{_node_mark(row)} {row['name']}{extra}"


def home_keyboard(rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    rows = rows if rows is not None else nodes(True)
    keyboard: list[list[dict[str, str]]] = []
    for row in rows:
        keyboard.append([_btn(_node_button_label(row), f"go:{row['name']}")])
    keyboard.append([_btn("刷新", "home")])
    return _markup(keyboard)


def help_keyboard() -> dict[str, Any]:
    return _markup(
        [
            [_btn("机群总览", "home"), _btn("节点列表", "nodes")],
        ]
    )


def pick_keyboard(action: str, rows: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    rows = rows if rows is not None else nodes(False)
    keyboard: list[list[dict[str, str]]] = []
    for row in rows:
        if not row.get("enabled", True):
            continue
        keyboard.append([_btn(_node_button_label(row), f"{action}:{row['name']}")])
    if action.startswith("m:") or action == "bw":
        keyboard.append([_btn("全部", f"{action}:all")])
    keyboard.append([_btn("« 总览", "home")])
    return _markup(keyboard)


def node_keyboard(name: str) -> dict[str, Any]:
    name = util.normalize_node_name(name)
    return _markup(
        [
            [_btn("今日", f"m:today:{name}"), _btn("CPU", f"m:cpu:{name}"), _btn("内存", f"m:mem:{name}")],
            [_btn("磁盘", f"m:disk:{name}"), _btn("网速", f"m:net:{name}"), _btn("Xray", f"m:xray:{name}")],
            [_btn("运行时间", f"m:uptime:{name}"), _btn("测速", f"bw:{name}")],
            [_btn("« 总览", "home"), _btn("刷新", f"go:{name}")],
        ]
    )


def as_reply(value: Any) -> Reply:
    if isinstance(value, Reply):
        return value
    return Reply(str(value) if value is not None else "")


def default_target(args: list[str], rows: Optional[list[dict[str, Any]]] = None) -> str:
    if args and args[0].lower() != "all":
        return util.normalize_node_name(args[0])
    rows = rows if rows is not None else nodes(False)
    enabled = [row["name"] for row in rows if row.get("enabled", True)]
    if len(enabled) == 1:
        return enabled[0]
    raise ValueError("PICK")


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


def parse_callback(data: str) -> tuple[str, list[str]]:
    raw = (data or "").strip()
    if raw in {"home", "all"}:
        return "home", []
    if raw in {"nodes", "help"}:
        return raw, []
    if raw.startswith("go:"):
        return "go", [raw.split(":", 1)[1]]
    if raw.startswith("bw:"):
        return "bw", [raw.split(":", 1)[1]]
    if raw.startswith("m:"):
        parts = raw.split(":")
        if len(parts) >= 3 and parts[1] in METRIC_KINDS:
            return "metric", [parts[1], parts[2]]
    return "", []


def telegram_ok_to_ignore(exc: Exception) -> bool:
    text = str(exc).lower()
    return "message is not modified" in text


def _with_stamp(text: str) -> str:
    ts = time.strftime("%H:%M:%S", time.gmtime())
    return text.rstrip() + f"\n\n<i>{ts} UTC</i>"


def loading_text(data: str) -> str:
    cmd, args = parse_callback(data)
    if cmd == "bw":
        target = args[0] if args else ""
        if target == "all":
            return (
                "⏳ <b>正在测速</b>\n\n"
                "全部机器对 Cloudflare 下载+上传，大约十几秒。\n"
                "完成后这条消息会换成结果。"
            )
        name = report.h(util.normalize_node_name(target) if target else "节点")
        return (
            f"⏳ <b>正在测速</b> <code>{name}</code>\n\n"
            f"对 Cloudflare 下载、上传各约 3 秒，请稍候。\n"
            f"完成后这条消息会换成结果。"
        )
    if cmd == "metric" and args[:1] == ["net"]:
        target = args[1] if len(args) > 1 else ""
        if target == "all":
            return "⏳ <b>正在读网卡</b>\n\n全部机器各采约 3 秒，不打流。"
        name = report.h(util.normalize_node_name(target) if target else "节点")
        return (
            f"⏳ <b>正在读网卡</b> <code>{name}</code>\n\n"
            f"采样约 3 秒当前吞吐，不打流。"
        )
    if cmd == "metric" and args:
        label = KIND_LABEL.get(args[0], args[0])
        target = args[1] if len(args) > 1 else ""
        extra = f" <code>{report.h(target)}</code>" if target and target != "all" else ""
        return f"⏳ 正在查询{report.h(label)}{extra}…"
    labels = {
        "home": "正在刷新总览",
        "nodes": "正在拉取节点",
        "help": "正在打开帮助",
        "go": "正在打开详情",
    }
    return f"⏳ {labels.get(cmd, '处理中')}…"


def toast_for_reply(text: str, status: str) -> str:
    if status == "failed":
        return "更新失败"
    if status == "unchanged":
        return "已是最新"
    stripped = text.lstrip()
    if stripped.startswith("⏱") or "超时" in stripped[:80]:
        return "超时"
    if stripped.startswith("❌") or stripped.startswith("查询失败"):
        return "失败"
    return ""


def send_reply(
    token: str,
    chat_id: str,
    reply: Reply,
    edit_message_id: Optional[int] = None,
) -> str:
    """Send or edit. Returns 'edited', 'sent', 'unchanged', or 'failed'."""
    text = clip_text(_with_stamp(reply.text or "（空）"))
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply.markup is not None:
        payload["reply_markup"] = reply.markup
    if edit_message_id:
        edit_payload = dict(payload)
        edit_payload["message_id"] = int(edit_message_id)
        try:
            report.telegram_call(token, "editMessageText", edit_payload, timeout=15)
            return "edited"
        except RuntimeError as exc:
            if telegram_ok_to_ignore(exc):
                return "unchanged"
            print(f"editMessageText failed: {exc}", flush=True)
    try:
        report.telegram_call(token, "sendMessage", payload, timeout=15)
        return "sent"
    except Exception as exc:
        print(f"sendMessage failed: {exc}", flush=True)
        try:
            plain = dict(payload)
            plain.pop("parse_mode", None)
            report.telegram_call(token, "sendMessage", plain, timeout=15)
            return "sent"
        except Exception as exc2:
            print(f"sendMessage plain failed: {exc2}", flush=True)
            return "failed"


def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    if not callback_id:
        return
    payload: dict[str, Any] = {"callback_query_id": callback_id, "cache_time": 0}
    if text:
        payload["text"] = text[:180]
    try:
        report.telegram_call(token, "answerCallbackQuery", payload, timeout=10)
    except Exception as exc:
        print(f"answerCallbackQuery: {exc}", flush=True)


def allowed_chat(obj: dict[str, Any], chat_id: str) -> bool:
    want = str(chat_id).strip()
    chat = str(((obj.get("chat") or {}).get("id")) or "")
    if chat and chat == want:
        return True
    sender = str(((obj.get("from") or {}).get("id")) or "")
    return bool(sender) and sender == want


def view_home() -> Reply:
    rows = nodes(False)
    return Reply(formatters.fleet_overview(rows), home_keyboard(rows))


def view_nodes() -> Reply:
    rows = nodes(True)
    return Reply(formatters.fleet_overview(rows, title="📋 覆盖范围"), home_keyboard(rows))


def view_help() -> Reply:
    return Reply(HELP, help_keyboard())


def view_go(name: str) -> Reply:
    name = util.normalize_node_name(name)
    return Reply(formatters.node_detail(node_named(name)), node_keyboard(name))


def view_metric(kind: str, target: str) -> Reply:
    if kind == "net":
        if target == "all":
            return cmd_nic(["all"], {})
        return cmd_nic([util.normalize_node_name(target)], {})
    rows = nodes(False)
    if target == "all":
        chunks = [formatters.metric_block(row, kind) for row in rows]
        text = "\n\n".join(chunks) if chunks else "没有在线覆盖的节点。"
        return Reply(text, home_keyboard(rows))
    name = util.normalize_node_name(target)
    if kind == "traffic":
        return view_go(name)
    return Reply(formatters.metric_block(node_named(name), kind), node_keyboard(name))


def view_pick(kind_or_bw: str) -> Reply:
    rows = nodes(False)
    enabled = [row for row in rows if row.get("enabled", True)]
    if not enabled:
        return Reply("没有节点。", home_keyboard(rows))
    if len(enabled) == 1:
        name = enabled[0]["name"]
        if kind_or_bw == "bw":
            return cmd_bw([name], {})
        if kind_or_bw in {"go", "traffic"}:
            return view_go(name)
        return view_metric(kind_or_bw, name)
    if kind_or_bw == "bw":
        return Reply("测哪一台的带宽？", pick_keyboard("bw", enabled))
    if kind_or_bw in {"go", "traffic"}:
        return Reply("看哪一台？", pick_keyboard("go", enabled))
    label = KIND_LABEL.get(kind_or_bw, kind_or_bw)
    return Reply(f"看哪一台的{label}？", pick_keyboard(f"m:{kind_or_bw}", enabled))


def cmd_help(_: list[str], __: dict[str, str]) -> Reply:
    return view_help()


def cmd_all(_: list[str], __: dict[str, str]) -> Reply:
    return view_home()


def cmd_nodes(_: list[str], __: dict[str, str]) -> Reply:
    return view_nodes()


def cmd_go(args: list[str], _: dict[str, str]) -> Reply:
    try:
        name = default_target(args)
    except ValueError:
        return view_pick("go")
    return view_go(name)


def _one_or_all(args: list[str], kind: str) -> Reply:
    if args and args[0].lower() == "all":
        return view_metric(kind, "all")
    try:
        name = default_target(args)
    except ValueError:
        return view_pick(kind)
    return view_metric(kind, name)


def cmd_traffic(args: list[str], flags: dict[str, str]) -> Reply:
    if args and args[0].lower() == "all":
        return cmd_all(args, flags)
    return cmd_go(args, flags)


def cmd_cpu(args: list[str], _: dict[str, str]) -> Reply:
    return _one_or_all(args, "cpu")


def cmd_mem(args: list[str], _: dict[str, str]) -> Reply:
    return _one_or_all(args, "mem")


def cmd_disk(args: list[str], _: dict[str, str]) -> Reply:
    return _one_or_all(args, "disk")


def cmd_net(args: list[str], _: dict[str, str]) -> Reply:
    return _one_or_all(args, "net")


def cmd_today(args: list[str], _: dict[str, str]) -> Reply:
    return _one_or_all(args, "today")


def cmd_xray(args: list[str], _: dict[str, str]) -> Reply:
    return _one_or_all(args, "xray")


def cmd_uptime(args: list[str], _: dict[str, str]) -> Reply:
    return _one_or_all(args, "uptime")


def _format_job_result(name: str, job_type: str, data: dict[str, Any]) -> str:
    if job_type == "nic":
        row: dict[str, Any] = {}
        try:
            row = node_named(name)
        except KeyError:
            pass
        return formatters.nic_result(name, data, row)
    return formatters.bw_result(name, data)


def _await_job(name: str, job_id: str, job_type: str) -> str:
    label = "网卡采样" if job_type == "nic" else "测速"
    try:
        job = hub_call("GET", f"/v1/jobs/{job_id}?wait=1", timeout=55).get("job") or {}
    except Exception as exc:
        return (
            f"❌ <b>{report.h(name)}</b> {label}失败\n\n"
            f"{report.h(exc)}"
        )
    status = str(job.get("status") or "unknown")
    if status != "ok":
        err = str(job.get("error") or status)
        if status in {"timeout", "queued", "running"} or err == "timeout":
            return (
                f"⏱ <b>{report.h(name)}</b> {label}超时\n\n"
                f"超过约 50 秒还没有结果。节点可能掉线或正在忙，请再点一次。"
            )
        return (
            f"❌ <b>{report.h(name)}</b> {label}失败\n\n"
            f"{report.h(err)}"
        )
    return _format_job_result(name, job_type, job.get("result") or {})


def _run_job(name: str, job_type: str, seconds: float) -> str:
    label = "网卡采样" if job_type == "nic" else "测速"
    try:
        created = hub_call(
            "POST",
            "/v1/jobs",
            {"node": name, "type": job_type, "params": {"seconds": seconds}},
        )
    except Exception as exc:
        return (
            f"❌ <b>{report.h(name)}</b> 无法提交{label}任务\n\n"
            f"{report.h(exc)}"
        )
    job_id = created.get("id")
    if not job_id:
        return f"❌ <b>{report.h(name)}</b> 无法提交{label}任务\n\nHub 没有返回任务 id。"
    return _await_job(name, str(job_id), job_type)


def _run_jobs_all(job_type: str, seconds: float) -> Reply:
    label = "网卡采样" if job_type == "nic" else "测速"
    rows = [row for row in nodes(False) if row.get("enabled", True)]
    if not rows:
        return Reply("没有节点。", home_keyboard([]))
    started: list[tuple[str, str]] = []
    chunks: list[str] = []
    for row in rows:
        try:
            created = hub_call(
                "POST",
                "/v1/jobs",
                {"node": row["name"], "type": job_type, "params": {"seconds": seconds}},
            )
            job_id = created.get("id")
            if not job_id:
                chunks.append(
                    f"❌ <b>{report.h(row['name'])}</b> 无法提交{label}任务\n\nHub 没有返回任务 id。"
                )
                continue
            started.append((row["name"], str(job_id)))
        except Exception as exc:
            chunks.append(
                f"❌ <b>{report.h(row['name'])}</b> 无法提交{label}任务\n\n{report.h(exc)}"
            )
    chunks.extend(_await_job(name, job_id, job_type) for name, job_id in started)
    text = "\n\n".join(chunks) if chunks else f"没有可{label}的节点。"
    return Reply(text, home_keyboard(rows))


def cmd_bw(args: list[str], _: dict[str, str]) -> Reply:
    seconds = 3.0
    rest = list(args)
    if rest and re.fullmatch(r"\d+(?:\.\d+)?", rest[-1]):
        seconds = float(rest.pop())
    seconds = max(1.0, min(15.0, seconds))
    if rest and rest[0].lower() == "all":
        return _run_jobs_all("bw", seconds)
    try:
        name = default_target(rest)
    except ValueError:
        return view_pick("bw")
    return Reply(_run_job(name, "bw", seconds), node_keyboard(name))


def cmd_nic(args: list[str], _: dict[str, str]) -> Reply:
    seconds = 3.0
    rest = list(args)
    if rest and re.fullmatch(r"\d+(?:\.\d+)?", rest[-1]):
        seconds = float(rest.pop())
    seconds = max(1.0, min(15.0, seconds))
    if rest and rest[0].lower() == "all":
        return _run_jobs_all("nic", seconds)
    try:
        name = default_target(rest)
    except ValueError:
        return view_pick("net")
    return Reply(_run_job(name, "nic", seconds), node_keyboard(name))


def cmd_add(args: list[str], flags: dict[str, str]) -> Reply:
    if not args:
        return Reply("用法：<code>/add hk cap=2T reset=27</code>", help_keyboard())
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
    return Reply(
        formatters.add_help(util.normalize_node_name(name), public_hub(), util.format_cap(cap), reset_day),
        home_keyboard(),
    )


def cmd_cap(args: list[str], flags: dict[str, str]) -> Reply:
    if len(args) < 2:
        return Reply("用法：<code>/cap hk 2T</code> 或 <code>/cap hk unlimited</code>", help_keyboard())
    cap = util.parse_cap(args[1])
    hub_call("POST", "/v1/nodes", {"action": "cap", "name": args[0], "cap_bytes": cap})
    name = util.normalize_node_name(args[0])
    return Reply(f"{report.h(name)} 额度已改为 {report.h(util.format_cap(cap))}", node_keyboard(name))


def cmd_kick(args: list[str], _: dict[str, str]) -> Reply:
    if not args:
        return Reply("用法：<code>/kick hk</code>", help_keyboard())
    hub_call("POST", "/v1/nodes", {"action": "kick", "name": args[0]})
    return Reply(
        f"已踢出 <code>{report.h(util.normalize_node_name(args[0]))}</code>。再接入需要重新 /add。",
        home_keyboard(),
    )


def cmd_on(args: list[str], _: dict[str, str]) -> Reply:
    if not args:
        return Reply("用法：<code>/on hk</code>", help_keyboard())
    hub_call("POST", "/v1/nodes", {"action": "enable", "name": args[0]})
    name = util.normalize_node_name(args[0])
    return Reply(f"已启用 <code>{report.h(name)}</code>", node_keyboard(name))


def cmd_off(args: list[str], _: dict[str, str]) -> Reply:
    if not args:
        return Reply("用法：<code>/off hk</code>", help_keyboard())
    hub_call("POST", "/v1/nodes", {"action": "disable", "name": args[0]})
    name = util.normalize_node_name(args[0])
    return Reply(
        f"已停用 <code>{report.h(name)}</code>（机器还在，只是不汇总）",
        home_keyboard(),
    )


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


def handle(text: str) -> Reply:
    cmd, args, flags = parse_message(text)
    if not cmd:
        stripped = text.strip()
        rows = nodes(True)
        names = {row["name"]: row for row in rows}
        key = util.normalize_node_name(stripped)
        if key in names:
            return view_go(key)
        if stripped.startswith("/") or (stripped and " " not in stripped and len(stripped) <= 16):
            return Reply("没看懂。发 /help 或点按钮。", help_keyboard())
        return Reply("")
    return as_reply(HANDLERS[cmd](args, flags))


def handle_callback(data: str) -> Reply:
    cmd, args = parse_callback(data)
    if cmd == "home":
        return view_home()
    if cmd == "nodes":
        return view_nodes()
    if cmd == "help":
        return view_help()
    if cmd == "go":
        if not args or not args[0]:
            return view_pick("go")
        return view_go(args[0])
    if cmd == "metric":
        kind, target = args[0], args[1]
        if target == "all":
            return view_metric(kind, "all")
        if not util.valid_node_name(util.normalize_node_name(target)):
            raise KeyError(target)
        return view_metric(kind, target)
    if cmd == "bw":
        target = args[0] if args else ""
        if not target:
            return view_pick("bw")
        if target == "all":
            return cmd_bw(["all"], {})
        if not util.valid_node_name(util.normalize_node_name(target)):
            raise KeyError(target)
        return cmd_bw([target], {})
    return Reply("没看懂这个按钮。", help_keyboard())


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
        payload: dict[str, Any] = {"timeout": 0, "limit": 100, "allowed_updates": ALLOWED_UPDATES}
        if latest:
            payload["offset"] = latest
        body = report.telegram_call(token, "getUpdates", payload, timeout=15)
        results = body.get("result") or []
        if not results:
            return latest
        latest = int(results[-1]["update_id"]) + 1


def poll(token: str, offset: int) -> tuple[int, list[dict[str, Any]]]:
    payload: dict[str, Any] = {"timeout": 50, "limit": 20, "allowed_updates": ALLOWED_UPDATES}
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


def _dispatch(text: str) -> Reply:
    try:
        return handle(text)
    except KeyError:
        return Reply("没有这个节点。先点节点列表看覆盖范围。", home_keyboard())
    except ValueError as exc:
        if str(exc) == "PICK":
            return view_pick("go")
        return Reply(str(exc), help_keyboard())
    except Exception:
        traceback.print_exc()
        return Reply("❌ 查询失败，请再点一次。", help_keyboard())


def _dispatch_callback(data: str) -> Reply:
    try:
        return handle_callback(data)
    except KeyError:
        return Reply("没有这个节点。先点节点列表看覆盖范围。", home_keyboard())
    except ValueError as exc:
        return Reply(str(exc), help_keyboard())
    except Exception:
        traceback.print_exc()
        return Reply("❌ 查询失败，请再点一次。", help_keyboard())


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
        send_reply(token, chat_id, Reply(HELP + "\n\n机群版已启用，先点「机群总览」。", help_keyboard()))
        state["fleet_hello_sent"] = True
        util.save_json(state_path, state)
    print(f"traffic-bot fleet hub={hub_base()}", flush=True)
    last_cmd = 0.0
    while True:
        offset, updates = poll(token, offset)
        if offset:
            save_offset(offset)
        for update in updates:
            callback = update.get("callback_query")
            if callback:
                message = callback.get("message") or {}
                if not allowed_chat(message, chat_id) and not allowed_chat(callback, chat_id):
                    continue
                qid = str(callback.get("id") or "")
                now = time.monotonic()
                if now - last_cmd < 0.35:
                    answer_callback(token, qid, "稍等")
                    continue
                last_cmd = now
                data = str(callback.get("data") or "")
                msg_id = message.get("message_id")
                edit_id = int(msg_id) if msg_id else None
                toast = ""
                t0 = time.monotonic()
                try:
                    send_reply(
                        token,
                        chat_id,
                        Reply(loading_text(data)),
                        edit_message_id=edit_id,
                    )
                    reply = _dispatch_callback(data)
                    t1 = time.monotonic()
                    status = "failed"
                    if reply.text:
                        status = send_reply(token, chat_id, reply, edit_message_id=edit_id)
                    else:
                        status = send_reply(
                            token,
                            chat_id,
                            Reply("❌ 没有返回内容，请再点一次。", help_keyboard()),
                            edit_message_id=edit_id,
                        )
                    t2 = time.monotonic()
                    shown = reply.text or "❌ 没有返回内容"
                    print(f"cb {data} hub={t1-t0:.3f}s tg={t2-t1:.3f}s {status}", flush=True)
                    toast = toast_for_reply(shown, status)
                except Exception:
                    traceback.print_exc()
                    toast = "失败"
                    try:
                        send_reply(
                            token,
                            chat_id,
                            Reply("❌ 查询失败，请再点一次。", help_keyboard()),
                            edit_message_id=edit_id,
                        )
                    except Exception:
                        pass
                answer_callback(token, qid, toast)
                continue
            message = update.get("message") or {}
            if not allowed_chat(message, chat_id):
                continue
            now = time.monotonic()
            if now - last_cmd < 1.0:
                continue
            last_cmd = now
            text = message.get("text") or ""
            reply = _dispatch(text)
            if reply.text:
                try:
                    send_reply(token, chat_id, reply)
                except Exception:
                    traceback.print_exc()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
