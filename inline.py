#!/usr/bin/env python3
"""把当前批次渲染成一块可直接嵌进聊天窗口的内联面板（inline-panel.html）。
用法：python3 inline.py [batch_id]
点击带 data-hermes-send 的元素会把那句话当作隐藏提问发回给 agent。
"""
import html
import json
import sqlite3
import sys
import time
import urllib.request

DB = "workbench.db"
OUT = "inline-panel.html"
STATES = ["待判定", "已定曲风", "生成中", "待挑版", "已定版", "待封面", "已交付"]


def cards(batch):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "select * from songs where batch_id=? order by seq", (batch,))]
    name = c.execute("select name from batches where id=?", (batch,)).fetchone()
    c.close()
    return (name[0] if name else f"批次{batch}"), rows


def live_credit():
    for port in (8765, 8766, 8767):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/credits", timeout=2) as r:
                return json.load(r).get("credits")
        except Exception:
            continue
    return None


def esc(s):
    return html.escape(str(s or ""))


def card_html(r):
    st = r["state"] or "待判定"
    idx = STATES.index(st) if st in STATES else 0
    steps = "".join(
        f'<i class="{"on" if i <= idx else ""}"></i>' for i in range(len(STATES)))
    sig = esc(r["core_sig"] or "—")
    title = esc(r["title"])
    has = []
    if r["lyrics"]:
        has.append(f'词{len(r["lyrics"])}')
    if r["suno"]:
        has.append("指令")
    if r["files"]:
        try:
            has.append(f'{len(json.loads(r["files"]))}版')
        except Exception:
            pass
    ask = f'看第 {r["seq"]} 首《{r["title"]}》的歌词和曲风，给我判断'
    return f'''<div class="c" data-hermes-send="{esc(ask)}">
  <div class="n">{r["seq"]:02d}</div>
  <div class="t">{title}</div>
  <div class="s">{st}</div>
  <div class="steps">{steps}</div>
  <div class="m"><span>{sig}</span><span>{esc(r["role"] or "")}</span></div>
  <div class="f">{" · ".join(has) or "空"}</div>
</div>'''


def main():
    batch = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    name, rows = cards(batch)
    credit = live_credit()
    groups = {}
    for r in rows:
        if r["core_sig"]:
            groups.setdefault(r["core_sig"], []).append(r["title"])
    dup = {k: v for k, v in groups.items() if len(v) > 1}
    warn = ""
    if dup:
        parts = "；".join(f'<b>{esc(k)}</b> ×{len(v)} → {esc("、".join(v))}'
                          for k, v in dup.items())
        warn = f'<div class="warn">同批编制重复 {len(dup)} 组：{parts}</div>'
    else:
        warn = f'<div class="ok">同批编制无重复 · 核心 {len(groups)} 种</div>'
    done = sum(1 for r in rows if r["lyrics"])
    styled = sum(1 for r in rows if r["suno"])
    body = "\n".join(card_html(r) for r in rows)
    page = f'''<!doctype html><meta charset="utf-8">
<style>
*{{box-sizing:border-box}}
body{{margin:0;font:13px/1.5 -apple-system,"PingFang SC",sans-serif;color:var(--foreground)}}
.bar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}}
.h{{font-weight:600;font-size:14px}}
.k{{color:var(--muted-foreground);font-size:12px}}
.btn{{border:1px solid var(--border);border-radius:7px;padding:4px 10px;font-size:12px;
  cursor:pointer;color:var(--foreground);background:transparent}}
.btn:hover{{background:var(--accent)}}
.btn.p{{border-color:var(--accent);font-weight:600}}
.warn{{border:1px solid #ff6b5e;border-left-width:3px;border-radius:6px;padding:6px 9px;
  font-size:12px;margin-bottom:10px}}
.ok{{border:1px solid var(--border);border-radius:6px;padding:6px 9px;
  font-size:12px;margin-bottom:10px;color:var(--muted-foreground)}}
.wall{{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:8px}}
.c{{border:1px solid var(--border);border-radius:8px;padding:8px 9px;cursor:pointer;position:relative}}
.c:hover{{border-color:var(--accent)}}
.n{{position:absolute;top:7px;right:8px;font-size:11px;color:var(--muted-foreground)}}
.t{{font-weight:600;font-size:13px;padding-right:20px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}}
.s{{display:inline-block;font-size:11px;border:1px solid var(--border);border-radius:5px;
  padding:0 5px;margin:5px 0 4px;color:var(--muted-foreground)}}
.steps{{display:flex;gap:2px;margin-bottom:5px}}
.steps i{{flex:1;height:3px;border-radius:2px;background:var(--border)}}
.steps i.on{{background:var(--accent)}}
.m{{display:flex;justify-content:space-between;gap:6px;font-size:11px;
  color:var(--muted-foreground);white-space:nowrap;overflow:hidden}}
.f{{font-size:11px;color:var(--muted-foreground);margin-top:3px}}
</style>
<div class="bar">
  <span class="h">歌曲工作台</span>
  <span class="k">{esc(name)} · {len(rows)} 首 · 有词 {done} · 有指令 {styled}</span>
  <span class="k">积分 {credit if credit is not None else "未连上"}</span>
  <span class="btn p" data-hermes-send="把这一批还没写的歌词全部写完">整批写词</span>
  <span class="btn" data-hermes-send="给这一批还没定曲风的都定上，别和同批重复">整批定曲风</span>
  <span class="btn" data-hermes-send="把定好曲风的全部推送到 suno 生成">推送生成</span>
  <span class="btn" data-hermes-send="轮询一下生成进度，把结果同步到面板">轮询</span>
</div>
{warn}
<div class="wall">
{body}
</div>
<div class="k" style="margin-top:8px">点卡片 = 让我看这首；点按钮 = 下一道整批指令</div>
'''
    open(OUT, "w").write(page)
    print(f"{OUT} · {len(rows)} 张卡 · 重复 {len(dup)} 组 · {len(page)} 字节")


if __name__ == "__main__":
    main()
