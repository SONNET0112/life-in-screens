#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_data.py — 把真实的娱乐数据灌进《我的人生去哪了》

它只做三件事：抓取 → 落成 data/*.json → 按标记块替换 index.html 里的 DATA / HEAT。

────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────
  # 1) Steam（官方 API，唯一零阻力的数据源，优先做这个）
  python tools/fetch_data.py steam --key <API_KEY> --steamid <STEAMID64 或自定义 ID>

  # 2) B站观看历史（未公开接口，但它能提供「几点在看」的真实时刻 → 热力图）
  python tools/fetch_data.py bilibili --sessdata <SESSDATA>

  # 3) 音乐：国内平台都没有官方 API，所以走 CSV 手工导入
  #    先看看表头长什么样：
  python tools/fetch_data.py music --template
  #    然后填好 data/music.csv 再导入：
  python tools/fetch_data.py music

  # 4) 合并所有 data/*.json 并写进 index.html（会先备份成 index.html.bak）
  python tools/fetch_data.py patch

  # 5) 先不联网，跑一遍完整流程看看效果
  python tools/fetch_data.py demo && python tools/fetch_data.py patch

────────────────────────────────────────────────────────
凭据怎么拿
────────────────────────────────────────────────────────
  Steam API Key : https://steamcommunity.com/dev/apikey  免费，域名随便填
  SteamID64     : https://steamid.io ，或 Steam 个人资料页 URL 里那串 17 位数字
                  也可以直接填自定义 ID（如 https://steamcommunity.com/id/xxxx 里的 xxxx）
  B站 SESSDATA  : 浏览器登录 bilibili.com → F12 → Application → Cookies
                  → https://www.bilibili.com → 复制 SESSDATA 的值

⚠ 凭据只走命令行/环境变量，脚本不落盘。
⚠ Steam 隐私设置：个人资料 → 编辑个人资料 → 隐私设置 →「游戏详情」必须设为「公开」，
  否则 GetOwnedGames 会返回空列表。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# Windows 控制台默认不是 UTF-8，中文会炸在 print 上
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HTML = ROOT / "index.html"

DATA_BEGIN, DATA_END = "/* ==== DATA:BEGIN ==== */", "/* ==== DATA:END ==== */"
HEAT_BEGIN, HEAT_END = "/* ==== HEAT:BEGIN ==== */", "/* ==== HEAT:END ==== */"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ────────────────────────────── 基础设施 ──────────────────────────────

def http_json(url: str, params: dict | None = None, headers: dict | None = None,
              timeout: int = 25) -> dict:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def write_json(path: Path, obj) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → 写入 {path.relative_to(ROOT)}")


def js_str(s) -> str:
    """转成单引号 JS 字符串字面量，和手写的 DATA 风格保持一致。"""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def item_to_js(it: dict) -> str:
    return ("{c:%s, n:%s, s:%s, y:%s, v:%s, cov:%s}"
            % (js_str(it["c"]), js_str(it["n"]), js_str(it.get("s", "")),
               int(it["y"]), it["v"], js_str(it.get("cov", ""))))


