# song-workbench

A zero-dependency local workbench that turns a spreadsheet of song ideas into a tracked production pipeline: **judge → write lyrics → pick a style → generate → pick a take → cover → deliver**, one card per song.

Built for people who produce music in batches and keep losing track of which song is at which stage — and who keep accidentally shipping two songs with the same arrangement.

![status](https://img.shields.io/badge/python-3.9%2B-blue) ![deps](https://img.shields.io/badge/dependencies-none%20(stdlib)-green)

## Why it exists

Batch songwriting breaks in three places:

1. **State lives in your head.** With 10 songs in a batch you forget what has lyrics, what has a style, what is waiting for a take choice.
2. **Arrangements converge.** You ask for "emotional mandopop" ten times and get ten variants of the same piano ballad. Nobody notices until the batch is done.
3. **The tools don't talk.** The generator, the lyric writer, the cover tool and the delivery folder are four separate things.

`song-workbench` is a thin scheduling layer over whatever generator you already use. It stores the pipeline state, enforces an arrangement-diversity rule, and calls out over HTTP — it does not touch your generator's code.

## How it works

```
songs.xlsx ──► import batch ──► cards (one per song)
                                   │
        ┌──────────────────────────┼───────────────────────────┐
        ▼                          ▼                           ▼
   write lyrics            pick a style                  push to generator
 (your LLM endpoint)   (diversity-checked)              (HTTP, queued)
        │                          │                           │
        └──────────► state machine ◄───────────────────────────┘
                          │
            待判定 → 已定曲风 → 生成中 → 待挑版 → 已定版 → 待封面 → 已交付
```

- **Storage**: a single SQLite file (`workbench.db`). No server, no migrations, no ORM.
- **UI**: one HTML page served by the stdlib `http.server`, card wall + detail panel.
- **Generator**: any HTTP service that exposes `POST /api/create` (lyrics + style) and `GET /api/jobs`. The port is discovered at runtime, never hardcoded.
- **LLM**: any OpenAI-compatible `/chat/completions` endpoint, configured through the generator panel or `LLM_BASE_URL` / `LLM_MODEL`.

## The arrangement-diversity rule

This is the part worth stealing even if you don't use the rest.

Every style string is reduced to a **core signature**: the instrument family, the tempo feel, and the vocal family.

| Raw style | Core signature |
|---|---|
| `piano, strings, female vocal, 70 bpm` | `钢琴,弦乐 \| 慢 \| 女声` |
| `electric guitar, drums, 128 bpm` | `电吉他,鼓 \| 快 \| 男声` |
| `fingerstyle acoustic ballad, 68 bpm, male soft vocal` | `木吉他 \| 慢 \| 男声` |

Instruments are grouped into families first (`drums`/`drum kit`/`electronic drums` are one family, `arp`/`pad` collapse into `synth`, `electric piano` into `piano`), English tags are mapped to the same families as their Chinese equivalents, and tempo falls into slow / mid / fast. Two songs in the same batch may not share a signature: the second occurrence is flagged in the UI and **blocked at push time**, before any credits are spent. Across batches the collision is only reported, never blocked.

A style string with no BPM number cannot be pushed. That single rule kills most accidental duplicates, because tempo is the dimension people silently reuse.

## Quick start

```bash
git clone https://github.com/<you>/song-workbench.git
cd song-workbench
python3 app.py           # http://127.0.0.1:7792/
```

Then import a spreadsheet with columns `歌名 / 歌词 / 指令 / 主题` (title / lyrics / style prompt / theme). Missing columns are tolerated; the importer maps by header name.

Point it at your generator:

```bash
SUNO_URL=http://127.0.0.1:8765 python3 app.py
```

Without `SUNO_URL` it probes `8765–8767` for a service answering `GET /api/outdir`, and caches the result for 20 seconds.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/cards?batch=N` | Cards of a batch, plus conflict groups and core-signature count |
| `POST` | `/api/batch/new` | Import a batch from an `.xlsx` (`{source, count, offset, name}`) |
| `POST` | `/api/song/write` | Write lyrics for one card |
| `POST` | `/api/song/style` | Pick a style for one card, avoiding siblings' signatures |
| `POST` | `/api/song/push` | Push one card to the generator (validated) |
| `POST` | `/api/batch/write` | Write lyrics for a whole batch |
| `POST` | `/api/batch/style` | Style a whole batch |
| `POST` | `/api/poll` | Reconcile job status into card states |
| `GET` | `/api/reveal` | Open the batch's output folder |

## Design decisions

- **No framework.** Two files of Python and one HTML page. It has to keep working in a year, on a laptop, without a `pip install`.
- **No write-back to the generator.** Scheduling is HTTP-only, so upgrading the generator can never break the workbench.
- **The database is a cache of intent, not of assets.** Audio stays wherever the generator wrote it; cards store job IDs only.
- **Import copies, never mutates.** The imported `.xlsx` is read once and never written.
- **State only moves forward.** Polling cannot demote a card from `已定版`; a failed job rolls back to `已定曲风` with the failure reason attached.

## Status

Used daily for batches of 9–11 songs. The pipeline is implemented end to end. Cover binding and the delivery ledger are the next milestone.

## License

MIT.
