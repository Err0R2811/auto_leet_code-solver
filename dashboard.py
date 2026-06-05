"""
LeetCode Auto-Solver Web Dashboard
Run: python dashboard.py  ->  open http://localhost:5000
"""
import os
import sys
import json
import time
import queue
import threading
import requests
from flask import Flask, render_template, jsonify, Response, request

from pathlib import Path
from agent import LeetCodeAgent, set_log_hook, log, DIFFICULTY, MAX_RETRIES, LANG, MODEL
from playwright.sync_api import sync_playwright


class _Resp:
    def __init__(self, resp):
        self._resp = resp
        self.status = resp.status_code
    def json(self):
        return self._resp.json()
    def text(self):
        return self._resp.text

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

class _ReqAdapter:
    def __init__(self, session):
        self._session = session
    def post(self, url, data=None, headers=None):
        merged = dict(_BROWSER_HEADERS)
        if headers:
            merged.update(headers)
        return _Resp(self._session.post(url, data=data, headers=merged))
    def get(self, url, headers=None):
        merged = dict(_BROWSER_HEADERS)
        if headers:
            merged.update(headers)
        return _Resp(self._session.get(url, headers=merged))

app = Flask(__name__)

_client_queues = []
_client_queues_lock = threading.Lock()
agent_state = {
    "status": "idle",
    "stats": {"accepted": 0, "failed": 0, "total": 0},
    "profile": None,
    "difficulty": DIFFICULTY,
    "current_problem": "",
    "lang": LANG,
    "model": MODEL,
    "max_retries": MAX_RETRIES,
    "manual_queue": 0,
}
agent_state_lock = threading.Lock()
_agent_instance = None
_agent_thread = None
_playwright_instance = None
_playwright_browser = None
_startup_cookies = None
_stop_flag = threading.Event()

_manual_queue = []
_manual_queue_lock = threading.Lock()
_manual_worker = None
_manual_worker_running = False

_skip_problems = set()
_skip_problems_lock = threading.Lock()
_SKIP_FILE = Path(__file__).parent / "skip_problems.json"

def _load_skip_problems():
    try:
        if _SKIP_FILE.exists():
            data = json.loads(_SKIP_FILE.read_text())
            if isinstance(data, list):
                _skip_problems.update(data)
    except Exception:
        pass

def _save_skip_problems():
    try:
        _SKIP_FILE.write_text(json.dumps(sorted(_skip_problems)))
    except Exception:
        pass


def _refresh_session_from_browser(session):
    """Pull fresh cookies from the Playwright-connected browser into a requests.Session."""
    try:
        if _playwright_browser is None:
            return False
        ctx = _playwright_browser.contexts[0]
        cookies = ctx.cookies()
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", "leetcode.com"))
        return True
    except Exception:
        return False


def dashboard_log(msg, tag=""):
    ts = time.strftime("%H:%M:%S")
    text = f"{tag} {msg}" if tag else str(msg)
    formatted = f"[{ts}] {text}"
    print(text, flush=True)
    with _client_queues_lock:
        dead = []
        for q in _client_queues:
            try:
                q.put_nowait(formatted)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _client_queues.remove(q)


def emit_state():
    with agent_state_lock:
        return agent_state.copy()


@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/history")
def history_page():
    items = []
    if _agent_instance:
        with agent_state_lock:
            items = list(reversed(_agent_instance.history))
    return render_template("history.html", history=json.dumps(items))


@app.route("/api/state")
def api_state():
    s = emit_state()
    with agent_state_lock:
        s["manual_queue"] = len(_manual_queue)
    return jsonify(s)


@app.route("/api/start", methods=["POST"])
def api_start():
    global _agent_thread, _playwright_instance
    with agent_state_lock:
        if agent_state["status"] == "running":
            return jsonify({"ok": False, "error": "Already running"})
        agent_state["status"] = "running"
        agent_state["stats"] = {"accepted": 0, "failed": 0, "total": 0}
        agent_state["current_problem"] = ""
    _stop_flag.clear()
    _agent_thread = threading.Thread(target=_run_agent, daemon=True)
    _agent_thread.start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if _agent_instance:
        _agent_instance._running = False
    _stop_flag.set()
    with agent_state_lock:
        agent_state["status"] = "stopped"
    return jsonify({"ok": True})


@app.route("/api/difficulty/<level>", methods=["POST"])
def api_difficulty(level):
    if level not in ("Easy", "Medium", "Hard"):
        return jsonify({"ok": False, "error": "Invalid difficulty"}), 400
    if _agent_instance:
        _agent_instance._next_diff = level
    with agent_state_lock:
        agent_state["difficulty"] = level
    dashboard_log(f"Difficulty set to {level}")
    return jsonify({"ok": True})


@app.route("/manual")
def manual_page():
    return render_template("manual.html")