def load_all() -> tuple[list[dict], list[list[float]] | None]:
    """读 data/*.json，合并成 (items, heat)。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        raise SystemExit(
            "data/ 目录里没有任何 json。先跑一个抓取命令，或先看看效果：\n"
            "  python tools/fetch_data.py demo")

    items: list[dict] = []
    heat = None
    for f in files:
        # utf-8-sig：编辑器或 PowerShell 写出 BOM 也能读，有 BOM 无 BOM 都吃
        payload = json.loads(f.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            got = payload.get("items") or []
            if payload.get("heat"):
                heat = payload["heat"]
        else:
            got = payload
        print(f"  读取 {f.name}: {len(got)} 条")
        items += got

    # 归类校验 + 去重 + 排序
    for it in items:
        if it.get("c") not in ("game", "music", "video"):
            raise SystemExit(f"分类只能是 game/music/video，但看到 {it.get('c')!r}：{it!r}")
        for k in ("n", "y", "v"):
            if k not in it:
                raise SystemExit(f"条目缺字段 {k!r}：{it!r}")

    seen, uniq = set(), []
    for it in items:
        key = (it["c"], it["n"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    order = {"game": 0, "music": 1, "video": 2}
    uniq.sort(key=lambda d: (order[d["c"]], -float(d["v"])))

    if heat is not None:
        if (not isinstance(heat, list) or len(heat) != 7
                or any(not isinstance(r, list) or len(r) != 24 for r in heat)):
            print("  ⚠ heat 形状不是 7×24，已忽略，热力图将回退到演示分布")
            heat = None
    return uniq, heat


# ────────────────────────────── Steam ──────────────────────────────

def resolve_steamid(key: str, ident: str) -> str:
    if re.fullmatch(r"\d{17}", ident):
        return ident
    print(f"  「{ident}」不是 17 位数字，按自定义 ID 解析…")
    d = http_json("https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
                  {"key": key, "vanityurl": ident})
    r = d.get("response", {})
    if r.get("success") != 1:
        raise SystemExit(f"无法解析自定义 ID「{ident}」：{r.get('message', '未知原因')}\n"
                         "  提示：去 https://steamid.io 查你的 17 位 SteamID64 直接传进来更稳。")
    return r["steamid"]


def cmd_steam(a) -> None:
    key = a.key or os.environ.get("STEAM_API_KEY")
    if not key:
        raise SystemExit("缺少 --key（或设置环境变量 STEAM_API_KEY）。\n"
                         "  免费申请：https://steamcommunity.com/dev/apikey")

    sid = resolve_steamid(key, a.steamid)
    print(f"  已解析 SteamID64 = {sid}")

    d = http_json("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/", {
        "key": key, "steamid": sid, "include_appinfo": 1,
        "include_played_free_games": 1, "format": "json",
    })
    games = (d.get("response") or {}).get("games") or []
    if not games:
        raise SystemExit(
            "Steam 返回了 0 个游戏。按可能性排序：\n"
            "  1. 隐私设置没开：Steam → 个人资料 → 编辑个人资料 → 隐私设置\n"
            "     把「游戏详情」设为「公开」（「个人资料」也要公开）\n"
            "  2. SteamID64 填错了（去 https://steamid.io 核对）\n"
            "  3. 这个账号确实没有任何游玩记录")

    art = {"header": "header.jpg", "capsule": "capsule_616x353.jpg",
           "library": "library_600x900.jpg"}[a.art]

    items, skipped = [], 0
    for g in games:
        hours = (g.get("playtime_forever") or 0) / 60.0
        if hours < a.min_hours:
            skipped += 1
            continue
        rt = g.get("rtime_last_played") or 0
        items.append({
            "c": "game",
            "n": g.get("name") or f"App {g.get('appid')}",
            "s": "",
            # Steam 只给「最后一次游玩的时刻」，给不出每年分布，
            # 所以这里的年份是「最后玩过的那年」，是个近似值。
            "y": datetime.fromtimestamp(rt).year if rt else datetime.now().year,
            "v": round(hours, 1),
            "cov": f"https://cdn.cloudflare.steamstatic.com/steam/apps/{g['appid']}/{art}",
        })

    items.sort(key=lambda x: -x["v"])
    tot = sum(x["v"] for x in items)
    print(f"  {len(items)} 个游戏（跳过 {skipped} 个低于 {a.min_hours}h 的），合计 {tot:,.0f} 小时")
    print(f"  年份口径：最后一次游玩的年份（Steam 不提供逐年的游玩分布）")
    write_json(DATA_DIR / "steam.json", items)


# ────────────────────────────── B站 ──────────────────────────────

def cmd_bilibili(a) -> None:
    sess = a.sessdata or os.environ.get("BILI_SESSDATA")
    if not sess:
        raise SystemExit(
            "缺少 --sessdata（或环境变量 BILI_SESSDATA）。\n"
            "  bilibili.com 登录后 F12 → Application → Cookies → https://www.bilibili.com\n"
            "  复制 SESSDATA 的值（很长一串，含 %2C 之类的转义是正常的）")

    headers = {"Cookie": f"SESSDATA={sess}", "Referer": "https://www.bilibili.com/"}
    raw, pn = [], 1
    while pn <= a.max_pages:
        d = http_json("https://api.bilibili.com/x/v2/history",
                      {"ps": 30, "pn": pn}, headers=headers)
        if d.get("code") != 0:
            raise SystemExit(f"B站返回 code={d.get('code')} {d.get('message')!r}\n"
                             "  code=-101 基本就是 SESSDATA 失效或填错了。")
        batch = (d.get("data") or {}).get("list") or []
        if not batch:
            break
        raw += batch
        print(f"  第 {pn} 页：{len(batch)} 条（累计 {len(raw)}）")
        if len(batch) < 30:
            break
        pn += 1
        time.sleep(0.6)          # 别把人家的接口打疼

    if not raw:
        raise SystemExit("拿到 0 条观看历史。可能是账号历史被清空，或 cookie 不对。")

    # 第一次跑时把原始字段打出来，接口变了你能一眼看到
    if a.show_fields:
        print("  原始条目字段：" + ", ".join(sorted(raw[0].keys())))

    # 按标题聚合：同一条视频看多次要合并，否则墙会被同一部剧刷屏
    agg: dict[str, dict] = {}
    heat = [[0.0] * 24 for _ in range(7)]
    for e in raw:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        secs = float(e.get("duration") or 0)
        ts = int(e.get("view_at") or 0)
        if ts <= 0:
            continue
        dt = datetime.fromtimestamp(ts)
        hours = secs / 3600.0

        heat[dt.weekday()][dt.hour] += hours

        cur = agg.get(title)
        if cur is None:
            agg[title] = {
                "c": "video",
                "n": title,
                "s": (e.get("author_name") or e.get("tag_name") or "").strip(),
                "y": dt.year,
                "v": hours,
                "cov": e.get("cover") or "",
            }
        else:
            cur["v"] += hours
            if dt.year > cur["y"]:
                cur["y"] = dt.year

    items = [v for v in agg.values() if v["v"] >= a.min_hours]
    items.sort(key=lambda x: -x["v"])
    for it in items:
        it["v"] = round(it["v"], 1)

    tot = sum(x["v"] for x in items)
    print(f"  {len(items)} 条视频（原始 {len(raw)} 次观看记录），合计 {tot:,.1f} 小时")
    print(f"  覆盖时间段：{datetime.fromtimestamp(min(int(e['view_at']) for e in raw)):%Y-%m-%d}"
          f" ~ {datetime.fromtimestamp(max(int(e['view_at']) for e in raw)):%Y-%m-%d}")
    print("  注意：B站只保留最近一段时间的观看历史，所以这只是一部分。")

    hsum = sum(sum(r) for r in heat)
    if hsum > 0:
        peak = max(((heat[d][h], d, h) for d in range(7) for h in range(24)))
        print(f"  热力图峰值：{WEEKDAYS[peak[1]]} {peak[2]:02d}:00（累计 {peak[0]:.1f} 小时）")

    write_json(DATA_DIR / "bilibili.json", {
        "items": items,
        "heat": [[round(x, 3) for x in row] for row in heat],
    })


# ────────────────────────────── 音乐（CSV） ──────────────────────────────

MUSIC_COLS = ["name", "sub", "year", "hours"]

def cmd_music(a) -> None:
    path = DATA_DIR / "music.csv"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if a.template or not path.exists():
        if path.exists() and not a.template:
            pass
        path.write_text(
            "name,sub,year,hours\n"
            "万能青年旅店,冀西南林路行,2020,62\n"
            "Radiohead,In Rainbows,2021,48\n"
            "草东没有派对,丑奴儿,2019,44\n",
            encoding="utf-8-sig")           # BOM 让 Excel 双击打开不乱码
        print(f"  已生成模板 {path.relative_to(ROOT)}")
        if a.template:
            print("  填好每一行再跑一次 `python tools/fetch_data.py music`")
            return

    items, bad = [], 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            try:
                items.append({
                    "c": "music",
                    "n": name,
                    "s": (row.get("sub") or "").strip(),
                    "y": int(float(row.get("year") or 0)),
                    "v": round(float(row.get("hours") or 0), 1),
                    "cov": (row.get("cov") or "").strip(),
                })
            except (TypeError, ValueError):
                bad += 1
                print(f"  ⚠ 跳过无法解析的行：{row}")

    items.sort(key=lambda x: -x["v"])
    print(f"  {len(items)} 条音乐" + (f"（跳过 {bad} 行）" if bad else ""))
    print(f"  合计 {sum(x['v'] for x in items):,.1f} 小时")
    write_json(DATA_DIR / "music.json", items)


# ────────────────────────────── 写进 HTML ──────────────────────────────

def replace_block(html: str, begin: str, end: str, payload: str) -> str:
    i, j = html.find(begin), html.find(end)
    if i < 0 or j < 0 or j < i:
        raise SystemExit(f"index.html 里找不到标记块 {begin} / {end}。"
                         "你是不是把标记删掉了？")
    return html[:i + len(begin)] + "\n" + payload + "\n" + html[j:]


def cmd_patch(a) -> None:
    items, heat = load_all()

    counts = {}
    for it in items:
        counts[it["c"]] = counts.get(it["c"], 0) + 1
    total = sum(float(it["v"]) for it in items)

    data_js = ("const DATA = [\n"
               + ",\n".join("  " + item_to_js(it) for it in items)
               + "\n];")

    html = HTML.read_text(encoding="utf-8")
    html = replace_block(html, DATA_BEGIN, DATA_END, data_js)

    if heat is not None:
        rows = ",\n".join(
            "  [" + ", ".join(f"{v:g}" for v in row) + "]" for row in heat)
        heat_js = "const HEAT = [\n" + rows + "\n];"
        html = replace_block(html, HEAT_BEGIN, HEAT_END, heat_js)
        print("  热力图：已写入真实「星期 × 小时」矩阵")
    else:
        print("  热力图：没有真实数据，页面继续用演示分布")

    print(f"\n  合计 {len(items)} 条："
          + "、".join(f"{k} {v}" for k, v in sorted(counts.items()))
          + f"　总计 {total:,.0f} 小时（{total/24:.0f} 天）")

    if a.dry_run:
        print("\n  --dry-run：没有写入 index.html")
        return

    backup = HTML.with_suffix(".html.bak")
    shutil.copyfile(HTML, backup)
    print(f"  已备份 → {backup.name}")
    HTML.write_text(html, encoding="utf-8")
    print(f"  已写入 {HTML.name}　（双击就能看）")


# ────────────────────────────── 演示数据 ──────────────────────────────

def _demo_heat(d: int, h: int) -> float:
    """演示用的作息分布：晚 9 点主峰 + 午休小峰，周末更重，工作时间压低。"""
    evening = math.exp(-((h - 21) ** 2) / 7) * 6.0
    noon = math.exp(-((h - 12.5) ** 2) / 3.2) * 3.0
    weekend = 1.7 if d >= 5 else (1.15 if d == 4 else 1.0)
    work = 0.3 if 9 <= h <= 17 else 1.0
    return round(max(0.0, (evening + noon) * weekend * work), 2)


def cmd_demo(a) -> None:
    """不联网，造一份形状正确的数据，用来验证整条流水线。"""
    steam = [
        {"c": "game", "n": "艾尔登法环", "s": "", "y": 2022, "v": 187.3,
         "cov": "https://cdn.cloudflare.steamstatic.com/steam/apps/1245620/header.jpg"},
        {"c": "game", "n": "文明6", "s": "", "y": 2023, "v": 210.0,
         "cov": "https://cdn.cloudflare.steamstatic.com/steam/apps/289070/header.jpg"},
        {"c": "game", "n": "环世界", "s": "", "y": 2021, "v": 133.5,
         "cov": "https://cdn.cloudflare.steamstatic.com/steam/apps/294100/header.jpg"},
        {"c": "game", "n": "传送门2", "s": "", "y": 2024, "v": 22.0,
         "cov": "https://cdn.cloudflare.steamstatic.com/steam/apps/620/header.jpg"},
    ]
    music = [
        {"c": "music", "n": "万能青年旅店", "s": "冀西南林路行", "y": 2020, "v": 62, "cov": ""},
        {"c": "music", "n": "草东没有派对", "s": "丑奴儿", "y": 2019, "v": 44, "cov": ""},
        {"c": "music", "n": "Radiohead", "s": "In Rainbows", "y": 2021, "v": 48, "cov": ""},
    ]
    heat = [[_demo_heat(d, h) for h in range(24)] for d in range(7)]
    bili = {
        "items": [
            {"c": "video", "n": "进击的巨人", "s": "UP主甲", "y": 2023, "v": 54, "cov": ""},
            {"c": "video", "n": "地球脉动", "s": "纪录片", "y": 2024, "v": 26, "cov": ""},
        ],
        "heat": heat,
    }
    print("  生成演示数据（形状与真实抓取完全一致）：")
    write_json(DATA_DIR / "steam.json", steam)
    write_json(DATA_DIR / "music.json", music)
    write_json(DATA_DIR / "bilibili.json", bili)
    print("\n  下一步：python tools/fetch_data.py patch")


# ────────────────────────────── 入口 ──────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        prog="fetch_data.py",
        description="把真实娱乐数据抓取并写入《我的人生去哪了》的 index.html",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不联网先看效果：  python tools/fetch_data.py demo && python tools/fetch_data.py patch")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("steam", help="抓 Steam 游戏时长（官方 API）")
    s.add_argument("--key", help="Steam Web API Key，也可用环境变量 STEAM_API_KEY")
    s.add_argument("--steamid", required=True, help="SteamID64，或自定义 ID")
    s.add_argument("--min-hours", type=float, default=1.0, help="低于这个小时数就忽略（默认 1）")
    s.add_argument("--art", choices=["header", "capsule", "library"], default="header",
                   help="封面图规格：header 460x215（最稳）/ capsule 616x353 / library 600x900 竖版")
    s.set_defaults(func=cmd_steam)

    b = sub.add_parser("bilibili", help="抓 B站观看历史（含真实时刻 → 热力图）")
    b.add_argument("--sessdata", help="B站 cookie 里的 SESSDATA，也可用环境变量 BILI_SESSDATA")
    b.add_argument("--max-pages", type=int, default=20, help="最多翻多少页，每页 30 条（默认 20）")
    b.add_argument("--min-hours", type=float, default=0.2, help="低于这个小时数就忽略（默认 0.2）")
    b.add_argument("--no-show-fields", dest="show_fields", action="store_false",
                   help="不打第一条的原始字段名")
    b.set_defaults(func=cmd_bilibili, show_fields=True)

    m = sub.add_parser("music", help="从 data/music.csv 导入音乐记录")
    m.add_argument("--template", action="store_true", help="只生成 CSV 模板，不导入")
    m.set_defaults(func=cmd_music)

    pa = sub.add_parser("patch", help="合并 data/*.json 并写进 index.html")
    pa.add_argument("--dry-run", action="store_true", help="只报数，不写文件")
    pa.set_defaults(func=cmd_patch)

    d = sub.add_parser("demo", help="生成演示数据（不联网），用来验证流水线")
    d.set_defaults(func=cmd_demo)

    args = p.parse_args()
    print(f"\n[{args.cmd}]")
    args.func(args)
    print()


if __name__ == "__main__":
    main()
