# -*- coding: utf-8 -*-
"""HTTP API：ThreadingHTTPServer + 纯标准库路由。

角色在服务端逐端点强制：editor 可编辑草稿，approver 可发布/撤权，公开端点只读发布闭包。
"""
import json, re, os, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from .service import Service, Forbidden, NotFound, BadRequest
from .indexer import Indexer
from . import search as search_mod
from .phonology import SCHEMES

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

class Ctx:
    def __init__(self, service, indexer):
        self.svc, self.idx = service, indexer

# ---------------- 端点 ----------------
def h_login(c, req):
    u = c.svc.user(req.body.get("token", ""))
    if not u:
        raise Forbidden("令牌无效")
    return {"username": u["username"], "role": u["role"]}

def h_search(c, req):
    return search_mod.search(c.svc.db, c.svc, req.qs.get("q", ""))

def h_compare(c, req):
    return search_mod.compare(c.svc.db, c.svc, req.qs.get("q", ""))

def h_entry(c, req, entry_id):
    v = req.qs.get("v")
    return c.svc.entry_view(entry_id, int(v) if v else None)

def h_citation(c, req, cid):
    return c.svc.resolve_citation(cid)

def h_schemes(c, req):
    return SCHEMES

def h_convert(c, req, pron_id):
    return c.svc.convert_pron(pron_id, req.body["target"])

def h_tasks(c, req):
    rows = c.svc.db.query("SELECT * FROM index_tasks ORDER BY id DESC LIMIT 50")
    return {"published": c.svc.db.get_pointer("published"),
            "indexed": c.svc.db.get_pointer("indexed"),
            "tasks": [dict(r) for r in rows]}

def h_draft_entries(c, req):
    rows = c.svc.db.query("SELECT e.* FROM entries e WHERE version=(SELECT MAX(version) "
                          "FROM entries WHERE id=e.id) ORDER BY updated_at DESC")
    return [dict(r) for r in rows]

def h_draft_entry(c, req, entry_id):
    cur = lambda t: [dict(r) for r in c.svc.db.query(
        f"SELECT x.* FROM {t} x WHERE x.version=(SELECT MAX(version) FROM {t} WHERE id=x.id)")]
    e = c.svc._cur("entries", entry_id)
    if not e:
        raise NotFound("词条不存在")
    eid = e["id"]
    senses = [r for r in cur("senses") if r["entry_id"] == eid and not r["deleted"]]
    sense_ids = {r["id"] for r in senses}
    return {"entry": dict(e),
            "forms": [r for r in cur("forms") if r["entry_id"] == eid and not r["deleted"]],
            "prons": [r for r in cur("prons") if r["entry_id"] == eid and not r["deleted"]],
            "senses": senses,
            "examples": [r for r in cur("examples")
                         if not r["deleted"] and r["sense_id"] in sense_ids],
            "relations": [r for r in cur("relations")
                          if not r["deleted"] and (r["src"] == eid or r["dst"] == eid)]}

def h_create_entry(c, req):
    eid = c.svc.create_entry(req.user["username"], req.body["headword"],
                             req.body.get("context_note", ""))
    return {"id": eid}

def h_update_entry(c, req, entry_id):
    c.svc.update_entry(req.user["username"], entry_id, **req.body)
    return {"ok": True}

def h_add_child(c, req, entry_id, kind):
    b, u = req.body, req.user["username"]
    if kind == "forms":
        fid = c.svc.add_form(u, entry_id, b["text"], b.get("script", "simp"),
                             b.get("is_standard", 0), b.get("note", ""))
        return {"id": fid}
    if kind == "prons":
        return {"id": c.svc.add_pron(u, entry_id, b["scheme"], b["value"],
                                     b.get("region", ""), b.get("source_id"))}
    if kind == "senses":
        return {"id": c.svc.add_sense(u, entry_id, b["num"], b["definition"],
                                      b.get("pos", ""), b.get("region", ""))}
    if kind == "relations":
        return {"id": c.svc.add_relation(u, entry_id, b["dst"], b["type"], b.get("note", ""))}
    raise BadRequest("未知子资源")

def h_add_example(c, req, sense_id):
    b = req.body
    return {"id": c.svc.add_example(req.user["username"], sense_id, b["text"],
            b.get("translation", ""), b.get("region", ""), b.get("source_id"), b.get("media_id"))}

def h_update_sense(c, req, sense_id):
    c.svc.update_sense(req.user["username"], sense_id, **req.body)
    return {"ok": True}

def h_delete_sense(c, req, sense_id):
    c.svc.delete_sense(req.user["username"], sense_id)
    return {"ok": True}

def h_merge(c, req, entry_id):
    c.svc.merge_entries(req.user["username"], entry_id, req.body["into"])
    return {"ok": True}