@app.route("/api/manual-solve", methods=["POST"])
def api_manual_solve():
    try:
        data = request.get_json() or {}
        slug = (data.get("slug") or "").strip()
        if not slug:
            return jsonify({"ok": False, "error": "No problem slug"}), 400
        if "/problems/" in slug:
            slug = slug.split("/problems/")[1].split("/")[0].split("?")[0]
        slug = slug.strip("/")
        if not slug:
            return jsonify({"ok": False, "error": "Invalid slug"}), 400

        # If numeric, look up the slug from problem number
        if slug.isdigit():
            try:
                if _agent_instance:
                    _refresh_session_from_browser(_agent_instance.api._session)
                    found = _agent_instance.get_slug_by_number(slug)
                else:
                    lookup = _make_requests_agent()
                    found = lookup.get_slug_by_number(slug)
            except Exception as e:
                dashboard_log(f"Failed to resolve #{slug}: {e}")
                return jsonify({"ok": False, "error": f"Failed to look up problem #{slug}: {e}"}), 502
            if not found:
                return jsonify({"ok": False, "error": f"No problem found for #{slug}"}), 404
            slug = found["titleSlug"]
            dashboard_log(f"Resolved #{slug} -> {found['title']} ({found['difficulty']})")

        with _manual_queue_lock:
            _manual_queue.append(slug)
        with agent_state_lock:
            agent_state["manual_queue"] = len(_manual_queue)
        dashboard_log(f"Manual solve queued: {slug}")

        global _manual_worker, _manual_worker_running
        with _manual_queue_lock:
            if not _manual_worker_running:
                _manual_worker_running = True
                _manual_worker = threading.Thread(target=_manual_worker_loop, daemon=True)
                _manual_worker.start()
        return jsonify({"ok": True, "slug": slug})
    except Exception as e:
        dashboard_log(f"Manual solve route error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/history")
def api_history():
    if _agent_instance:
        with agent_state_lock:
            items = list(_agent_instance.history)
        return jsonify(items)
    return jsonify([])


@app.route("/api/profile")
def api_profile():
    with agent_state_lock:
        return jsonify(agent_state.get("profile"))


@app.route("/api/logs")
def api_logs():
    q = queue.Queue(maxsize=512)
    with _client_queues_lock:
        _client_queues.append(q)
    def generate():
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"data: {json.dumps({'msg': msg})}\n\n"
                except queue.Empty:
                    s = emit_state()
                    if s["status"] == "stopped":
                        break
                    yield f"data: {json.dumps({'msg': ''})}\n\n"
        except GeneratorExit:
            pass
        finally:
            with _client_queues_lock:
                try:
                    _client_queues.remove(q)
                except ValueError:
                    pass
    return Response(generate(), mimetype="text/event-stream")


def _make_csrf_refresher(agent, session):
    def refresher():
        for c in session.cookies:
            if c.name == "csrftoken" and "leetcode.com" in c.domain:
                agent.csrf = c.value
                return
        agent.csrf = ""
    return refresher


def _make_requests_agent():
    session = requests.Session()
    if not _refresh_session_from_browser(session) and _startup_cookies:
        for c in _startup_cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", "leetcode.com"))
    agent = LeetCodeAgent.__new__(LeetCodeAgent)
    agent.api = _ReqAdapter(session)
    agent.csrf = ""
    agent._refresh_csrf = _make_csrf_refresher(agent, session)
    agent._pw = None
    agent.browser = None
    agent.context = None
    agent.last_submit_time = 0
    agent.history = []
    agent._last_solution = {}
    agent._refresh_csrf()
    return agent


def _manual_worker_loop():
    global _manual_worker_running
    set_log_hook(dashboard_log)
    agent = _make_requests_agent()
    try:
        agent.check_signed_in()
    except Exception as e:
        dashboard_log(f"Manual agent init error: {e}")
        _manual_worker_running = False
        return

    while True:
        with _manual_queue_lock:
            if not _manual_queue:
                _manual_worker_running = False
                return
            slug = _manual_queue.pop(0)
        with agent_state_lock:
            agent_state["manual_queue"] = len(_manual_queue)

        dashboard_log(f"Manual solving: {slug}")
        try:
            detail = agent.get_problem_detail(slug)
            problem = {
                "titleSlug": slug,
                "title": detail.get("title", slug),
                "difficulty": detail.get("difficulty", "Hard"),
            }
            with agent_state_lock:
                agent_state["current_problem"] = problem["title"]
            result = agent.solve_problem(problem)
            dashboard_log(f"Manual solve {'accepted' if result == 'accepted' else 'failed'}: {slug}")
        except Exception as e:
            dashboard_log(f"Manual solve error: {e}")
        finally:
            with agent_state_lock:
                agent_state["current_problem"] = ""
                agent_state["manual_queue"] = len(_manual_queue)


