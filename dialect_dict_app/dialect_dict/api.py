"""HTTP 接口层（纯标准库 http.server）。

- 公开接口无需登录；编辑接口要求 editor；审核/发布/撤权要求 approver。
- 每个请求独立数据库连接；词库只存在于服务端 SQLite。
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import db, services as S, search as search_mod
from . import publish as publish_mod

ROUTES = []


def route(method, pattern, roles=None):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn, roles))
        return fn
    return deco


# ---------------------------------------------------------------- 公开

@route("GET", r"^/api/meta$")
def meta(conn, user, q, b):
    return S.meta(conn)


@route("GET", r"^/api/public/search$")
def public_search(conn, user, q, b):
    return {"results": search_mod.search(
        conn, q.get("q", [""])[0], q.get("mode", ["auto"])[0])}


@route("GET", r"^/api/public/search/compare$")
def public_compare(conn, user, q, b):
    return search_mod.compare(conn, q.get("q", [""])[0])


@route("GET", r"^/api/public/entries/(?P<eid>[^/]+)$")
def public_entry(conn, user, q, b, eid):
    v = q.get("v", [None])[0]
    res = publish_mod.serve_public(conn, eid, int(v) if v else None)
    if res is None:
        raise S.NotFound("词条未发布: %s" % eid)
    return res


@route("GET", r"^/api/public/entries/(?P<eid>[^/]+)/versions$")
def public_entry_versions(conn, user, q, b, eid):
    return {"versions": publish_mod.list_published_versions(conn, eid)}


@route("GET", r"^/api/public/entries/(?P<eid>[^/]+)/pron$")
def public_entry_pron(conn, user, q, b, eid):
    scheme = q.get("scheme", ["jyutping"])[0]
    return {"scheme": scheme,
            "items": S.pronunciation_display(conn, eid, scheme)}


@route("GET", r"^/api/public/senses/(?P<sid>[^/]+)$")
def public_sense(conn, user, q, b, sid):
    return S.public_sense_view(conn, sid)


@route("GET", r"^/api/public/articles/(?P<aid>[^/]+)$")
def public_article(conn, user, q, b, aid):
    return S.get_article(conn, aid)


# ---------------------------------------------------------------- 后台（读）

@route("GET", r"^/api/entries$", roles=("editor", "approver"))
def list_entries(conn, user, q, b):
    return {"entries": S.list_entries(conn)}


@route("GET", r"^/api/entries/(?P<eid>[^/]+)$", roles=("editor", "approver"))
def entry_detail(conn, user, q, b, eid):
    return S.get_entry_detail(conn, eid)


@route("GET", r"^/api/entries/(?P<eid>[^/]+)/homophone-candidates$",
       roles=("editor", "approver"))
def homophone_candidates(conn, user, q, b, eid):
    return {"candidates": S.homophone_candidates(conn, eid)}


@route("GET", r"^/api/audit$", roles=("approver",))
def audit(conn, user, q, b):
    return {"log": [dict(r) for r in conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")]}


@route("GET", r"^/api/index/tasks$", roles=("editor", "approver"))
def index_tasks(conn, user, q, b):
    return {"tasks": [dict(r) for r in conn.execute(
        "SELECT * FROM index_tasks ORDER BY id DESC LIMIT 100")]}


# ---------------------------------------------------------------- 后台（编辑）

@route("POST", r"^/api/entries$", roles=("editor",))
def create_entry(conn, user, q, b):
    return {"id": S.create_entry(conn, user, b.get("headword", ""),
                                 b.get("note", ""))}


@route("PUT", r"^/api/entries/(?P<eid>[^/]+)$", roles=("editor",))
def update_entry(conn, user, q, b, eid):
    S.update_entry(conn, user, eid, b.get("headword"), b.get("note"))
    return {"ok": True}


@route("POST", r"^/api/entries/(?P<eid>[^/]+)/submit$", roles=("editor",))
def submit_entry(conn, user, q, b, eid):
    S.submit_entry(conn, user, eid)
    return {"ok": True}


@route("POST", r"^/api/entries/(?P<eid>[^/]+)/pronunciations$", roles=("editor",))
def add_pron(conn, user, q, b, eid):
    return {"id": S.add_pronunciation(conn, user, eid, b["scheme"],
                                      b["text"], b.get("region_id"))}


@route("POST", r"^/api/entries/(?P<eid>[^/]+)/pronunciations/convert$",
       roles=("editor",))
def convert_pron(conn, user, q, b, eid):
    return {"made": S.convert_pronunciations(conn, user, eid, b["to_scheme"])}


@route("POST", r"^/api/entries/(?P<eid>[^/]+)/senses$", roles=("editor",))
def add_sense(conn, user, q, b, eid):
    return {"id": S.add_sense(conn, user, eid, b["definition"],
                              b.get("usage_note", ""), b.get("region_id"))}


@route("PUT", r"^/api/senses/(?P<sid>[^/]+)$", roles=("editor",))
def update_sense(conn, user, q, b, sid):
    return {"version": S.update_sense(
        conn, user, sid, b.get("definition"), b.get("usage_note"),
        b["region_id"] if "region_id" in b else "__keep__")}


@route("DELETE", r"^/api/senses/(?P<sid>[^/]+)$", roles=("editor",))
def delete_sense(conn, user, q, b, sid):
    return S.delete_sense(conn, user, sid)


@route("POST", r"^/api/senses/(?P<sid>[^/]+)/examples$", roles=("editor",))
def add_example(conn, user, q, b, sid):
    return {"id": S.add_example(conn, user, sid, b["text"],
                                b.get("translation", ""), b.get("source_id"),
                                b.get("region_id"))}


@route("PUT", r"^/api/examples/(?P<xid>[^/]+)$", roles=("editor",))
def update_example(conn, user, q, b, xid):
    return {"version": S.update_example(
        conn, user, xid, b.get("text"), b.get("translation"),
        b.get("source_id", "__keep__"), b.get("region_id", "__keep__"))}


@route("POST", r"^/api/examples/(?P<xid>[^/]+)/audio$", roles=("editor",))
def attach_audio(conn, user, q, b, xid):
    return {"id": S.attach_audio(conn, user, xid, b["url"],
                                 b.get("transcript", ""))}


@route("POST", r"^/api/entries/(?P<eid>[^/]+)/relations$", roles=("editor",))
def add_relation(conn, user, q, b, eid):
    return {"id": S.add_relation(conn, user, eid, b["to_entry"], b["type"],
                                 b.get("note", ""))}


@route("DELETE", r"^/api/relations/(?P<rid>[^/]+)$", roles=("editor",))
def remove_relation(conn, user, q, b, rid):
    S.remove_relation(conn, user, rid)
    return {"ok": True}


@route("POST", r"^/api/entries/merge$", roles=("editor",))
def merge_entries(conn, user, q, b):
    return S.merge_entries(conn, user, b["source_id"], b["target_id"])


@route("POST", r"^/api/entries/(?P<eid>[^/]+)/split$", roles=("editor",))
def split_entry(conn, user, q, b, eid):
    return S.split_entry(conn, user, eid, b["sense_ids"],
                         b.get("restore_entry_id"), b.get("new_headword"))


@route("POST", r"^/api/articles$", roles=("editor",))
def create_article(conn, user, q, b):
    return {"id": S.create_article(conn, user, b["title"], b.get("body", ""),
                                   b.get("citations", []))}


@route("GET", r"^/api/articles/(?P<aid>[^/]+)$", roles=("editor", "approver"))
def get_article(conn, user, q, b, aid):
    return S.get_article(conn, aid)


@route("POST", r"^/api/sources$", roles=("editor",))
def add_source(conn, user, q, b):
    return {"id": S.add_source(conn, user, b["title"], b.get("author", ""),
                               b.get("year"), b.get("note", ""))}


@route("POST", r"^/api/regions$", roles=("editor",))
def add_region(conn, user, q, b):
    return {"id": S.add_region(conn, user, b["name"], b.get("note", ""))}


# ---------------------------------------------------------------- 后台（审校）

@route("POST", r"^/api/entries/(?P<eid>[^/]+)/approve$", roles=("approver",))
def approve_entry(conn, user, q, b, eid):
    S.approve_entry(conn, user, eid)
    return {"ok": True}


@route("POST", r"^/api/entries/(?P<eid>[^/]+)/publish$", roles=("approver",))
def publish_entry(conn, user, q, b, eid):
    return {"version": S.publish_entry(conn, user, eid)}


@route("POST", r"^/api/audio/(?P<auid>[^/]+)/revoke$", roles=("approver",))
def revoke_audio(conn, user, q, b, auid):
    S.revoke_audio(conn, user, auid, b.get("reason", ""))
    return {"ok": True}


# ---------------------------------------------------------------- 服务器

CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript", ".css": "text/css",
                 ".wav": "audio/wav", ".mp3": "audio/mpeg",
                 ".json": "application/json"}


def make_handler(db_path, static_dir):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send_json(self, status, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_static(self, path):
            if path == "/":
                path = "/public.html"
            elif path == "/admin":
                path = "/admin.html"
            elif path.startswith("/static/"):
                path = path[len("/static"):]   # static_dir 即静态根
            safe = os.path.normpath(path).lstrip("/")
            full = os.path.normpath(os.path.join(static_dir, safe))
            if not full.startswith(os.path.abspath(static_dir)) \
                    or not os.path.isfile(full):
                self._send_json(404, {"error": "not found"})
                return
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type",
                             CONTENT_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if method == "GET" and not path.startswith("/api/"):
                self._send_static(path)
                return
            body = {}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "请求体不是合法 JSON"})
                    return
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.startswith("Bearer ") else ""
            conn = db.connect(db_path)
            try:
                user = S.get_user_by_token(conn, token) if token else None
                for m, rx, fn, roles in ROUTES:
                    if m != method:
                        continue
                    mt = rx.match(path)
                    if not mt:
                        continue
                    if roles:
                        if not user:
                            self._send_json(401, {"error": "未登录"})
                            return
                        if user["role"] not in roles:
                            self._send_json(403, {"error": "权限不足：需要 %s"
                                                % "/".join(roles)})
                            return
                    try:
                        res = fn(conn, user, query, body, **mt.groupdict())
                        conn.commit()
                        self._send_json(200, res)
                    except S.NotFound as e:
                        conn.rollback()
                        self._send_json(404, {"error": str(e)})
                    except PermissionError as e:
                        conn.rollback()
                        self._send_json(403, {"error": str(e)})
                    except S.Conflict as e:
                        conn.rollback()
                        self._send_json(409, {"error": str(e)})
                    except (ValueError, KeyError) as e:
                        conn.rollback()
                        self._send_json(400, {"error": "参数错误: %s" % e})
                    return
                self._send_json(404, {"error": "not found"})
            finally:
                conn.close()

        do_GET = lambda s: s._handle("GET")
        do_POST = lambda s: s._handle("POST")
        do_PUT = lambda s: s._handle("PUT")
        do_DELETE = lambda s: s._handle("DELETE")

    return Handler


def create_server(db_path, static_dir, host="127.0.0.1", port=8000):
    return ThreadingHTTPServer((host, port), make_handler(db_path, static_dir))
