#!/usr/bin/env python3
"""歌曲工作台 —— 批次 / 卡片 / 曲风去重 / 驱动 suno-panel(:7788)

零依赖：只用标准库 + SQLite。suno-panel 一行不改，本面板当编排层。
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "workbench.db")
STATIC = os.path.join(HERE, "static")
SUNO_FALLBACK = ["http://127.0.0.1:8765", "http://127.0.0.1:8766", "http://127.0.0.1:8767"]
SUNO_ENV = os.environ.get("SUNO_URL", "")
_SUNO_CACHE = {"url": None, "at": 0}
SONG_OUT = os.path.expanduser(os.environ.get("SONG_OUT", "~/song-workbench/out"))
LYRIC_SRC = os.path.expanduser(os.environ.get("SONG_SOURCES", "~/song-workbench/sources"))
PORT = int(os.environ.get("PORT", "7792"))

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
STATE_FLOW = ["待判定", "已定曲风", "生成中", "待挑版", "已定版", "待封面", "已交付"]

INSTR = ["钢琴", "电钢琴", "弦乐", "大提琴", "小提琴", "木吉他", "电吉他", "贝斯", "鼓", "鼓组",
         "电子鼓", "合成器", "pad", "arp", "长笛", "口琴", "萨克斯", "古筝", "二胡", "手风琴",
         "琵琶", "笛子", "马林巴", "铃铛", "合唱", "和声", "808"]
# 英文标签（suno-panel 的 /api/prompt 常输出英文 tags）→ 归到同一个乐器族
EN2CN = {"piano": "钢琴", "electric piano": "电钢琴", "strings": "弦乐", "cello": "大提琴",
         "violin": "小提琴", "acoustic guitar": "木吉他", "guitar": "木吉他",
         "electric guitar": "电吉他", "bass": "贝斯", "drums": "鼓", "drum machine": "电子鼓",
         "synth": "合成器", "synthesizer": "合成器", "flute": "长笛", "harmonica": "口琴",
         "sax": "萨克斯", "saxophone": "萨克斯", "erhu": "二胡", "guzheng": "古筝",
         "pipa": "琵琶", "accordion": "手风琴", "bell": "铃铛", "marimba": "马林巴",
         "choir": "合唱", "808": "808"}
TEMPO = ["慢板", "中速", "中板", "快", "trap", "ballad", "bpm"]
VOCAL = ["男声独唱", "女声独唱", "男声", "女声", "气声", "沙哑", "清亮", "厚实", "撕裂",
         "假声", "低吟", "童声", "合唱", "rap", "说唱"]


# ---------- xlsx ----------
def read_xlsx(path):
    z = zipfile.ZipFile(path)
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(NS + "si"):
            ss.append("".join(t.text or "" for t in si.iter(NS + "t")))
    sheet = "xl/worksheets/sheet1.xml"
    for n in z.namelist():
        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"):
            sheet = n
            break
    root = ET.fromstring(z.read(sheet))
    table = []
    for row in root.iter(NS + "row"):
        cells = {}
        for c in row.findall(NS + "c"):
            ref = c.get("r") or ""
            col = re.sub(r"\d", "", ref)
            v = c.find(NS + "v")
            if c.get("t") == "s" and v is not None:
                val = ss[int(v.text)] if int(v.text) < len(ss) else ""
            elif c.get("t") == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(NS + "t"))
            else:
                val = v.text if v is not None else ""
            cells[col] = (val or "").strip()
        table.append(cells)
    if not table:
        return []
    head = table[0]
    cols = {head[k]: k for k in head}
    out = []
    for cells in table[1:]:
        if not any(cells.values()):
            continue
        item = {name: cells.get(col, "") for name, col in cols.items()}
        out.append(item)
    return out


def pick(d, *names):
    for n in names:
        for k, v in d.items():
            if n in k:
                return v or ""
    return ""


# ---------- db ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
    create table if not exists batches(
      id integer primary key autoincrement, name text, created real, source text, note text);
    create table if not exists songs(
      id integer primary key autoincrement, batch_id integer, seq integer,
      theme text, title text, role text, lyrics text, genre text, highlight text,
      suno text, score text, state text default '待判定',
      core_sig text, suno_job text, files text, picked integer, delivered text,
      note text, updated real);
    """)
    c.commit()
    c.close()


