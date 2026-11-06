#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb-conv —— 读取 / 检索 / 导出 WorkBuddy 本地会话记录。

数据来源（数据库只读，正文来自本地 JSONL）：
  索引  <WORKBUDDY_DIR>/workbuddy.db → sessions 表
  正文  <WORKBUDDY_DIR>/projects/*/<sessionId>.jsonl

子命令：
  find   <关键词> [--deep] [-n N] [--json]  按标题查找；仅标题未命中时 --deep 才搜索正文
  list   [-n N] [--cwd 关键字] [--json]      列出最近会话
  show   <id前缀> [--trace] [--out 文件]      导出某次会话正文（默认只正文，不含工具调用）
  search <关键词> [-n N] [--all] [--json]     跨项目搜索正文及可选事件文本
  title  <id前缀>                            只看标题/元信息

用法示例：
  python scripts/wb-conv.py find keyword
  python scripts/wb-conv.py find keyword --deep
  python scripts/wb-conv.py show abcdef12
  python scripts/wb-conv.py search keyword -n 10
"""
import argparse
import datetime as dt
from collections import deque
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from urllib.parse import quote
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from wb_catalog import load_catalog

DEFAULT_WB_HOME = os.path.join(os.path.expanduser("~"), ".workbuddy")
WB_HOME = None
DB_PATH = None
PROJECTS_DIR = None


def set_home(cli_home=None):
    """Resolve data location: --home, WORKBUDDY_DIR, then ~/.workbuddy."""
    global WB_HOME, DB_PATH, PROJECTS_DIR
    selected = cli_home or os.environ.get("WORKBUDDY_DIR") or DEFAULT_WB_HOME
    WB_HOME = os.path.abspath(os.path.expanduser(os.path.expandvars(selected)))
    DB_PATH = os.path.join(WB_HOME, "workbuddy.db")
    PROJECTS_DIR = os.path.join(WB_HOME, "projects")


def jsonl_paths(sid):
    return sorted(glob.glob(os.path.join(PROJECTS_DIR, "*", f"{sid}.jsonl")))


def catalog():
    return load_catalog(WB_HOME)


def catalog_rows(include_deleted=True):
    return [s for s in catalog()["sessions"] if include_deleted or not s["deleted"]]


def pick_session(prefix, require_path=True):
    rows = [s for s in catalog_rows() if s["id"].startswith(prefix)]
    if not rows:
        sys.exit(f"没有匹配 {prefix!r} 的会话")
    exact = [s for s in rows if s["id"] == prefix]
    if exact:
        rows = exact
    if len(rows) > 1:
        print("前缀有歧义，命中多个，请给更长的前缀：", file=sys.stderr)
        for row in rows[:20]:
            print(f"  {row['id']}", file=sys.stderr)
        sys.exit(2)
    row = rows[0]
    if len(row["paths"]) > 1:
        sys.exit(f"会话 {row['id']} 存在多个项目副本（JSONL），无法安全选择: " + ", ".join(row["paths"]))
    if require_path and not row["paths"]:
        sys.exit(f"会话 {row['id']} 正文文件缺失")
    return row

SYS_BLOCK_RE = re.compile(r"<system-reminder\b.*?</system-reminder>", re.S | re.I)
ANY_TAG_BLOCK_RE = re.compile(r"<(identity_context|project_context|project_guidance|"
                              r"connector-status|user_info|memory|memory_and_skills_reminder|"
                              r"working_memory_content|user_memory|agent_skills|response_language|"
                              r"craft_mode|assistant_tail)\b.*?</\1>", re.S | re.I)
USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S | re.I)
ASSISTANT_TAIL_RE = re.compile(r"<assistant_tail>(.*?)</assistant_tail>", re.S | re.I)

TEXT_PART_TYPES = {"input_text", "output_text", "text"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_MIME_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                       "image/webp": ".webp", "image/bmp": ".bmp"}


def ts(ms):
    if not ms:
        return "-"
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def open_db():
    if not os.path.exists(DB_PATH):
        sys.exit(f"找不到数据库: {DB_PATH}")
    return sqlite3.connect(f"{Path(DB_PATH).as_uri()}?mode=ro", uri=True)


def session_jsonl(sid):
    """跨所有项目目录按 sessionId 找 jsonl，绕开目录 slug 规则。"""
    hits = jsonl_paths(sid)
    if len(hits) > 1:
        sys.exit(f"会话 {sid} 有多个 JSONL 副本，无法安全选择: " + ", ".join(hits))
    return hits[0] if hits else None


def resolve_prefix(prefix):
    """支持 id 前缀；返回 (完整 id, jsonl 路径)。"""
    row = pick_session(prefix)
    return row["id"], row["paths"][0]


AUTOMATION_WRAPPER_MARK = "You MUST follow these steps in order"


def clean_user_text(t):
    """剥掉 host 注入的上下文块，尽量保留用户实际输入。

    普通对话提取 user_query 内容。定时任务的包装边界不稳定，因此完整保留正文并加提示。
    """
    ms = list(USER_QUERY_RE.finditer(t))
    if not ms:
        return strip_injected(t)

    body = ms[-1].group(1)
    if AUTOMATION_WRAPPER_MARK in body:
        body = strip_injected_tags(body)
        return f"[定时任务自动 prompt，内容完整保留，可能包含执行包装]\n\n{body}"
    return strip_injected(body.strip())


TAIL_TAG_RE = re.compile(r"</?(system-reminder|automation_system_reminder|memory_and_skills_reminder|"
                         r"user_query|identity_context|project_context|project_guidance|craft_mode)\b[^>]*>",
                         re.I)


def strip_injected(t):
    # 嵌套块要从内往外剥：automation_system_reminder 常常嵌在 system-reminder 里，
    # 先删外层会让 SYS_BLOCK_RE 在内部第一个 </system-reminder> 处提前收尾，留下残渣。
    t = re.sub(r"<automation_system_reminder\b.*?</automation_system_reminder>", "", t, flags=re.S | re.I)
    t = SYS_BLOCK_RE.sub("", t)
    t = ANY_TAG_BLOCK_RE.sub("", t)
    t = TAIL_TAG_RE.sub("", t)
    # 注入块被切碎后会留下孤立的标点行（如单独一个 ":"），清掉
    lines = []
    for ln in t.splitlines():
        s = ln.strip()
        if s and len(s) <= 2 and not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", s):
            continue
        if s:
            lines.append(ln)
    return re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip()


def strip_injected_tags(t):
    """Remove known injected blocks while preserving all surrounding text and whitespace."""
    t = re.sub(r"<automation_system_reminder\b.*?</automation_system_reminder>", "", t, flags=re.S | re.I)
    t = SYS_BLOCK_RE.sub("", t)
    t = ANY_TAG_BLOCK_RE.sub("", t)
    return TAIL_TAG_RE.sub("", t)


def iter_message_records(path, prefilter=None, raw=False):
    """产出消息文本及结构化图片引用；JSONL仍只解析一次。"""
    esc = json.dumps(prefilter, ensure_ascii=True)[1:-1] if prefilter else None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if prefilter and prefilter not in line and esc not in line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict) or d.get("type") != "message":
                continue
            role = d.get("role") or "?"
            parts = d.get("content") or []
            buf = []
            images = []
            if isinstance(parts, str):
                buf.append(parts)
            elif isinstance(parts, list):
                for c in parts:
                    if isinstance(c, dict) and c.get("type") in TEXT_PART_TYPES:
                        buf.append(c.get("text") or "")
                    elif isinstance(c, dict) and c.get("type") == "image_blob_ref":
                        images.append((c.get("blob_path"), c.get("mime"), None))
                    elif isinstance(c, dict) and c.get("type") in {"input_image", "image_gen_tool_result"}:
                        images.append((None, None, c.get("type")))
            text = "\n".join(x for x in buf if isinstance(x, str) and x).strip()
            if role == "user" and text:
                text = text if raw else clean_user_text(text)
            if text or images:
                yield role, text, d.get("timestamp"), images


def iter_messages(path, prefilter=None, raw=False):
    """产出 (role, text, timestamp)，兼容既有检索调用方。

    prefilter 给了关键词时，先做原始行字符串预筛再 json.loads ——
    先按原始行筛选关键词，再解析可能命中的 JSON 行。
    中文若被写成 \\uXXXX 转义（ensure_ascii），明文匹配会漏，所以两种形式都试。
    """
    for role, text, timestamp, _ in iter_message_records(path, prefilter=prefilter, raw=raw):
        if text:
            yield role, text, timestamp


def attachment_dir_for(out_path):
    out = Path(out_path)
    return out.parent / (out.stem + "-attachments")


def _inside(path, root):
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)),
                                   os.path.normcase(os.path.realpath(root))]) == os.path.normcase(os.path.realpath(root))
    except ValueError:
        return False


def _validate_attachment_dir(directory):
    """Reject attachment outputs resolving into WorkBuddy storage before any writes."""
    if _inside(directory, WB_HOME):
        sys.exit("拒绝将图片附件写入 WorkBuddy 数据目录")
    if os.path.lexists(directory) and not os.path.isdir(directory):
        sys.exit("图片附件目录已存在且不是目录")


def _write_temp_file(directory, prefix, data):
    """Write bytes under a unique sibling name, keeping partial files on failure."""
    try:
        fd, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    except OSError:
        return None, None
    try:
        stream = os.fdopen(fd, "wb")
        with stream as f:
            written = f.write(data)
            if written != len(data):
                raise OSError("short write")
            f.flush()
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None, Path(name)
    return Path(name), None


def _export_image(blob_path, mime, directory, cache):
    """Copy a referenced local image from within WB_HOME; return safe relative link or None."""
    if not isinstance(blob_path, str) or not blob_path.strip():
        return None
    source = Path(os.path.expanduser(os.path.expandvars(blob_path)))
    if not source.is_absolute():
        source = Path(WB_HOME) / source
    source = Path(os.path.abspath(source))
    if not _inside(source, WB_HOME):
        return None
    try:
        if not source.is_file():
            return None
        mime_suffix = IMAGE_MIME_SUFFIXES.get(mime) if isinstance(mime, str) else None
        suffix = mime_suffix or source.suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            return None
        data = source.read_bytes()
        if not data:
            return None
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    key = (digest, suffix)
    if key in cache:
        return cache[key]
    name = f"{digest}{suffix}"
    target = Path(directory) / name
    if os.path.lexists(target):
        try:
            if target.is_symlink() or not target.is_file() or target.read_bytes() != data:
                return None
        except OSError:
            return None
    else:
        temporary, partial = _write_temp_file(directory, ".wb-image-", data)
        if temporary is None:
            return None
        try:
            # Hard-link publication is atomic and fails instead of replacing a concurrent target
            os.link(temporary, target)
        except OSError:
            try:
                if target.is_symlink() or not target.is_file() or target.read_bytes() != data:
                    temporary.unlink()
                    return None
            except OSError:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                return None
        try:
            temporary.unlink()
        except OSError:
            pass
    cache[key] = name
    return name


def iter_trace(path):
    """产出全部事件摘要，供 --trace 使用。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t == "function_call":
                name = d.get("name") or (d.get("function") or {}).get("name") or "?"
                args = d.get("arguments") or (d.get("function") or {}).get("arguments") or ""
                yield f"[tool] {name}  {str(args)[:200]}"
            elif t == "function_call_result":
                body = d.get("output") or d.get("result") or ""
                yield f"[result] {str(body)[:200]}"
            elif t == "reasoning":
                yield f"[think] {str(d.get('text') or d.get('content') or '')[:200]}"


def cmd_list(args):
    data = catalog()
    if data["index"]["status"] != "ok":
        print(f"警告：会话索引{data['index']['status']}（{data['index']['detail']}），按 JSONL 文件列出可发现会话", file=sys.stderr)
    rows = [s for s in data["sessions"] if not s["deleted"]]
    rows = [s for s in rows if not args.cwd or (s["cwd"] and args.cwd.casefold() in s["cwd"].casefold())]
    rows.sort(key=lambda s: (s["updated_ms"] is not None, s["updated_ms"] or 0, s["id"]), reverse=True)
    rows = rows[:args.n]
    out = []
    for s in rows:
        p = s["paths"][0] if len(s["paths"]) == 1 else None
        out.append({
            "id": s["id"], "short": s["id"][:8], "cwd": s["cwd"],
            "title": s["title"] or ("(未索引)" if not s["indexed"] else "(无标题)"),
            "status": s["status"], "updated": ts(s["updated_at"]),
            "updated_ms": s["updated_at"], "mode": s["mode"], "model": s["model"],
            "automation": s["automation"],
            "size_mb": round(os.path.getsize(p) / 1048576, 2) if p else None,
        })

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    print(f"{'更新时间':<17} {'状态':<10} {'ID':<9} {'MB':>6}  标题")
    print("-" * 96)
    for r in out:
        mb = f"{r['size_mb']:.1f}" if r["size_mb"] else "-"
        flag = " [定时]" if r["automation"] else ""
        print(f"{r['updated']:<17} {r['status']:<10} {r['short']:<9} {mb:>6}  {r['title']}{flag}")


def cmd_show(args):
    if args.images and not args.out:
        sys.exit("--images 必须与 --out 一起使用")
    sid, path = resolve_prefix(args.id)
    lines = []
    total = 0
    if args.out:
        if os.path.lexists(args.out) and not os.path.isfile(args.out):
            sys.exit("--out 必须指向普通文件，不能是目录或特殊文件")
        out_parent = os.path.dirname(os.path.abspath(args.out)) or "."
        if os.path.lexists(out_parent) and not os.path.isdir(out_parent):
            sys.exit("--out 的父路径不是目录")
        out_path = os.path.normcase(os.path.realpath(os.path.abspath(args.out)))
        wb_root = os.path.normcase(os.path.realpath(os.path.abspath(WB_HOME)))
        source_path = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        try:
            if os.path.commonpath([out_path, wb_root]) == wb_root:
                sys.exit("拒绝将导出文件写入 WorkBuddy 数据目录")
        except ValueError:
            pass
        if source_path == out_path:
            sys.exit("拒绝覆盖源会话 JSONL")
        if os.path.exists(args.out):
            for protected_path, message in ((path, "拒绝通过硬链接覆盖源会话 JSONL"),
                                            (DB_PATH, "拒绝通过硬链接覆盖 WorkBuddy 数据库")):
                try:
                    if os.path.samefile(args.out, protected_path):
                        sys.exit(message)
                except OSError:
                    continue
        if args.images:
            _validate_attachment_dir(attachment_dir_for(args.out))
        try:
            os.makedirs(out_parent, exist_ok=True)
        except OSError:
            sys.exit("无法创建 --out 的父目录")
        if args.images:
            _validate_attachment_dir(attachment_dir_for(args.out))
    if args.raw:
        args.full = True
    image_cache = {}
    attachment_dir = attachment_dir_for(args.out) if args.images else None
    attachment_created = False
    records = (iter_message_records(path, raw=args.raw) if args.images else
               ((role, text, timestamp, [])
                for role, text, timestamp in iter_messages(path, raw=args.raw)))
    tail = getattr(args, "tail", None)
    if tail is not None:
        if tail < 1:
            sys.exit("--tail 必须是正整数")
        records = deque(records, maxlen=tail)
    for role, text, t, images in records:
        if not text and not images:
            continue
        total += 1
        label = "用户" if role == "user" else ("助手" if role == "assistant" else role)
        lines.append(f"\n### [{label}] {ts(t)}")
        if text:
            lines.append(text if args.full else text[: args.maxlen])
        if not args.full and len(text) > args.maxlen:
            lines.append(f"...(截断，原文 {len(text)} 字，用 --full 看全)")
        for blob_path, mime, unsupported_type in images if args.images else []:
            if unsupported_type:
                lines.append(f"[图片未导出：不支持的图片类型 {unsupported_type}]")
                print("未导出不支持的图片结构", file=sys.stderr)
                continue
            if not attachment_created:
                try:
                    os.makedirs(attachment_dir, exist_ok=True)
                    _validate_attachment_dir(attachment_dir)
                    attachment_created = True
                except OSError:
                    pass
            name = _export_image(blob_path, mime, attachment_dir, image_cache) if attachment_created else None
            if name:
                relative = Path(Path(args.out).stem + "-attachments") / name
                link = quote(relative.as_posix(), safe="/-._~")
                lines.append(f"![图片]({link})")
            else:
                lines.append("[图片未导出：本地图片引用缺失、不可读或格式不支持]")
                print("图片引用未导出（引用缺失、不可读或格式不支持）", file=sys.stderr)
    if args.trace:
        lines.append("\n## 工具调用轨迹")
        for x in iter_trace(path):
            lines.append(x)

    header = f"# 会话 {sid}\n源文件: {path}\n消息数: {total}\n"
    body = header + "\n".join(lines)
    if args.out:
        temporary, partial = _write_temp_file(
            os.path.dirname(os.path.abspath(args.out)) or ".", ".wb-markdown-", body.encode("utf-8"))
        if temporary is None:
            sys.exit("Markdown 导出写入失败，部分临时文件保留用于诊断")
        try:
            os.replace(temporary, args.out)
        except OSError:
            sys.exit("Markdown 导出发布失败，临时文件保留用于诊断")
        print(f"已导出 {len(body)} 字符 → {args.out}（消息 {total} 条）")
    else:
        print(body)


def cmd_title(args):
    row = pick_session(args.id, require_path=False)
    d = {"id": row["id"], "cwd": row["cwd"], "title": row["raw_title"],
         "custom_title": row["custom_title"], "status": row["status"],
         "created_at": row["created_at"], "updated_at": row["updated_at"],
         "updated_ms": row["updated_ms"], "mode": row["mode"], "model": row["model"],
         "permission_mode": row["permission_mode"], "automation": row["automation"],
         "indexed": row["indexed"]}
    d["jsonl"] = row["paths"][0] if len(row["paths"]) == 1 else None
    d["jsonl_paths"] = row["paths"]
    d["size_bytes"] = os.path.getsize(d["jsonl"]) if d["jsonl"] else None
    d["display_title"] = row["title"] or ("(未索引)" if not row["indexed"] else "(无标题)")
    print(json.dumps(d, ensure_ascii=False, indent=1))


def cmd_locate(args):
    rows = [s for s in catalog_rows() if s["id"].startswith(args.id)]
    exact = [s for s in rows if s["id"] == args.id]
    if exact:
        rows = exact
    if len(rows) > 1:
        if args.json:
            print(json.dumps({"matches": [{"id": s["id"], "paths": s["paths"], "indexed": s["indexed"]} for s in rows]}, ensure_ascii=False, indent=1))
        else:
            print("前缀有歧义：")
            for s in rows:
                print(f"{s['id']} indexed={str(s['indexed']).lower()} paths={', '.join(s['paths']) or '(正文缺失)'}")
        return 2
    if not rows:
        sys.exit(f"没有匹配 {args.id!r} 的会话")
    s = rows[0]
    result = {"id": s["id"], "paths": s["paths"], "indexed": s["indexed"], "body_missing": not bool(s["paths"])}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"{s['id']} indexed={str(s['indexed']).lower()} body_missing={str(result['body_missing']).lower()}")
        for path in s["paths"]:
            print(path)
    return 0


def cmd_doctor(args):
    data = catalog()
    sessions = data["sessions"]
    indexed = [s for s in sessions if s["indexed"]]
    physical = [s for s in sessions if s["paths"]]
    out = {"index": data["index"], "indexed_count": len(indexed),
           "jsonl_session_count": len(physical), "jsonl_file_count": sum(len(s["paths"]) for s in sessions),
           "orphan_count": sum(bool(s["paths"]) and not s["indexed"] for s in sessions),
           "missing_count": sum(s["indexed"] and not s["paths"] for s in sessions),
           "duplicate_count": sum(len(s["paths"]) > 1 for s in sessions),
           "issues": [{"id": s["id"], "paths": s["paths"], "indexed": s["indexed"]}
                      for s in sessions if not s["indexed"] or not s["paths"] or len(s["paths"]) > 1]}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        for k, v in out.items():
            print(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}")
    return 0


def iter_all_text(path):
    """把一行事件里所有字符串摊平，供 --all 全量搜索（含推理与工具输出）。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            role = d.get("role") if isinstance(d, dict) and isinstance(d.get("role"), str) else None
            timestamp = d.get("timestamp") if isinstance(d, dict) else None
            stack = [d]
            while stack:
                x = stack.pop()
                if isinstance(x, str):
                    yield role, x, timestamp
                elif isinstance(x, dict):
                    stack.extend(x.values())
                elif isinstance(x, list):
                    stack.extend(x)


def cmd_search(args):
    kw = args.keyword
    hits = []
    sessions = catalog()["sessions"]
    since = args.since
    before = args.before
    for meta in sessions:
        sid = meta["id"]
        paths = meta["paths"]
        if args.cwd and (not meta.get("cwd") or args.cwd.casefold() not in meta["cwd"].casefold()):
            continue
        n = 0
        samples = []
        last_ms = None
        size_bytes = 0
        for path in paths:
            try:
                size_bytes += os.path.getsize(path)
            except OSError:
                continue
            source = iter_all_text(path) if args.all else iter_messages(path, prefilter=kw)
            for role, text, t in source:
                stamp = timestamp_ms(t)
                if args.role and role != args.role:
                    continue
                if (since is not None or before is not None) and stamp is None:
                    continue
                if since is not None and stamp < since:
                    continue
                if before is not None and stamp >= before:
                    continue
                if kw not in text:
                    continue
                n += 1
                if stamp is not None and (last_ms is None or stamp > last_ms):
                    last_ms = stamp
                if len(samples) < 2:
                    i = text.find(kw)
                    seg = text[max(0, i - 60): i + 100].replace("\n", " ")
                    samples.append(f"    [{role} {ts(t)}] …{seg}…")
        if n:
            hits.append({"id": sid, "short": sid[:8], "count": n,
                         "size_mb": round(size_bytes / 1048576, 2),
                         "samples": samples, "title": meta.get("title"),
                         "cwd": meta.get("cwd"), "last_match_ms": last_ms,
                         "paths": paths, "ambiguous": len(paths) > 1})
    if args.sort == "recent":
        hits.sort(key=lambda x: (x["last_match_ms"] is not None, x["last_match_ms"] or 0, x["id"]), reverse=True)
    else:
        hits.sort(key=lambda x: (-x["count"], x["id"]))
    hits = hits[: args.n]
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return
    if not hits:
        print(f"没有会话命中 {kw!r}")
        return
    sort_label = "按最近命中时间排序" if args.sort == "recent" else "按命中数排序"
    print(f"命中 {len(hits)} 个会话（{sort_label}）\n")
    for h in hits:
        print(f"[{h['short']}] {h['count']} 处 · {h['size_mb']}MB · {h['id']}")
        for s in h["samples"]:
            print(s)
        print()


def cmd_find(args):
    """按索引查标题，只有无标题命中且传 --deep 时才读取 JSONL 正文。"""
    kw = args.keyword
    data = catalog()
    if data["index"]["status"] != "ok":
        print(f"警告：会话索引{data['index']['status']}（{data['index']['detail']}），标题未索引项无法匹配", file=sys.stderr)
    sessions = [s for s in data["sessions"] if not s["deleted"]]
    rows = [s for s in sessions if s["indexed"] and kw.casefold() in (s["title"] or "").casefold()]
    rows.sort(key=lambda s: (s["updated_ms"] is not None, s["updated_ms"] or 0, s["id"]), reverse=True)

    def hit(s):
        sid = s["id"]
        p = s["paths"][0] if len(s["paths"]) == 1 else None
        return {
            "id": sid, "short": sid[:8], "cwd": s["cwd"],
            "title": s["title"] or "(无标题)",
            "status": s["status"], "updated": ts(s["updated_ms"]),
            "automation": s["automation"],
            "size_mb": round(os.path.getsize(p) / 1048576, 2) if p else None,
        }

    out = []
    if not rows and args.deep:
        for s in sessions:
            if any(any(kw in text for _, text, _ in iter_messages(path, prefilter=kw)) for path in s["paths"]):
                item = hit(s)
                if not s["indexed"]:
                    item.update(cwd=None, title="(未索引)", status=None, updated="-", automation=False)
                item["size_mb"] = round(sum(os.path.getsize(p) for p in s["paths"]) / 1048576, 2) if s["paths"] else None
                item["match"] = "body"
                out.append(item)
    elif rows:
        out = [hit(r) for r in rows]
    out = out[:args.n]
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    if out:
        source = "标题" if rows else "正文"
        print(f"{source}命中 {len(out)} 条：\n")
        print(f"{'更新时间':<17} {'状态':<10} {'ID':<9} {'MB':>6}  标题")
        print("-" * 96)
        for r in out:
            mb = f"{r['size_mb']:.1f}" if r["size_mb"] else "-"
            flag = " [定时]" if r["automation"] else ""
            print(f"{r['updated']:<17} {r['status']:<10} {r['short']:<9} {mb:>6}  {r['title']}{flag}")
        print(f'\n打开：python scripts/wb-conv.py show {out[0]["short"]}')
        return

    print(f"标题里没有 {kw!r}。" if data["index"]["status"] == "ok" else f"标题暂不可用，索引{data['index']['status']}。")
    if args.deep:
        print("正文中也没有命中。")
    else:
        print(f"想搜正文加 --deep：")
        print(f"  python scripts/wb-conv.py find {kw} --deep")


def timestamp_ms(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            return int(number) if math.isfinite(number) else None
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = naive_local_utc(parsed)
        return datetime_epoch_ms(parsed)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def parse_bound(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = naive_local_utc(parsed)
        return datetime_epoch_ms(parsed)
    except (ValueError, TypeError, OverflowError, OSError):
        raise argparse.ArgumentTypeError(f"无效日期或时间: {value}")


def naive_local_utc(value):
    """Convert local wall time exactly, with a narrow Windows pre-epoch fallback."""
    try:
        return value.astimezone(dt.timezone.utc)
    except (OSError, OverflowError, ValueError):
        if value.year > 1970:
            raise
        # Windows may reject pre-epoch localtime conversions. Its standard local
        # offset is the only available fallback here; modern dates use the exact
        # platform timezone rules above, including daylight saving transitions.
        standard_offset = dt.timedelta(seconds=-time.timezone)
        return (value - standard_offset).replace(tzinfo=dt.timezone.utc)


def datetime_epoch_ms(value):
    utc_value = value.astimezone(dt.timezone.utc)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return int((utc_value - epoch).total_seconds() * 1000)


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是正整数")
    if number < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="读取 WorkBuddy 本地会话")
    ap.add_argument("--home", help="WorkBuddy 数据目录（优先级高于 WORKBUDDY_DIR）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出最近会话")
    p.add_argument("-n", type=positive_int, default=20)
    p.add_argument("--cwd", help="按工作目录关键字过滤")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("find", help="按标题秒查（DB 索引）")
    p.add_argument("keyword")
    p.add_argument("--deep", action="store_true", help="仅标题未命中时下沉搜索正文")
    p.add_argument("-n", type=positive_int, default=10)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("show", help="导出会话正文")
    p.add_argument("id", help="会话 id 或前缀")
    p.add_argument("--full", action="store_true", help="不截断")
    p.add_argument("--maxlen", type=positive_int, default=1500)
    p.add_argument("--tail", type=positive_int, help="只输出最近 N 条消息")
    p.add_argument("--trace", action="store_true", help="附工具调用轨迹")
    p.add_argument("--raw", action="store_true", help="保留用户消息包装原文（自动完整输出）")
    p.add_argument("--out", help="写入文件")
    p.add_argument("--images", action="store_true", help="复制消息中的本地图片引用到导出附件目录")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("title", help="查看会话元信息")
    p.add_argument("id")
    p.set_defaults(func=cmd_title)

    p = sub.add_parser("search", help="跨会话搜正文")
    p.add_argument("keyword")
    p.add_argument("-n", type=positive_int, default=10)
    p.add_argument("--all", action="store_true", help="同时搜索推理与工具事件中的文本")
    p.add_argument("--since", type=parse_bound, help="起始时间，包含边界")
    p.add_argument("--before", type=parse_bound, help="结束时间，不包含边界")
    p.add_argument("--role", choices=("user", "assistant"))
    p.add_argument("--cwd", help="按索引中的工作目录过滤")
    p.add_argument("--sort", choices=("relevance", "recent"), default="relevance")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    for name, handler, help_text in (("locate", cmd_locate, "定位会话 ID 与全部正文文件"),
                                     ("doctor", cmd_doctor, "诊断索引与 JSONL 对应关系")):
        p = sub.add_parser(name, help=help_text)
        if name == "locate":
            p.add_argument("id", help="会话 id 或前缀")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=handler)

    args = ap.parse_args()
    if args.cmd == "search" and args.since is not None and args.before is not None and args.since >= args.before:
        ap.error("--since 必须早于 --before")
    set_home(args.home)
    result = args.func(args)
    if isinstance(result, int):
        raise SystemExit(result)


if __name__ == "__main__":
    main()