def _run_agent():
    global _agent_instance
    set_log_hook(dashboard_log)
    try:
        _agent_instance = _make_requests_agent()
        _agent_instance._current_diff = DIFFICULTY
        _agent_instance._next_diff = None
        _agent_instance._running = True
        current_difficulty = _agent_instance._current_diff

        with agent_state_lock:
            agent_state["difficulty"] = current_difficulty

        username = _agent_instance.check_signed_in()
        if username:
            try:
                profile = _agent_instance.get_user_profile(username)
                with agent_state_lock:
                    agent_state["profile"] = profile
                dashboard_log(f"Profile: {profile.get('username')} (rank {profile.get('profile', {}).get('ranking', '?')})")
            except Exception as e:
                dashboard_log(f"Profile fetch failed: {e}")

        dashboard_log(f"Difficulty: {current_difficulty} | Lang: {LANG} | Retries: {MAX_RETRIES}")
        dashboard_log("Dashboard controls active")

        stats = {"accepted": 0, "failed": 0, "total": 0}

        def fetch_problems():
            dashboard_log(f"Fetching {current_difficulty} problems...")
            probs = _agent_instance.get_problem_list(current_difficulty)
            with _skip_problems_lock:
                filtered = [p for p in probs if p["titleSlug"] not in _skip_problems]
            skipped = len(probs) - len(filtered)
            if skipped:
                dashboard_log(f"Skipped {skipped} previously failed problems")
            dashboard_log(f"Got {len(filtered)} unsolved {current_difficulty} problems")
            return filtered

        try:
            problems = fetch_problems()
        except Exception as e:
            dashboard_log(f"Failed: {e}")
            with agent_state_lock:
                agent_state["status"] = "error"
            return

        idx = 0
        while _agent_instance._running and not _stop_flag.is_set():
            if idx >= len(problems):
                dashboard_log(f"No more {current_difficulty}. Re-fetching...")
                try:
                    problems = fetch_problems()
                    idx = 0
                    if not problems:
                        dashboard_log("No unsolved. Waiting 60s...")
                        if _stop_flag.wait(60):
                            break
                        continue
                except Exception as e:
                    dashboard_log(f"Fetch failed: {e}")
                    break

            problem = problems[idx]
            idx += 1
            stats["total"] += 1
            with agent_state_lock:
                agent_state["stats"]["total"] = stats["total"]
                agent_state["current_problem"] = problem["title"]
                agent_state["difficulty"] = current_difficulty

            try:
                result = _agent_instance.solve_problem(problem)
                if result == "failed":
                    with _skip_problems_lock:
                        _skip_problems.add(problem["titleSlug"])
                        _save_skip_problems()
                stats[result] += 1
            except Exception as e:
                dashboard_log(f"Error: {e}")
                with _skip_problems_lock:
                    _skip_problems.add(problem["titleSlug"])
                    _save_skip_problems()
                stats["failed"] += 1

            with agent_state_lock:
                agent_state["stats"]["accepted"] = stats["accepted"]
                agent_state["stats"]["failed"] = stats["failed"]
                agent_state["current_problem"] = ""

            dashboard_log(f"Stats: {stats['accepted']} accepted / {stats['failed']} failed / {stats['total']} total")

            if _agent_instance._next_diff and _agent_instance._next_diff != current_difficulty:
                current_difficulty = _agent_instance._next_diff
                _agent_instance._current_diff = current_difficulty
                _agent_instance._next_diff = None
                dashboard_log(f"Switching to {current_difficulty}")
                with agent_state_lock:
                    agent_state["difficulty"] = current_difficulty
                try:
                    problems = fetch_problems()
                    idx = 0
                    continue
                except Exception as e:
                    dashboard_log(f"Fetch after toggle failed: {e}")
                    break

            if _stop_flag.wait(5):
                break

        dashboard_log(f"Done. Accepted: {stats['accepted']}/{stats['total']}")
    except Exception as e:
        dashboard_log(f"Agent error: {e}")
    finally:
        with agent_state_lock:
            agent_state["status"] = "stopped"


if __name__ == "__main__":
    set_log_hook(dashboard_log)
    try:
        pw = sync_playwright().start()
        _playwright_instance = pw
        agent = LeetCodeAgent(pw)
        try:
            agent.connect()
            _playwright_browser = agent.browser
            _startup_cookies = agent.context.cookies()
            username = agent.check_signed_in()
            if username:
                profile = agent.get_user_profile(username)
                with agent_state_lock:
                    agent_state["profile"] = profile
                dashboard_log(f"Profile loaded: {profile.get('username')}")
        except Exception as e:
            dashboard_log(f"Profile fetch: {e}")

        _agent_instance = _make_requests_agent()
        _load_skip_problems()
        if _skip_problems:
            dashboard_log(f"Loaded {len(_skip_problems)} skipped problems from previous session")
    except Exception as e:
        dashboard_log(f"Init: {e}")

    app.run(debug=False, port=5000)
