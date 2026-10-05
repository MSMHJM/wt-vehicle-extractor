# -*- coding: utf-8 -*-
import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRACTOR = os.path.join(HERE, "wt_extract.py")
UI = os.path.join(HERE, "wt_web.html")
CONFIG_PATH = os.path.join(HERE, "wt_config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            value = json.load(f)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_config(value):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


class TaskManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.task_id = 0
        self.lines = queue.Queue()
        self.status = "idle"
        self.code = None
        self.started = None
        self.finished = None

    def start(self, args, kind):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                raise RuntimeError("已有任务正在运行")
            self.task_id += 1
            self.status = "running"
            self.code = None
            self.started = time.time()
            self.finished = None
            self.proc = subprocess.Popen(args, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
            threading.Thread(target=self._read, args=(self.proc, self.task_id, kind), daemon=True).start()
            return self.task_id

    def _read(self, proc, task_id, kind):
        for line in proc.stdout:
            self.lines.put({"task": task_id, "kind": kind, "line": line.rstrip("\r\n")})
        code = proc.wait()
        with self.lock:
            if task_id == self.task_id:
                self.status = "done" if code == 0 else "failed"
                self.code = code
                self.finished = time.time()
        self.lines.put({"task": task_id, "kind": kind, "done": True, "code": code})

    def stop(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
                self.status = "stopping"
                return True
        return False

    def drain(self, task_id):
        result = []
        while True:
            try:
                item = self.lines.get_nowait()
            except queue.Empty:
                break
            if item.get("task") == task_id:
                result.append(item)
        return result

    def snapshot(self):
        with self.lock:
            return {"task": self.task_id, "status": self.status, "code": self.code, "started": self.started, "finished": self.finished}


manager = TaskManager()


def json_body(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    return json.loads(handler.rfile.read(length) or b"{}")


def send_json(handler, payload, code=200):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            try:
                with open(UI, "rb") as f:
                    data = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/status":
            send_json(self, manager.snapshot())
            return
        if path == "/api/config":
            send_json(self, load_config())
            return
        if path == "/api/events":
            query = urlparse(self.path).query
            task = int(query.split("task=", 1)[1] or manager.task_id) if "task=" in query else manager.task_id
            send_json(self, {"events": manager.drain(task), "snapshot": manager.snapshot()})
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = json_body(self)
            if path == "/api/config":
                config = load_config()
                config.update({key: body[key] for key in ("res", "blender", "out", "dae") if key in body})
                save_config(config)
                send_json(self, config)
                return
            if path == "/api/scan":
                config = load_config()
                res = body.get("res") or config.get("res", "")
                if not res:
                    raise ValueError("请先在页面填写游戏 res 目录")
                cmd = [sys.executable, EXTRACTOR, "--list-names", "--category", body.get("category", "飞机"), "--res", res]
                if body.get("refresh"):
                    cmd.append("--refresh-scan")
                task = manager.start(cmd, "scan")
                send_json(self, {"task": task})
                return
            if path == "/api/extract":
                config = load_config()
                name = str(body.get("name", "")).strip()
                res = body.get("res") or config.get("res", "")
                blender = body.get("blender") or config.get("blender", "")
                out = body.get("out") or config.get("out", os.path.join(HERE, "output"))
                if not name:
                    raise ValueError("缺少载具资源名")
                if not res or not blender:
                    raise ValueError("请先填写游戏 res 目录和 Blender 路径")
                save_config({**config, "res": res, "blender": blender, "out": out})
                cmd = [sys.executable, EXTRACTOR, "--name", name, "--res", res, "--lod", str(body.get("lod", 0)), "--out", out, "--blender", blender]
                if body.get("keepEffects"):
                    cmd.append("--keep-effects")
                if body.get("keepTemp"):
                    cmd.append("--keep-temp")
                task = manager.start(cmd, "extract")
                send_json(self, {"task": task})
                return
            if path == "/api/stop":
                send_json(self, {"stopped": manager.stop()})
                return
            if path == "/api/diagnose":
                config = load_config()
                checks = {"extractor": os.path.isfile(EXTRACTOR), "res": os.path.isdir(body.get("res") or config.get("res", "")), "blender": os.path.isfile(body.get("blender") or config.get("blender", ""))}
                send_json(self, checks)
                return
            if path == "/api/open-output":
                output = os.path.abspath(body.get("out", os.path.join(HERE, "output")))
                if os.path.isdir(output):
                    os.startfile(output)
                    send_json(self, {"opened": True})
                else:
                    send_json(self, {"opened": False}, 404)
                return
            if path == "/api/clear-cache":
                removed = 0
                cache_dir = os.path.join(HERE, ".wt-cache")
                for name in os.listdir(cache_dir) if os.path.isdir(cache_dir) else []:
                    if name.startswith("wt_names_"):
                        try:
                            os.remove(os.path.join(cache_dir, name))
                            removed += 1
                        except OSError:
                            pass
                send_json(self, {"removed": removed})
                return
            self.send_error(404)
        except (ValueError, OSError, RuntimeError) as exc:
            send_json(self, {"error": str(exc)}, 400)


def main():
    port = 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"WT Web UI: {url}", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
