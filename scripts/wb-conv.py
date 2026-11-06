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
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import quote

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
    found = {}
    for p in glob.glob(os.path.join(PROJECTS_DIR, "*", f"{prefix}*.jsonl")):
        found.setdefault(os.path.basename(p)[:-6], []).append(p)
    rows = [(sid, paths[0]) for sid, paths in found.items()]
    if not rows:
        sys.exit(f"没有匹配 {prefix!r} 的会话文件")
    if len(rows) > 1:
        exact = [r for r in rows if r[0] == prefix]
        if exact and len(found[prefix]) == 1:
            return exact[0]
        print("前缀有歧义，命中多个，请给更长的前缀：", file=sys.stderr)
        for sid, _ in rows[:20]:
            print(f"  {sid}", file=sys.stderr)
        sys.exit(2)
    if len(found[rows[0][0]]) > 1:
        sys.exit(f"会话 {rows[0][0]} 存在多个项目副本，无法安全选择: " + ", ".join(found[rows[0][0]]))
    return rows[0]


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
        suffix = IMAGE_MIME_SUFFIXES.get(mime) or source.suffix.lower()
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
        try:
            target.write_bytes(data)
        except OSError:
            return None
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
    con = open_db()
    cur = con.cursor()
    sql = ("select id, cwd, title, custom_title, status, updated_at, mode, model, "
           "is_background_automation from sessions where deleted_at is null")
    params = []
    if args.cwd:
        sql += " and cwd like ?"
        params.append(f"%{args.cwd}%")
    sql += " order by coalesce(last_activity_at, updated_at) desc limit ?"
    params.append(args.n)
    rows = cur.execute(sql, params).fetchall()

    out = []
    for sid, cwd, title, custom, status, upd, mode, model, bg in rows:
        p = session_jsonl(sid)
        out.append({
            "id": sid, "short": sid[:8], "cwd": cwd,
            "title": custom or title or "(无标题)",
            "status": status, "updated": ts(upd),
            "updated_ms": upd, "mode": mode, "model": model,
            "automation": bool(bg),
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
    if args.raw:
        args.full = True
    image_cache = {}
    attachment_dir = attachment_dir_for(args.out) if args.images else None
    attachment_created = False
    records = (iter_message_records(path, raw=args.raw) if args.images else
               ((role, text, timestamp, [])
                for role, text, timestamp in iter_messages(path, raw=args.raw)))
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
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"已导出 {len(body)} 字符 → {args.out}（消息 {total} 条）")
    else:
        print(body)


def cmd_title(args):
    sid, path = resolve_prefix(args.id)
    con = open_db()
    row = con.execute(
        "select id,cwd,title,custom_title,status,created_at,updated_at,mode,model,permission_mode "
        "from sessions where id=?", (sid,)).fetchone()
    keys = ["id", "cwd", "title", "custom_title", "status", "created_at", "updated_at",
            "mode", "model", "permission_mode"]
    d = dict(zip(keys, row)) if row else {}
    d["jsonl"] = path
    d["size_bytes"] = os.path.getsize(path)
    print(json.dumps(d, ensure_ascii=False, indent=1))


def iter_all_text(path):
    """把一行事件里所有字符串摊平，供 --all 全量搜索（含推理与工具输出）。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            stack = [d]
            while stack:
                x = stack.pop()
                if isinstance(x, str):
                    yield "$all", x, None
                elif isinstance(x, dict):
                    stack.extend(x.values())
                elif isinstance(x, list):
                    stack.extend(x)


def cmd_search(args):
    kw = args.keyword
    hits = []
    for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        sid = os.path.basename(path)[:-6]
        n = 0
        samples = []
        source = iter_all_text(path) if args.all else iter_messages(path, prefilter=kw)
        for role, text, t in source:
            if kw not in text:
                continue
            n += 1
            if len(samples) < 2:
                i = text.find(kw)
                seg = text[max(0, i - 60): i + 100].replace("\n", " ")
                samples.append(f"    [{role} {ts(t)}] …{seg}…")
        if n:
            hits.append({"id": sid, "short": sid[:8], "count": n,
                         "size_mb": round(os.path.getsize(path) / 1048576, 2),
                         "samples": samples})
    hits.sort(key=lambda x: -x["count"])
    hits = hits[: args.n]
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return
    if not hits:
        print(f"没有会话命中 {kw!r}")
        return
    print(f"命中 {len(hits)} 个会话（按命中数排序）\n")
    for h in hits:
        print(f"[{h['short']}] {h['count']} 处 · {h['size_mb']}MB · {h['id']}")
        for s in h["samples"]:
            print(s)
        print()


def cmd_find(args):
    """按索引查标题，只有无标题命中且传 --deep 时才读取 JSONL 正文。"""
    kw = args.keyword
    like = f"%{kw}%"
    con = open_db()
    rows = con.execute(
        "select id, cwd, title, custom_title, status, updated_at, is_background_automation "
        "from sessions where deleted_at is null and (title like ? or custom_title like ?) "
        "order by coalesce(last_activity_at, updated_at) desc",
        (like, like)).fetchall()

    def hit(r):
        sid, cwd, title, custom, status, upd, bg = r
        p = session_jsonl(sid)
        return {
            "id": sid, "short": sid[:8], "cwd": cwd,
            "title": custom or title or "(无标题)",
            "status": status, "updated": ts(upd),
            "automation": bool(bg),
            "size_mb": round(os.path.getsize(p) / 1048576, 2) if p else None,
        }

    out = []
    if not rows and args.deep:
        paths = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
        for path in paths:
            sid = os.path.basename(path)[:-6]
            if any(kw in text for _, text, _ in iter_messages(path, prefilter=kw)):
                row = con.execute("select id,cwd,title,custom_title,status,updated_at,is_background_automation from sessions where id=? and deleted_at is null", (sid,)).fetchone()
                if row:
                    out.append(hit(row))
                else:
                    out.append({"id": sid, "short": sid[:8], "cwd": None, "title": "(索引不可用)", "status": None, "updated": "-", "automation": False, "size_mb": round(os.path.getsize(path)/1048576, 2), "match": "body"})
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

    print(f"标题里没有 {kw!r}。")
    if args.deep:
        print("正文中也没有命中。")
    else:
        print(f"想搜正文加 --deep：")
        print(f"  python scripts/wb-conv.py find {kw} --deep")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="读取 WorkBuddy 本地会话")
    ap.add_argument("--home", help="WorkBuddy 数据目录（优先级高于 WORKBUDDY_DIR）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出最近会话")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--cwd", help="按工作目录关键字过滤")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("find", help="按标题秒查（DB 索引）")
    p.add_argument("keyword")
    p.add_argument("--deep", action="store_true", help="仅标题未命中时下沉搜索正文")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("show", help="导出会话正文")
    p.add_argument("id", help="会话 id 或前缀")
    p.add_argument("--full", action="store_true", help="不截断")
    p.add_argument("--maxlen", type=int, default=1500)
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
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--all", action="store_true", help="同时搜索推理与工具事件中的文本")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    args = ap.parse_args()
    set_home(args.home)
    args.func(args)


if __name__ == "__main__":
    main()
