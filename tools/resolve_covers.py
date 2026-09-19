#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resolve_covers.py — 给 data/*.json 里的条目自动配上真实封面图

不填 cov 的条目，页面上会显示程序生成的抽象封面（几何母题）。
想看到真实的游戏封面 / 专辑封面 / 视频封面，就跑这个。

    python tools/resolve_covers.py            # 只补还没有封面的条目
    python tools/resolve_covers.py --force    # 全部重新解析
    python tools/resolve_covers.py --dry-run  # 只报告，不写文件
    python tools/resolve_covers.py --only game

各分类用的图源（都不需要 API key）：
    游戏  Steam 搜索 → appid → cdn.cloudflare.steamstatic.com
    音乐  iTunes Search API（中文独立音乐覆盖一般，拿不到就保留生成封面）
    视频  B站搜索 API（有反爬限流，会退避重试）

四条原则，都是踩过坑才加的：
  1. 候选按排名逐个「下载验图」，第一个验过的才用 —— 既躲开配错，也躲开 CDN 死链。
  2. 匹配必须对得上。配错封面比没有封面更糟：Hades 和 Hades II 的封面很像，很难发现。
  3. 别名表存**完整英文标题**，不存片段。只存 "witcher" 会匹配到 "The Witcher 3 REDkit"（MOD 工具）。
  4. 比对前做繁简归一。iTunes 对华语专辑常返回繁体（黑梦 → 黑夢），不归一会被误判成「搜不到」。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Steam 搜索只认英文名。用户真实的 Steam 数据本来就是英文名（GetOwnedGames 不本地化），
# 这张表只是为了让手写的中文样例数据也能配上封面。
# 值必须是**完整英文标题** —— 存片段会把续作 / MOD 工具 / 资料片匹配进来。
GAME_ALIASES: dict[str, str] = {
    "文明6":          "Sid Meier's Civilization VI",
    "艾尔登法环":      "ELDEN RING",
    "怪物猎人：世界":   "Monster Hunter: World",
    "巫师3：狂猎":     "The Witcher 3: Wild Hunt",
    "环世界":         "RimWorld",
    "荒野大镖客2":     "Red Dead Redemption 2",
    "女神异闻录5":     "Persona 5",
    "只狼：影逝二度":   "Sekiro: Shadows Die Twice",
    "泰拉瑞亚":       "Terraria",
    "黑帝斯":         "Hades",
    "空洞骑士":       "Hollow Knight",
    "星露谷物语":      "Stardew Valley",
    "死亡搁浅":       "DEATH STRANDING",
    "极乐迪斯科":      "Disco Elysium",
    "蔚蓝":           "Celeste",
    "双人成行":       "It Takes Two",
    "传送门2":        "Portal 2",
    # 塞尔达是 Switch 独占，Steam 上没有 —— 就该退回生成封面，不硬凑一张错的
}

# 只为了比对用：iTunes 对华语专辑经常返回繁体名，不归一会被误判成「对不上」。
_T2S = str.maketrans({
    "夢": "梦", "華": "华", "麗": "丽", "險": "险", "愛": "爱", "樂": "乐", "聲": "声",
    "東": "东", "車": "车", "這": "这", "個": "个", "們": "们", "時": "时", "會": "会",
    "說": "说", "對": "对", "開": "开", "關": "关", "實": "实", "現": "现", "樣": "样",
    "讓": "让", "過": "过", "還": "还", "進": "进", "來": "来", "為": "为", "與": "与",
    "長": "长", "見": "见", "覺": "觉", "點": "点", "學": "学", "國": "国", "語": "语",
    "讀": "读", "書": "书", "電": "电", "話": "话", "誰": "谁", "風": "风", "雲": "云",
    "飛": "飞", "鳥": "鸟", "龍": "龙", "買": "买", "賣": "卖", "錢": "钱", "銀": "银",
    "鋼": "钢", "鐵": "铁", "鐘": "钟", "間": "间", "陽": "阳", "陰": "阴", "園": "园",
    "遠": "远", "邊": "边", "導": "导", "層": "层", "歲": "岁", "萬": "万", "經": "经",
    "歷": "历", "斷": "断", "戀": "恋", "團": "团", "圖": "图", "場": "场", "處": "处",
    "備": "备", "復": "复", "興": "兴", "舉": "举", "舊": "旧", "觀": "观", "認": "认",
    "議": "议", "護": "护", "譜": "谱", "貝": "贝", "財": "财", "貴": "贵", "費": "费",
    "資": "资", "賞": "赏", "質": "质", "贏": "赢", "輕": "轻", "輪": "轮", "轉": "转",
    "運": "运", "遊": "游", "適": "适", "選": "选", "遲": "迟", "遺": "遗", "鄉": "乡",
    "醫": "医", "釋": "释", "錄": "录", "鏡": "镜", "顯": "显", "願": "愿", "類": "类",
    "飄": "飘", "驚": "惊", "體": "体", "髮": "发", "鬥": "斗", "魚": "鱼", "鮮": "鲜",
    "鳴": "鸣", "鴻": "鸿", "鷹": "鹰", "黃": "黄",
    # 第二批：人名用字和其余高频字
    "兒": "儿", "竇": "窦", "趙": "赵", "孫": "孙", "楊": "杨", "張": "张", "劉": "刘",
    "陳": "陈", "鄭": "郑", "韓": "韩", "馬": "马", "馮": "冯", "葉": "叶", "許": "许",
    "謝": "谢", "蘇": "苏", "鄧": "邓", "蕭": "萧", "羅": "罗", "歐": "欧", "嚴": "严",
    "譚": "谭", "賈": "贾", "賀": "贺", "賴": "赖", "龐": "庞", "藍": "蓝", "畢": "毕",
    "齊": "齐", "魯": "鲁", "諸": "诸", "衛": "卫", "黨": "党", "豐": "丰", "區": "区",
    "靜": "静", "綠": "绿", "紅": "红", "純": "纯", "練": "练", "織": "织", "終": "终",
    "結": "结", "給": "给", "統": "统", "絕": "绝", "續": "续", "線": "线", "編": "编",
    "緣": "缘", "聞": "闻", "聯": "联", "聽": "听", "腳": "脚", "臉": "脸", "臨": "临",
    "藝": "艺", "節": "节", "藥": "药", "號": "号", "蟲": "虫", "裝": "装", "規": "规",
    "視": "视", "親": "亲", "觸": "触", "計": "计", "記": "记", "訊": "讯", "設": "设",
    "訪": "访", "評": "评", "詞": "词", "試": "试", "詩": "诗", "詳": "详", "誌": "志",
    "誠": "诚", "誤": "误", "課": "课", "調": "调", "談": "谈", "請": "请", "論": "论",
    "淚": "泪", "嘆": "叹",
})


