#!/usr/bin/env python3
"""Small token-protected HTTP service for cached, on-demand Pass-2 sections.

This intentionally uses only the standard library. It has no TLS, accounts, or
multi-process coordination; place it behind a trusted reverse proxy for real
internet exposure and run one process per database/cache namespace.
"""

import argparse
import html
import json
import os
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import deep_study

PROMPT_VERSION = "pass2-v1"
DAILY_CAP = int(os.environ.get("READER_DAILY_SECTION_CAP", "150"))
CONCURRENCY = 2


def _reader_models():
    raw = os.environ.get("READER_MODELS", "ds-direct")
    models = tuple(item.strip() for item in raw.split(",") if item.strip())
    return models or ("ds-direct",)


def _conn():
    return db.ensure(db.get_connection())


class ReaderStore:
    def list_outlines(self):
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT o.arxiv_id, p.title, o.outline, o.source_fingerprint,
                                      o.model, o.created_at
                                 FROM reader_outline o JOIN papers p USING (arxiv_id)
                                ORDER BY o.created_at DESC""")
                return cur.fetchall()
        finally:
            conn.close()

    def get_outline(self, arxiv_id):
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT p.title, o.outline, o.source_fingerprint, o.model
                                 FROM reader_outline o JOIN papers p USING (arxiv_id)
                                WHERE o.arxiv_id=%s""", (arxiv_id,))
                return cur.fetchone()
        finally:
            conn.close()

    def get_cache(self, key):
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT status, html, error, updated_at
                                 FROM reader_section_cache
                                WHERE arxiv_id=%s AND section_index=%s
                                  AND source_fingerprint=%s AND prompt_version=%s""", key)
                return cur.fetchone()
        finally:
            conn.close()

    def claim_cache(self, key):
        """Atomically claim a new/expired section; return (claimed, row)."""
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO reader_section_cache
                    (arxiv_id, section_index, source_fingerprint, prompt_version, status)
                    VALUES (%s,%s,%s,%s,'queued') ON CONFLICT DO NOTHING""", key)
                inserted = cur.rowcount == 1
                cur.execute("""SELECT status, html, error, updated_at
                                 FROM reader_section_cache
                                WHERE arxiv_id=%s AND section_index=%s
                                  AND source_fingerprint=%s AND prompt_version=%s
                                FOR UPDATE""", key)
                row = cur.fetchone()
                claimed = False
                if inserted:
                    claimed = True
                elif row[0] in ("queued", "running", "failed"):
                    cur.execute("""UPDATE reader_section_cache SET status='queued',
                                      html=NULL, error=NULL, updated_at=NOW()
                                    WHERE arxiv_id=%s AND section_index=%s
                                      AND source_fingerprint=%s AND prompt_version=%s
                                      AND updated_at < NOW() - INTERVAL '10 minutes'""", key)
                    claimed = cur.rowcount == 1
                if claimed:
                    row = ("queued", None, None, None)
            conn.commit()
            return claimed, row
        finally:
            conn.close()

    def mark_running(self, key):
        self._mark(key, "running", None, None)

    def mark_done(self, key, fragment):
        self._mark(key, "done", fragment, None)

    def mark_failed(self, key, error):
        self._mark(key, "failed", None, str(error)[:500])

    def _mark(self, key, status, fragment, error):
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""UPDATE reader_section_cache
                                  SET status=%s, html=%s, error=%s, updated_at=NOW()
                                WHERE arxiv_id=%s AND section_index=%s
                                  AND source_fingerprint=%s AND prompt_version=%s""",
                            (status, fragment, error, *key))
            conn.commit()
        finally:
            conn.close()

    def consume_daily_cap(self):
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO reader_section_usage(usage_date, generated_count)
                    VALUES (CURRENT_DATE, 1)
                    ON CONFLICT (usage_date) DO UPDATE
                    SET generated_count=reader_section_usage.generated_count+1
                    WHERE reader_section_usage.generated_count < %s
                    RETURNING generated_count""", (DAILY_CAP,))
                accepted = cur.fetchone() is not None
            conn.commit()
            return accepted
        finally:
            conn.close()


class RequestLimiter:
    def __init__(self, per_ip=10):
        self.per_ip = per_ip
        self.lock = threading.Lock()
        self.requests = defaultdict(deque)

    def allow(self, ip):
        now = time.time()
        with self.lock:
            q = self.requests[ip]
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= self.per_ip:
                return False
            q.append(now)
            return True