def row2card(r):
    files = json.loads(r["files"]) if r["files"] else []
    return {"id": r["id"], "batch_id": r["batch_id"], "seq": r["seq"], "theme": r["theme"],
            "title": r["title"], "role": r["role"], "lyrics": r["lyrics"], "genre": r["genre"],
            "highlight": r["highlight"], "suno": r["suno"], "score": r["score"],
            "state": r["state"], "sig": r["core_sig"], "job": r["suno_job"],
            "files": files, "picked": r["picked"], "delivered": r["delivered"], "note": r["note"]}


# ---------- 编制核心 / 去重 ----------
def core_signature(genre, suno, role):
    """编制核心 = 主奏乐器族 + 速度感 + 人声族。人声只记族（男/女/童/说唱），避免重复计数。"""
    text = f"{genre} {suno} {role}".lower()
    for en, cn in EN2CN.items():
        if en in text and cn not in text:
            text += " " + cn
    fam = []
    for i in INSTR:
        if i.lower() not in text:
            continue
        # 乐器归族，避免「鼓/鼓组/电子鼓」「钢琴/电钢琴」重复计分
        if i in ("鼓组", "电子鼓") and "鼓" in fam:
            continue
        if i == "鼓" and ("鼓组" in fam or "电子鼓" in fam):
            continue
        if i == "电钢琴" and "钢琴" in fam:
            continue
        if i in ("arp", "pad") and "合成器" in fam:
            continue
        fam.append(i)
    ins = fam[:3]
    tempo = ""
    for t in ("慢板", "中速", "中板", "快"):
        if t in text:
            tempo = t
            break
    if not tempo:
        bp = re.findall(r"(\d{2,3})\s*bpm", text)
        if bp:
            v = int(bp[0])
            tempo = "慢" if v < 80 else ("中" if v < 115 else "快")
    vocal = ""
    if "说唱" in text or "rap" in text:
        vocal = "说唱"
    elif "童声" in text:
        vocal = "童声"
    elif "女声" in text:
        vocal = "女声"
    elif "男声" in text:
        vocal = "男声"
    return " | ".join(x for x in (",".join(ins), tempo, vocal) if x)


def batch_conflicts(cards):
    """同批编制核心去重：按「主奏乐器族+速度+人声族」分组。
    第一次出现不报警；第二首起标红，并给出第几次重复。"""
    groups = {}
    for c in cards:
        if c["sig"]:
            groups.setdefault(c["sig"], []).append(c)
    flags = {}
    for sig, lst in groups.items():
        if len(lst) < 2:
            continue
        for n, c in enumerate(lst[1:], start=2):
            flags.setdefault(c["id"], []).append(
                f"第{n}次重复核心「{sig}」（首见《{lst[0]['title']}》）")
    summary = [{"sig": sig, "n": len(lst), "titles": [c["title"] for c in lst]}
               for sig, lst in sorted(groups.items(), key=lambda kv: -len(kv[1])) if len(lst) > 1]
    return flags, summary


# ---------- suno-panel 桥 ----------
def suno_base():
    """找 suno-panel 真正在跑的端口（它会在 8765 起顺延）。"""
    if SUNO_ENV:
        return SUNO_ENV
    now = time.time()
    if _SUNO_CACHE["url"] and now - _SUNO_CACHE["at"] < 20:
        return _SUNO_CACHE["url"]
    for cand in SUNO_FALLBACK:
        try:
            with urllib.request.urlopen(cand + "/api/outdir", timeout=2) as f:
                if f.status == 200:
                    _SUNO_CACHE.update(url=cand, at=now)
                    return cand
        except Exception:
            continue
    _SUNO_CACHE.update(url=None, at=now)
    return SUNO_FALLBACK[0]


def suno_call(path, payload=None, timeout=30):
    url = suno_base() + path
    try:
        if payload is None:
            with urllib.request.urlopen(url, timeout=timeout) as f:
                return json.loads(f.read().decode() or "{}")
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return json.loads(f.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "{}") or {"ok": False, "error": f"HTTP {e.code}"}
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"连不上 suno-panel（8765 起）— {e}"}