# ────────────────────────────── HTTP ──────────────────────────────

def fetch(url: str, headers: dict | None = None, timeout: int = 20, tries: int = 3):
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            last = e
            # 412 = B站的反爬限流，等久一点再来
            time.sleep(3.5 * (n + 1) if e.code == 412 else 1.2 * (n + 1))
        except Exception as e:
            last = e
            time.sleep(1.2 * (n + 1))
    raise last


def get_json(url: str, headers: dict | None = None, tries: int = 3):
    _, _, body = fetch(url, headers, tries=tries)
    return json.loads(body.decode("utf-8", "replace"))


def is_image(url: str, headers: dict | None = None) -> tuple[bool, str]:
    """真去下载一次，确认是可用的图片，而不是 404 页面或 HTML 错误页。"""
    try:
        st, ct, body = fetch(url, headers, timeout=25, tries=2)
        ok = st == 200 and ct.lower().startswith("image") and len(body) > 800
        return ok, f"HTTP {st}, {ct}, {len(body):,}B"
    except Exception as e:
        return False, str(e)[:90]


# ────────────────────────────── 匹配 ──────────────────────────────

def norm(s: str) -> str:
    """只留字母数字和汉字，并做繁简归一，用来做模糊比对。"""
    s = (s or "").translate(_T2S)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", s.lower())


def matches(query: str, got: str) -> bool:
    q, g = norm(query), norm(got)
    return bool(q) and bool(g) and (q in g or g in q)


def rank_candidate(name: str, expect: str):
    """给候选打分，越小越好。None 表示不相关。

    expect 必须是**完整标题**（如 "The Witcher 3: Wild Hunt"）：
      完全相等          → 最优
      包含完整标题      → 次之，长度差越小越好
      不包含完整标题    → 直接淘汰

    这样：
        查 "Hades"                    → Hades 胜过 Hades II（后者是包含关系，但长度差更大）
        查 "Hollow Knight"            → Hollow Knight 胜过 Hollow Knight: Silksong
        查 "The Witcher 3: Wild Hunt" → 排除 "The Witcher 3 REDkit"（根本不包含完整标题）
        查 "Red Dead Redemption 2"    → 排除 "Red Dead Redemption"（1 代，不含末尾的 2）
    """
    n, t = norm(name), norm(expect)
    if not t or t not in n:
        return None
    return (0 if n == t else 1, abs(len(n) - len(t)), len(n))


# ────────────────────────────── 三类图源 ──────────────────────────────
# 每个 resolver 返回 (候选列表, 错误说明)。候选列表里每项是 (url, 说明)。
# 真正的下载验图在主流程里做，按顺序试，第一个验过的才采用。

def cands_steam(item: dict) -> tuple[list, str | None]:
    name = item["n"]
    expect = GAME_ALIASES.get(name, name)
    arr = get_json("https://steamcommunity.com/actions/SearchApps/"
                   + urllib.parse.quote(expect))
    if not arr:
        return [], f"Steam 搜不到「{expect}」"

    scored = []
    for cand in arr:
        sc = rank_candidate(cand.get("name", ""), expect)
        if sc is not None:
            scored.append((sc, cand))
    if not scored:
        return [], f"结果对不上（首个是 {arr[0].get('name', '?')[:30]}）"

    scored.sort(key=lambda p: p[0])
    out = []
    for _, cand in scored:
        out.append((f"https://cdn.cloudflare.steamstatic.com/steam/apps/"
                    f"{cand['appid']}/header.jpg", f"→ {cand['name']}"))
    return out, None