def h_split(c, req, entry_id):
    c.svc.split_entry(req.user["username"], entry_id, req.body.get("sense_ids", []))
    return {"ok": True}

def h_publish(c, req):
    vid = c.svc.publish(req.user["username"], req.body.get("note", ""))
    return {"version_id": vid}

def h_revoke(c, req, media_id):
    c.svc.revoke_media(req.user["username"], media_id)
    return {"ok": True}

def h_add_source(c, req):
    b = req.body
    return {"id": c.svc.add_source(req.user["username"], b["title"], b.get("author", ""),
                                   b.get("year"), b.get("license", ""))}

def h_add_media(c, req):
    b = req.body
    return {"id": c.svc.add_media(req.user["username"], b["path"], b.get("transcript", ""))}

def h_add_citation(c, req):
    return {"id": c.svc.create_citation(req.user["username"], req.body["article_uri"],
                                        req.body["sense_id"])}

ROUTES = [
    ("POST", r"/api/login",                          h_login,        None),
    ("GET",  r"/api/search",                         h_search,       None),
    ("GET",  r"/api/compare",                        h_compare,      None),
    ("GET",  r"/api/entry/([\w-]+)",                 h_entry,        None),
    ("GET",  r"/api/citation/([\w-]+)",              h_citation,     None),
    ("GET",  r"/api/schemes",                        h_schemes,      None),
    ("POST", r"/api/prons/([\w-]+)/convert",         h_convert,      None),
    ("GET",  r"/api/tasks",                          h_tasks,        "editor"),
    ("GET",  r"/api/draft/entries",                  h_draft_entries,"editor"),
    ("GET",  r"/api/draft/entry/([\w-]+)",           h_draft_entry,  "editor"),
    ("POST", r"/api/entries",                        h_create_entry, "editor"),
    ("POST", r"/api/entries/([\w-]+)/update",        h_update_entry, "editor"),
    ("POST", r"/api/entries/([\w-]+)/(forms|prons|senses|relations)", h_add_child, "editor"),
    ("POST", r"/api/senses/([\w-]+)/examples",       h_add_example,  "editor"),
    ("POST", r"/api/senses/([\w-]+)/update",         h_update_sense, "editor"),
    ("POST", r"/api/senses/([\w-]+)/delete",         h_delete_sense, "editor"),
    ("POST", r"/api/entries/([\w-]+)/merge",         h_merge,        "editor"),
    ("POST", r"/api/entries/([\w-]+)/split",         h_split,        "editor"),
    ("POST", r"/api/sources",                        h_add_source,   "editor"),
    ("POST", r"/api/media",                          h_add_media,    "editor"),
    ("POST", r"/api/citations",                      h_add_citation, "editor"),
    ("POST", r"/api/publish",                        h_publish,      "approver"),
    ("POST", r"/api/media/([\w-]+)/revoke",          h_revoke,       "approver"),
]

class Req:
    def __init__(self, qs, body, user):
        self.qs, self.body, self.user = qs, body, user

def make_handler(ctx):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默
            pass
        def _send(self, code, obj, ctype="application/json; charset=utf-8"):
            data = obj.encode() if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def _static(self, path):
            p = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
            if not p.startswith(STATIC) or not os.path.isfile(p):
                return self._send(404, {"error": "not found"})
            ctype = "text/html; charset=utf-8" if p.endswith(".html") else "application/octet-stream"
            self._send(200, open(p, "rb").read().decode("utf-8", "replace"), ctype)
        def _route(self, method):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if method == "GET" and path == "/":
                return self._static("public.html")
            if method == "GET" and path == "/editor":
                return self._static("editor.html")
            if method == "GET" and path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
            token = self.headers.get("X-Token", "")
            user = ctx.svc.user(token)
            for m, pat, fn, role in ROUTES:
                if m != method:
                    continue
                g = re.fullmatch(pat, path)
                if not g:
                    continue
                try:
                    if role == "editor" and (not user or user["role"] not in ("editor", "approver")):
                        raise Forbidden("需要编辑或审校角色")
                    if role == "approver" and (not user or user["role"] != "approver"):
                        raise Forbidden("需要审校（批准）角色")
                    return self._send(200, fn(ctx, Req(qs, body, user), *g.groups()))
                except Forbidden as e:
                    return self._send(403, {"error": str(e)})
                except NotFound as e:
                    return self._send(404, {"error": str(e)})
                except (BadRequest, KeyError, ValueError) as e:
                    return self._send(400, {"error": str(e)})
            self._send(404, {"error": "no route"})
        def do_GET(self):  self._route("GET")
        def do_POST(self): self._route("POST")
    return H

def serve(service, indexer, port=8000):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Ctx(service, indexer)))
    return srv