class SectionCoordinator:
    def __init__(self, store, generator=None, daily_cap=True, models=None):
        self.store = store
        self.generator = generator
        self.models = tuple(models) if models is not None else _reader_models()
        self.slots = threading.BoundedSemaphore(CONCURRENCY)
        self.limiter = RequestLimiter(10)
        self.daily_cap = daily_cap

    def request(self, arxiv_id, section_index, ip="unknown"):
        outline_row = self.store.get_outline(arxiv_id)
        if not outline_row:
            return 404, {"status": "failed", "error": "outline not found"}
        _, outline, fingerprint, model = outline_row
        sections = outline.get("big_sections") or []
        if not isinstance(section_index, int) or section_index < 0 or section_index >= len(sections):
            return 400, {"status": "failed", "error": "invalid section index"}
        if not self.limiter.allow(ip):
            return 429, {"status": "failed", "error": "per-IP section request limit exceeded"}
        key = (arxiv_id, section_index, fingerprint, PROMPT_VERSION)
        cached = self.store.get_cache(key)
        if cached and cached[0] == "done":
            return 200, {"status": "done", "html": cached[1]}
        if cached and cached[0] in ("queued", "running"):
            return 202, {"status": cached[0]}
        if cached and cached[0] == "failed" and cached[3] is not None:
            age = time.time() - cached[3].timestamp() if hasattr(cached[3], "timestamp") else 0
            if age < 60:
                return 200, {"status": "failed", "error": cached[2] or "generation failed"}
        if not self.slots.acquire(blocking=False):
            return 429, {"status": "failed", "error": "reader generation busy; retry shortly"}
        claimed, row = self.store.claim_cache(key)
        if not claimed:
            self.slots.release()
            if row[0] == "done":
                return 200, {"status": "done", "html": row[1]}
            return 202, {"status": row[0]}
        if self.daily_cap and not self.store.consume_daily_cap():
            self.store.mark_failed(key, "daily reader generation cap reached")
            self.slots.release()
            return 429, {"status": "failed", "error": "daily reader generation cap reached"}
        thread = threading.Thread(target=self._worker,
                                  args=(key, outline, section_index), daemon=True)
        thread.start()
        return 202, {"status": "queued"}

    def _worker(self, key, outline, section_index):
        try:
            self.store.mark_running(key)
            fragment = self._generate_with_fallback(outline, section_index)
            self.store.mark_done(key, fragment)
        except Exception as exc:
            self.store.mark_failed(key, exc)
        finally:
            self.slots.release()

    def _generate_with_fallback(self, outline, section_index):
        errors = []
        for model in self.models:
            try:
                if self.generator is None:
                    fragment = self._generate(outline, section_index, model)
                else:
                    fragment = self.generator(outline, section_index, model)
                if not fragment:
                    raise RuntimeError("section contained no explainable source text")
                print(f"[reader][section] model={model} outcome=success", flush=True)
                return fragment
            except Exception as exc:
                errors.append(f"{model}: {type(exc).__name__}")
                print(f"[reader][section] model={model} outcome=failed error={type(exc).__name__}",
                      flush=True)
        raise RuntimeError("all reader models failed (" + ", ".join(errors) + ")")

    @staticmethod
    def _generate(outline, section_index, model):
        section = outline["big_sections"][section_index]
        chunks = {item["index"]: item for item in outline.get("source_chunks") or []}
        html_parts = []
        for mapped in section.get("chunks") or []:
            chunk = chunks.get(mapped["index"])
            if chunk:
                fragment, _ = deep_study.pass2_explain(chunk, model)
                if fragment:
                    html_parts.append(fragment)
        return deep_study.strip_dollar_in_svg_text("\n".join(html_parts))


def _page(title, body, token):
    return f'''<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:auto;padding:18px;line-height:1.55;background:#fafafa;color:#222}}
.card{{background:#fff;border:1px solid #ddd;border-radius:12px;padding:14px;margin:12px 0}}
button{{width:100%;text-align:left;padding:12px;border:0;border-radius:9px;background:#eef5ff;margin:5px 0;font-size:1rem}}
.meta{{color:#666;font-size:.9rem}} .result{{margin:8px 0;padding:10px;background:#fffdf5;border-radius:8px}}
table{{max-width:100%;overflow:auto}} svg{{max-width:100%;height:auto}}
</style><body>{body}</body></html>'''