def cands_itunes(item: dict) -> tuple[list, str | None]:
    term = f"{item['n']} {item.get('s', '')}".strip()
    d = get_json("https://itunes.apple.com/search?"
                 + urllib.parse.urlencode({"term": term, "entity": "album", "limit": 15}))
    res = [r for r in (d.get("results") or []) if r.get("artworkUrl100")]
    if not res:
        return [], "iTunes 搜不到"

    album, artist = item.get("s", ""), item.get("n", "")

    # 专辑名对得上的排前面，其中名字最短的优先（选原版而不是 (Live) / (Expanded Edition)）
    hit = [r for r in res if album and matches(album, r.get("collectionName", ""))]
    hit.sort(key=lambda r: len(r.get("collectionName", "")))
    byartist = [r for r in res if matches(artist, r.get("artistName", ""))]

    seen, out = set(), []
    for r in hit + byartist:
        key = r.get("collectionId")
        if key in seen:
            continue
        seen.add(key)
        art = r["artworkUrl100"]
        art = art.replace("100x100bb", "600x600bb").replace("60x60bb", "600x600bb")
        out.append((art, f"→ {r.get('artistName','')} / {r.get('collectionName','')}"))
    if not out:
        return [], f"结果对不上（首个是 {res[0].get('collectionName', '?')[:30]}）"
    return out[:5], None


def cands_bilibili(item: dict) -> tuple[list, str | None]:
    d = get_json("https://api.bilibili.com/x/web-interface/search/type?"
                 + urllib.parse.urlencode({"search_type": "video", "keyword": item["n"]}),
                 tries=4)
    if d.get("code") != 0:
        return [], f"B站 code={d.get('code')}"
    res = (d.get("data") or {}).get("result") or []
    if not res:
        return [], "B站 搜不到"
    out = []
    for r0 in res[:6]:
        pic = (r0.get("pic") or "").split("@")[0]
        if not pic:
            continue
        if pic.startswith("//"):
            pic = "https:" + pic
        title = re.sub(r"<[^>]+>", "", r0.get("title", ""))   # 去掉 <em class="keyword">
        out.append((pic, f"→ {title[:36]}"))
    return (out, None) if out else ([], "B站 无封面字段")


RESOLVERS = {"game": cands_steam, "music": cands_itunes, "video": cands_bilibili}
VERIFY_HEADERS = {"video": {"Referer": "https://www.bilibili.com/"}}


# ────────────────────────────── 主流程 ──────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="连已有封面的条目也重新解析")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--sleep", type=float, default=0.6, help="每次请求间隔秒数（默认 0.6）")
    ap.add_argument("--only", choices=["game", "music", "video"], help="只处理某一类")
    a = ap.parse_args()

    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        raise SystemExit("data/ 里没有 json。先跑 python tools/fetch_data.py demo")

    total = filled = skipped = failed = 0

    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        items = payload if isinstance(payload, list) else payload.get("items", [])
        if not items:
            continue
        print(f"\n── {path.name}（{len(items)} 条）──")
        touched = False

        for it in items:
            total += 1
            if a.only and it["c"] != a.only:
                continue
            if it.get("cov") and not a.force:
                skipped += 1
                continue

            fn = RESOLVERS.get(it["c"])
            label = f"[{it['c']:5}] {it['n'][:24]:<26}"

            try:
                cands, err = fn(it)
            except Exception as e:
                print(f"  {label} ✗ 查询失败：{str(e)[:70]}")
                failed += 1
                time.sleep(a.sleep)
                continue
            if not cands:
                print(f"  {label} ✗ {err}")
                failed += 1
                time.sleep(a.sleep)
                continue

            # 按排名逐个下载验图，第一个验过的才用 —— 同时躲开「配错」和「死链」
            chosen = None
            for url, note in cands:
                ok, detail = is_image(url, VERIFY_HEADERS.get(it["c"]))
                if ok:
                    chosen = (url, note)
                    break
                print(f"  {label} ·  跳过候选（{detail}）{note}")
                time.sleep(0.25)

            if chosen:
                it["cov"] = chosen[0]
                touched = True
                filled += 1
                print(f"  {label} ✓ {chosen[1]}")
            else:
                print(f"  {label} ✗ {len(cands)} 个候选全部验图失败")
                failed += 1
            time.sleep(a.sleep)

        if touched and not a.dry_run:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  → 已写回 {path.name}")

    print(f"\n合计 {total} 条：新配封面 {filled}，已有跳过 {skipped}，解析不到 {failed}")
    if filled and not a.dry_run:
        print("下一步：python tools/fetch_data.py patch")


if __name__ == "__main__":
    main()