# ---------- 动作 ----------
def create_batch(name, source_path=None, count=None, offset=0):
    c = db()
    cur = c.execute("insert into batches(name,created,source,note) values(?,?,?,?)",
                    (name, time.time(), source_path or "原创歌词.xlsx", ""))
    bid = cur.lastrowid
    rows = []
    if source_path and os.path.exists(source_path):
        raw = read_xlsx(source_path)
        for i, d in enumerate(raw):
            rows.append({
                "theme": pick(d, "原创主题"), "title": pick(d, "歌曲名", "歌名"),
                "role": pick(d, "演唱身份"), "lyrics": pick(d, "歌词"),
                "genre": pick(d, "曲风"), "highlight": pick(d, "高光"),
                "suno": pick(d, "Suno", "指令"), "score": pick(d, "评分")})
    else:
        src = os.path.join(LYRIC_SRC, "原创歌词.xlsx")
        raw = read_xlsx(src)
        titles = [pick(d, "歌曲名", "歌名") for d in raw if pick(d, "歌曲名", "歌名")]
        rows = [{"theme": "", "title": t, "role": "", "lyrics": "", "genre": "",
                 "highlight": "", "suno": "", "score": ""} for t in titles]
    if count:
        rows = rows[offset:offset + int(count)]
    elif offset:
        rows = rows[offset:]
    for i, r in enumerate(rows):
        sig = core_signature(r["genre"], r["suno"], r["role"])
        state = "已定曲风" if (r["lyrics"] and r["suno"]) else "待判定"
        c.execute("""insert into songs(batch_id,seq,theme,title,role,lyrics,genre,highlight,suno,score,
                     state,core_sig,files,updated) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (bid, i + 1, r["theme"], r["title"], r["role"], r["lyrics"], r["genre"],
                   r["highlight"], r["suno"], r["score"], state, sig, "[]", time.time()))
    c.commit()
    c.close()
    return bid, len(rows)


def cards(bid):
    c = db()
    rs = c.execute("select * from songs where batch_id=? order by seq", (bid,)).fetchall()
    c.close()
    return [row2card(r) for r in rs]


def set_song(sid, fields):
    keys = [k for k in fields if k in
            ("lyrics", "genre", "suno", "role", "state", "note", "picked", "delivered", "score", "title", "highlight")]
    if not keys:
        return
    sets = ", ".join(f"{k}=?" for k in keys)
    vals = [fields[k] for k in keys]
    c = db()
    r = c.execute("select * from songs where id=?", (sid,)).fetchone()
    genre = fields.get("genre", r["genre"])
    suno = fields.get("suno", r["suno"])
    role = fields.get("role", r["role"])
    c.execute(f"update songs set {sets}, core_sig=?, updated=? where id=?",
              vals + [core_signature(genre, suno, role), time.time(), sid])
    c.commit()
    c.close()


# ---------- http ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            p = os.path.join(STATIC, "index.html")
            if not os.path.exists(p):
                return self._send({"error": "no ui"}, 404)
            return self._send(open(p, "rb").read(), ctype="text/html")
        if u.path == "/api/batches":
            c = db()
            rs = c.execute("""select b.*, (select count(*) from songs s where s.batch_id=b.id) n
                              from batches b order by b.id desc""").fetchall()
            c.close()
            return self._send({"ok": True, "batches": [dict(r) for r in rs]})
        if u.path == "/api/cards":
            bid = int((q.get("batch") or ["0"])[0] or 0)
            cs = cards(bid)
            flags, groups = batch_conflicts(cs)
            sigs = {c["sig"] for c in cs if c["sig"]}
            return self._send({"ok": True, "cards": cs, "conflicts": flags, "groups": groups,
                               "core_kinds": len(sigs), "flow": STATE_FLOW})
        if u.path == "/api/suno":
            j = suno_call("/api/jobs")
            return self._send({"ok": j.get("ok", False), "base": suno_base(),
                               "jobs": (j.get("jobs") or [])[:10], "error": j.get("error")})
        if u.path == "/api/audio":
            path = (q.get("path") or [""])[0]
            if not path or not os.path.exists(path):
                return self._send({"error": "no file"}, 404)
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        if u.path == "/api/sources":
            out = []
            for d in (LYRIC_SRC, os.path.join(LYRIC_SRC, "已完成")):
                if os.path.isdir(d):
                    for n in sorted(os.listdir(d)):
                        if n.endswith(".xlsx") and not n.startswith("~"):
                            out.append(os.path.join(d, n))
            return self._send({"ok": True, "sources": out})
        return self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        if u.path == "/api/batch/new":
            bid, n = create_batch(b.get("name") or time.strftime("批次 %m-%d %H:%M"),
                                  b.get("source") or None, b.get("count"), int(b.get("offset") or 0))
            return self._send({"ok": True, "batch": bid, "count": n})
        if u.path == "/api/song/save":
            set_song(int(b["id"]), b)
            return self._send({"ok": True})
        if u.path == "/api/song/write":
            sid = int(b["id"])
            c = db()
            r = c.execute("select * from songs where id=?", (sid,)).fetchone()
            c.close()
            if not r:
                return self._send({"ok": False, "error": "卡片不存在"}, 404)
            brief = (r["theme"] or "") + ("《" + r["title"] + "》" if r["title"] else "")
            res = suno_call("/api/lyrics", {"brief": brief, "style": r["genre"] or "",
                                            "sections": b.get("sections") or ""}, timeout=180)
            if not res.get("ok") or not res.get("text"):
                return self._send({"ok": False, "error": res.get("error") or "没拿到歌词"})
            set_song(sid, {"lyrics": res["text"].strip(),
                           "state": "已定曲风" if (r["suno"] or r["genre"]) else "待判定"})
            return self._send({"ok": True, "chars": len(res["text"])})
        if u.path == "/api/song/style":
            sid = int(b["id"])
            c = db()
            r = c.execute("select * from songs where id=?", (sid,)).fetchone()
            c.close()
            if not r:
                return self._send({"ok": False, "error": "卡片不存在"}, 404)
            if len((r["lyrics"] or "").strip()) < 10:
                return self._send({"ok": False, "error": "先写词再定曲风"}, 400)
            used = []
            c = db()
            for o in c.execute("select core_sig from songs where batch_id=? and id!=? and core_sig!=''",
                               (r["batch_id"], sid)).fetchall():
                used.append(o["core_sig"])
            c.close()
            text = f"《{r['title']}》{r['theme'] or ''}\n" + (r["lyrics"] or "")[:300]
            if used:
                text = ("这批已用过的编制（主奏乐器/速度/人声）不要再重复：" +
                        "；".join(sorted(set(used))) + "\n" + text)
            res = suno_call("/api/prompt", {"text": text}, timeout=120)
            tags = (res.get("tags") or "").strip()
            if not res.get("ok") or not tags:
                return self._send({"ok": False, "error": res.get("error") or "没拿到指令"})
            set_song(sid, {"suno": tags, "genre": tags[:60], "state": "已定曲风"})
            return self._send({"ok": True, "tags": tags})
        if u.path == "/api/batch/write":
            bid = int(b["batch"])
            done, fail = 0, []
            for card in cards(bid):
                if len((card["lyrics"] or "").strip()) >= 10:
                    continue
                brief = (card["theme"] or "") + ("《" + card["title"] + "》" if card["title"] else "")
                res = suno_call("/api/lyrics", {"brief": brief, "style": card["genre"] or ""}, timeout=180)
                if res.get("ok") and res.get("text"):
                    set_song(card["id"], {"lyrics": res["text"].strip()})
                    done += 1
                else:
                    fail.append(f"{card['title']}:{res.get('error')}")
                time.sleep(0.8)
            return self._send({"ok": True, "written": done, "failed": fail[:5]})
        if u.path == "/api/batch/style":
            bid = int(b["batch"])
            done, fail = 0, []
            for card in cards(bid):
                if len((card["lyrics"] or "").strip()) < 10 or card["suno"]:
                    continue
                c = db()
                used = [o["core_sig"] for o in c.execute(
                    "select core_sig from songs where batch_id=? and core_sig!=''", (bid,)).fetchall()]
                c.close()
                text = f"《{card['title']}》{card['theme'] or ''}\n" + (card["lyrics"] or "")[:300]
                if used:
                    text = ("这批已用过的编制（主奏乐器/速度/人声）不要再重复：" +
                            "；".join(sorted(set(used))) + "\n" + text)
                res = suno_call("/api/prompt", {"text": text}, timeout=120)
                tags = (res.get("tags") or "").strip()
                if res.get("ok") and tags:
                    set_song(card["id"], {"suno": tags, "genre": tags[:60], "state": "已定曲风"})
                    done += 1
                else:
                    fail.append(f"{card['title']}:{res.get('error')}")
                time.sleep(0.8)
            return self._send({"ok": True, "styled": done, "failed": fail[:5]})
        if u.path == "/api/song/push":
            sid = int(b["id"])
            c = db()
            r = c.execute("select * from songs where id=?", (sid,)).fetchone()
            c.close()
            if not r:
                return self._send({"ok": False, "error": "卡片不存在"}, 404)
            if len((r["lyrics"] or "").strip()) < 10:
                return self._send({"ok": False, "error": "歌词太短，先写词"}, 400)
            if not (r["suno"] or r["genre"]):
                return self._send({"ok": False, "error": "缺 Suno 指令/曲风"}, 400)
            res = suno_call("/api/create", {"title": r["title"], "lyrics": r["lyrics"],
                                            "tags": r["suno"] or r["genre"]})
            if res.get("ok"):
                set_song(sid, {"state": "生成中"})
                c = db()
                c.execute("update songs set suno_job=?, updated=? where id=?",
                          (res.get("job"), time.time(), sid))
                c.commit()
                c.close()
            return self._send(res)
        if u.path == "/api/batch/push":
            bid = int(b["batch"])
            cs = [c for c in cards(bid) if c["state"] in ("待判定", "已定曲风") and not c["job"]]
            out = []
            for c in cs:
                if len((c["lyrics"] or "").strip()) < 10 or not (c["suno"] or c["genre"]):
                    out.append({"id": c["id"], "title": c["title"], "ok": False, "error": "缺歌词或指令"})
                    continue
                res = suno_call("/api/create", {"title": c["title"], "lyrics": c["lyrics"],
                                                "tags": c["suno"] or c["genre"]})
                if res.get("ok"):
                    set_song(c["id"], {"state": "生成中"})
                    cc = db()
                    cc.execute("update songs set suno_job=?, updated=? where id=?",
                               (res.get("job"), time.time(), c["id"]))
                    cc.commit()
                    cc.close()
                out.append({"id": c["id"], "title": c["title"], **res})
                time.sleep(1.2)
            return self._send({"ok": True, "results": out})
        if u.path == "/api/poll":
            bid = int(b["batch"])
            j = suno_call("/api/jobs")
            if not j.get("ok"):
                return self._send({"ok": False, "error": j.get("error")})
            jobs = {x["id"]: x for x in j.get("jobs") or []}
            cs = cards(bid)
            changed = 0
            c = db()
            for card in cs:
                jid = card["job"]
                if not jid or jid not in jobs:
                    continue
                job = jobs[jid]
                files = card["files"]
                if job.get("files"):
                    files = [{"name": f.get("name"), "i": f.get("i"),
                              "url": f"{suno_base()}/api/file?job={jid}&i={f.get('i')}"} for f in job["files"]]
                    changed += 1
                st = (job.get("status") or "").lower()
                state, note = card["state"], card["note"] or ""
                if st in ("done", "complete", "success") and files:
                    if card["state"] not in ("已定版", "已交付"):
                        state = "待挑版"
                    note = ""
                elif st in ("failed", "error", "cancelled"):
                    state = "已定曲风" if (card["lyrics"] and (card["suno"] or card["genre"])) else "待判定"
                    note = "生成失败：" + str(job.get("error") or st)
                elif st in ("running", "processing", "queued", "submitted"):
                    state = "生成中"
                c.execute("update songs set files=?, state=?, note=?, updated=? where id=?",
                          (json.dumps(files, ensure_ascii=False), state, note, time.time(), card["id"]))
            c.commit()
            c.close()
            return self._send({"ok": True, "changed": changed})
        if u.path == "/api/song/pick":
            sid = int(b["id"])
            idx = int(b.get("index") or 0)
            c = db()
            r = c.execute("select * from songs where id=?", (sid,)).fetchone()
            files = json.loads(r["files"]) if r["files"] else []
            if idx >= len(files):
                c.close()
                return self._send({"ok": False, "error": "版本不存在"}, 400)
            src = files[idx]
            # 从 suno-panel 直接下载到交付目录
            os.makedirs(SONG_OUT, exist_ok=True)
            safe = re.sub(r"[^\w\u4e00-\u9fa5-]", "", r["title"] or "未命名")[:40]
            ext = os.path.splitext(src.get("name") or "")[1] or ".mp3"
            dst = os.path.join(SONG_OUT, f"{safe}-{int(time.time())%100000:05d}{ext}")
            try:
                urllib.request.urlretrieve(f"{suno_base()}/api/file?job={r['suno_job']}&i={src['i']}&dl=1", dst)
            except Exception as e:
                c.close()
                return self._send({"ok": False, "error": f"下载失败 {e}"}, 500)
            c.execute("update songs set picked=?, delivered=?, state=?, updated=? where id=?",
                      (idx, dst, "已定版", time.time(), sid))
            c.commit()
            c.close()
            return self._send({"ok": True, "delivered": dst})
        if u.path == "/api/reveal":
            p = b.get("path") or SONG_OUT
            if os.path.exists(p):
                subprocess.Popen(["open", "-R" if os.path.isfile(p) else "", p])
            return self._send({"ok": True})
        return self._send({"ok": False, "error": "not found"}, 404)


def main():
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"歌曲工作台 → http://127.0.0.1:{PORT}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