class ReaderHandler(BaseHTTPRequestHandler):
    server_version = "PaperReader/1.0"

    def _app(self):
        return self.server.reader_app

    def _authorized(self):
        token = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("t", [""])[0]
        return token and token == self._app().token

    def _json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._authorized():
            return self._json(401, {"error": "invalid reader token"})
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            cards = []
            for aid, title, outline, fp, model, created in self._app().store.list_outlines():
                href = f"/p/{urllib.parse.quote(aid, safe='')}?t={urllib.parse.quote(self._app().token)}"
                cards.append(f'<div class="card"><a href="{href}"><b>{html.escape(title or aid)}</b></a>'
                             f'<div class="meta">{html.escape(aid)} · model {html.escape(model)}</div></div>')
            return self._html(200, _page("论文导读", "<h1>论文导读</h1>" + "".join(cards) or "<p>暂无大纲</p>", self._app().token).encode())
        if parsed.path.startswith("/p/"):
            aid = urllib.parse.unquote(parsed.path[3:])
            row = self._app().store.get_outline(aid)
            if not row:
                return self._json(404, {"error": "outline not found"})
            title, outline, _, _ = row
            buttons = []
            for section in outline.get("big_sections") or []:
                idx = section["index"]
                result_id = f"result-{idx}"
                buttons.append(f'<div class="card"><button data-index="{idx}" data-aid="{html.escape(aid)}">'
                               f'{idx + 1}. {html.escape(section.get("title") or "未命名章节")}</button>'
                               f'<div id="{result_id}" class="result"></div></div>')
            script = f'''<script>
const token={json.dumps(self._app().token)}; const aid={json.dumps(aid)};
document.querySelectorAll('button[data-index]').forEach(b=>{{
 b.onclick=async()=>{{const i=Number(b.dataset.index), box=document.getElementById('result-'+i);
  if(box.dataset.started)return; box.dataset.started='1'; box.textContent='已排队，正在生成…';
  const r=await fetch('/api/section?t='+encodeURIComponent(token),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{arxiv_id:aid,section_index:i}})}});
  let d=await r.json(); if(d.html){{box.innerHTML=d.html;return;}}
  if(d.status==='failed'){{box.textContent=d.error||'生成失败';return;}}
  const poll=setInterval(async()=>{{const q=await fetch('/api/section?'+new URLSearchParams({{t:token,arxiv_id:aid,section_index:i}})); const x=await q.json();
    if(x.status==='done'||x.status==='failed'){{clearInterval(poll); if(x.html)box.innerHTML=x.html; else box.textContent=x.error||'生成失败';}}
  }},3000);
 }};
}});
</script>'''
            body = f'<p><a href="/?t={urllib.parse.quote(self._app().token)}">← 返回列表</a></p>'
            body += f'<h1>{html.escape(title or aid)}</h1><p class="meta">{html.escape(aid)}</p>'
            body += f'<p>{html.escape(outline.get("hook") or "")}</p><p>{html.escape(outline.get("takeaway") or "")}</p>'
            return self._html(200, _page(title or aid, body + "".join(buttons) + script, self._app().token).encode())
        if parsed.path == "/api/section":
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                status, payload = self._app().status(qs.get("arxiv_id", [""])[0], int(qs.get("section_index", [""])[0]))
            except (TypeError, ValueError):
                return self._json(400, {"status": "failed", "error": "invalid section index"})
            return self._json(status, payload)
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._json(401, {"error": "invalid reader token"})
        if urllib.parse.urlparse(self.path).path != "/api/section":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            aid, index = body.get("arxiv_id"), int(body.get("section_index"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return self._json(400, {"status": "failed", "error": "invalid JSON request"})
        status, payload = self._app().request(aid, index, self.client_address[0])
        return self._json(status, payload)


class ReaderApp:
    def __init__(self, token, store=None, coordinator=None):
        if not token:
            raise ValueError("READER_TOKEN is required")
        self.token = token
        self.store = store or ReaderStore()
        self.coordinator = coordinator or SectionCoordinator(self.store)

    def request(self, aid, index, ip):
        return self.coordinator.request(aid, index, ip)

    def status(self, aid, index):
        row = self.store.get_outline(aid)
        if not row:
            return 404, {"status": "failed", "error": "outline not found"}
        _, outline, fp, _ = row
        if not isinstance(index, int) or index < 0 or index >= len(outline.get("big_sections") or []):
            return 400, {"status": "failed", "error": "invalid section index"}
        row = self.store.get_cache((aid, index, fp, PROMPT_VERSION))
        if not row:
            return 404, {"status": "failed", "error": "section has not been requested"}
        return (200, {"status": row[0], "html": row[1]} if row[0] == "done"
                else {"status": row[0], "error": row[2]} if row[0] == "failed"
                else {"status": row[0]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = os.environ.get("READER_TOKEN")
    app = ReaderApp(token)
    server = ThreadingHTTPServer((args.host, args.port), ReaderHandler)
    server.reader_app = app
    print(f"reader service listening on {args.host}:{args.port}; TLS is not provided", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
