"""
LeetCode Auto-Solver Agent — Playwright CDP version
Connects to your existing browser (Brave/Chrome) where LeetCode is signed in.
No hardcoded cookies needed.
"""

import os
import re
import json
import time
import sys
import subprocess
import threading
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright

PROBLEM_DELAY = 5
PAGE_SIZE     = 50
MAX_RETRIES    = 4
DIFFICULTY    = "Hard"
LANG          = "java"
LANG_SLUG     = "java"
OPENROUTER_KEY = os.environ.get("OPENAI_API_KEY")
MODEL         = "openai/gpt-oss-120b:free"

SOLVE_PROMPT = """You are an expert competitive programmer. Solve this LeetCode problem.

PROBLEM:
{problem}

Rules:
- Write clean, correct Java
- Handle all edge cases
- Complete solution only, no test code, no print statements, no main method
- Use the exact Java template signature provided. Your solution MUST match the class and method names.

Respond ONLY with valid JSON, no markdown, no backticks:
{{
  "approach": "one sentence explanation",
  "code": "complete java solution here",
  "time": "O(...)",
  "space": "O(...)"
}}"""

RETRY_PROMPT = """Your previous solution failed. Debug and fix it.

PROBLEM:
{problem}

YOUR CODE:
{code}

VERDICT: {status}

{error_details}

Rules:
- Fix the actual bug shown above
- Write clean, correct Java
- Use the exact Java template signature provided
- Respond ONLY with valid JSON, no markdown, no backticks:
{{
  "approach": "what you changed",
  "code": "fixed java solution",
  "time": "O(...)",
  "space": "O(...)"
}}"""


_log_hook = None

def set_log_hook(fn):
    global _log_hook
    _log_hook = fn

def log(msg, tag=""):
    if _log_hook:
        _log_hook(msg, tag)
    else:
        print(f"{tag} {msg}" if tag else msg, flush=True)


def extract_json_block(text):
    if not text:
        return {}
    for pattern in [
        lambda t: json.loads(t.strip()),
        lambda t: json.loads(re.search(r"```json\s*(.*?)\s*```", t, re.DOTALL | re.IGNORECASE).group(1).strip()),
        lambda t: json.loads(re.search(r"```\s*(.*?)\s*```", t, re.DOTALL).group(1).strip()),
        lambda t: json.loads(t[t.find("{"):t.rfind("}")+1].strip()),
        lambda t: json.loads(t.replace("```json", "").replace("```", "").strip()),
    ]:
        try:
            return pattern(text)
        except Exception:
            continue
    log(f"Could not parse LLM response: {text[:200]}")
    return {}


def find_browser():
    candidates = [
        "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
        "C:\\Program Files (x86)\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
        os.path.expanduser("~\\AppData\\Local\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


class LeetCodeAgent:
    def __init__(self, playwright):
        self.api = None
        self.browser = None
        self.context = None
        self._pw = playwright
        self.last_submit_time = 0
        self.csrf = ""
        self.history = []

    def _refresh_csrf(self):
        cookies = self.context.cookies("https://leetcode.com")
        self.csrf = next((c["value"] for c in cookies if c["name"] == "csrftoken"), "")
        if not self.csrf:
            log("Warning: csrftoken cookie not found")

    def connect(self):
        cdp_port = 9222
        browser_path = find_browser()

        try:
            self.browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            log("Connected to existing browser at port 9222")
        except Exception:
            if not browser_path:
                log("Browser not found. Start it manually with --remote-debugging-port=9222")
                sys.exit(1)
            log("Starting browser with remote debugging on port 9222...")
            subprocess.Popen([browser_path, f"--remote-debugging-port={cdp_port}"])
            time.sleep(3)
            for _ in range(10):
                try:
                    self.browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
                    break
                except Exception:
                    time.sleep(2)
            else:
                log("Could not connect to browser")
                sys.exit(1)

        self.context = self.browser.contexts[0]
        self.api = self.context.request
        self._refresh_csrf()
        log(f"Connected - pages open: {len(self.context.pages)}")

    def check_signed_in(self):
        data = self._graphql("query { userStatus { userId username isSignedIn } }")
        us = data["data"]["userStatus"]
        if not us.get("isSignedIn"):
            log("Not signed in to LeetCode. Log in and try again.")
            sys.exit(1)
        log(f"Signed in as {us.get('username', us['userId'])}")
        return us.get("username")

    def get_user_profile(self, username):
        query = """
        query userProfile($username: String!) {
          matchedUser(username: $username) {
            username
            profile { realName userAvatar ranking reputation }
            submitStats {
              acSubmissionNum { difficulty count }
              totalSubmissionNum { difficulty count }
            }
          }
        }
        """
        data = self._graphql(query, {"username": username})
        return data["data"]["matchedUser"]

    def _graphql(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}})
        resp = self.api.post(
            "https://leetcode.com/graphql",
            data=body,
            headers={"Content-Type": "application/json", "Referer": "https://leetcode.com"},
        )
        data = resp.json()
        if "errors" in data:
            raise Exception(f"GraphQL error: {data['errors']}")
        return data

    def get_problem_list(self, difficulty):
        query = """
        query problemsetQuestionListV2($filters: QuestionFilterInput, $limit: Int, $skip: Int) {
          problemsetQuestionListV2(filters: $filters, limit: $limit, skip: $skip) {
            questions { titleSlug title difficulty status }
            totalLength
          }
        }
        """
        filters = {
            "filterCombineType": "ALL",
            "difficultyFilter": {"difficulties": [difficulty.upper()], "operator": "IS"},
        }
        data = self._graphql(query, {"skip": 0, "limit": 1, "filters": filters})
        total = data["data"]["problemsetQuestionListV2"]["totalLength"]

        all_questions = []
        for skip in range(0, total, PAGE_SIZE):
            data = self._graphql(query, {"skip": skip, "limit": PAGE_SIZE, "filters": filters})
            all_questions.extend(data["data"]["problemsetQuestionListV2"]["questions"])

        return [q for q in all_questions if q.get("status") != "SOLVED"]

    def get_slug_by_number(self, number):
        resp = self.api.get("https://leetcode.com/api/problems/all/")
        data = resp.json()
        if isinstance(data, dict) and "stat_status_pairs" in data:
            for q in data["stat_status_pairs"]:
                stat = q.get("stat", {})
                if stat.get("frontend_question_id") == int(number):
                    slug = stat.get("question__title_slug")
                    level = q.get("difficulty", {}).get("level", 1)
                    difficulties = {1: "Easy", 2: "Medium", 3: "Hard"}
                    status_map = {"ac": "SOLVED", "notac": "ATTEMPTED"}
                    return {
                        "titleSlug": slug,
                        "title": stat.get("question__title"),
                        "difficulty": difficulties.get(level, "Hard"),
                        "questionFrontendId": str(number),
                        "status": status_map.get(q.get("status")),
                    }
        return None

    def get_problem_detail(self, slug):
        query = """
        query questionData($titleSlug: String!) {
          question(titleSlug: $titleSlug) {
            questionId title content difficulty
            codeSnippets { lang langSlug code }
            exampleTestcases
          }
        }
        """
        data = self._graphql(query, {"titleSlug": slug})
        return data["data"]["question"]

    def submit_solution(self, slug, code, question_id):
        now = time.time()
        elapsed = now - self.last_submit_time
        if elapsed < 10:
            log(f"  Rate-limit: sleeping {10 - elapsed:.1f}s")
            time.sleep(10 - elapsed)

        body = json.dumps({"lang": LANG_SLUG, "question_id": question_id, "typed_code": code})
        self.last_submit_time = time.time()

        resp = self.api.post(
            f"https://leetcode.com/problems/{slug}/submit/",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-csrftoken": self.csrf,
                "Referer": f"https://leetcode.com/problems/{slug}/",
            },
        )

        if resp.status == 403:
            raise ValueError("Submit failed: 403 Forbidden")

        try:
            res_data = resp.json()
        except Exception:
            raise ValueError(f"Submit failed (HTTP {resp.status}): {resp.text()[:200]}")

        if "submission_id" not in res_data:
            raise ValueError(f"Submit failed: {res_data.get('error', res_data)}")
        return res_data["submission_id"]

    def check_result(self, submission_id):
        for _ in range(20):
            time.sleep(2)
            resp = self.api.get(
                f"https://leetcode.com/submissions/detail/{submission_id}/check/",
                headers={"Referer": "https://leetcode.com"},
            )
            data = resp.json()
            state = data.get("state", "")
            if state == "SUCCESS":
                status = data.get("status_msg", "")
                parts = []
                if data.get("full_compile_error"):
                    parts.append("COMPILE ERROR:\n" + data["full_compile_error"])
                if data.get("runtime_error"):
                    parts.append("RUNTIME ERROR:\n" + data["runtime_error"])
                if data.get("last_testcase"):
                    parts.append("LAST TESTCASE:\n" + data["last_testcase"])
                co = data.get("code_output")
                eo = data.get("expected_output")
                if co and eo:
                    parts.append(f"YOUR OUTPUT: {str(co)[:200]}")
                    parts.append(f"EXPECTED: {str(eo)[:200]}")
                tc = data.get("total_testcases")
                if tc:
                    passed = data.get("passed_test_case_count", 0)
                    if passed != tc:
                        parts.append(f"PASSED: {passed}/{tc} test cases")
                return status, "\n".join(parts)
            if state not in ("PENDING", "STARTED"):
                return state, ""
        return "TIMEOUT", ""

    def call_llm(self, prompt, retries=5):
        payload = {"model": MODEL, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}
        for attempt in range(retries):
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://leetcode-agent.local",
                },
                json=payload,
                timeout=60,
            )
            if resp.status_code == 429:
                try:
                    retry_after = resp.json()["error"]["metadata"].get("retry_after_seconds", 30)
                except Exception:
                    retry_after = 30
                log(f"  Rate-limited. Waiting {retry_after}s... (attempt {attempt+1}/{retries})")
                time.sleep(retry_after)
                continue
            if resp.status_code != 200:
                raise ValueError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            if "error" in data:
                raise ValueError(f"OpenRouter error: {data['error']}")
            if "choices" not in data or not data["choices"]:
                raise ValueError(f"OpenRouter missing choices: {data}")
            return extract_json_block(data["choices"][0]["message"]["content"])
        raise ValueError(f"OpenRouter rate-limited after {retries} retries")

    def solve_problem(self, problem):
        slug = problem["titleSlug"]
        title = problem["title"]
        log("")
        log(f"  {title} ({problem['difficulty']})")
        log(f"{'='*50}")

        try:
            detail = self.get_problem_detail(slug)
        except Exception as e:
            log(f"  Error: {e}")
            return "failed"

        content = re.sub(r"<[^>]+>", " ", detail.get("content") or "")
        content = re.sub(r"\s+", " ", content).strip()
        question_id = detail["questionId"]

        lang_template = ""
        for snip in detail.get("codeSnippets") or []:
            if snip.get("langSlug") == LANG_SLUG:
                lang_template = snip.get("code", "")
                break

        problem_text = f"{title}\n\n{content}"
        if lang_template:
            problem_text += f"\n\nTemplate:\n```{LANG}\n{lang_template}\n```"

        code = ""
        status = ""
        error_detail = ""
        self._last_solution = {
            "title": title, "slug": slug, "difficulty": problem.get("difficulty"),
            "code": "", "approach": "", "time": "", "space": "",
            "verdict": "", "attempt": 0, "accepted": False, "ts": time.time(),
        }
        for attempt in range(1, MAX_RETRIES + 1):
            log(f"  Attempt {attempt}/{MAX_RETRIES}")
            try:
                if attempt == 1:
                    result = self.call_llm(SOLVE_PROMPT.format(problem=problem_text))
                else:
                    result = self.call_llm(RETRY_PROMPT.format(problem=problem_text, code=code, status=status, error_details=error_detail))
            except Exception as e:
                log(f"  LLM error: {e}")
                status = f"LLM_ERROR: {e}"
                error_detail = ""
                continue

            if not result or "code" not in result:
                log(f"  LLM returned invalid response")
                status = "LLM_PARSE_ERROR"
                error_detail = f"Raw response: {str(result)[:200]}"
                continue

            code = result["code"]
            log(f"  Approach: {result.get('approach', '?')}")
            log(f"  Complexity: {result.get('time')} / {result.get('space')}")

            log("  Submitting...")
            try:
                sub_id = self.submit_solution(slug, code, question_id)
                status, error_detail = self.check_result(sub_id)
            except Exception as e:
                log(f"  Submit error: {e}")
                if "403" in str(e):
                    return "failed"
                status = f"SUBMIT_ERROR: {e}"
                error_detail = ""
                continue

            ok = status == "Accepted"
            log(f"  Verdict: {status}")
            self._last_solution = {
                "title": title, "slug": slug, "difficulty": problem.get("difficulty"),
                "code": code, "approach": result.get("approach", ""),
                "time": result.get("time", ""), "space": result.get("space", ""),
                "verdict": status, "attempt": attempt, "accepted": ok,
                "ts": time.time(),
            }

            self.history.append(dict(self._last_solution))

            if ok:
                return "accepted"

            log(f"  Debug: {error_detail[:300] if error_detail else 'none'}")

        self._last_solution["verdict"] = status
        return "failed"

    def _keyboard_listener(self):
        try:
            import msvcrt
            while self._running:
                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key == b'\t':
                        cycle = {"Easy": "Medium", "Medium": "Hard", "Hard": "Easy"}
                        self._next_diff = cycle[self._current_diff]
                        log(f"  [Tab] Will switch to {self._next_diff} after this problem")
                    elif key == b'\x1b':
                        log("  [Esc] Stopping...")
                        self._running = False
                        break
                time.sleep(0.2)
        except ImportError:
            pass

    def _check_difficulty_toggle(self, current):
        toggle_file = Path(__file__).parent / "difficulty.txt"
        if toggle_file.exists():
            val = toggle_file.read_text().strip()
            if val in ("Easy", "Medium", "Hard") and val != current:
                toggle_file.unlink()
                return val
        if self._next_diff and self._next_diff != current:
            diff = self._next_diff
            self._next_diff = None
            return diff
        return None

    def run(self):
        log("LeetCode Auto-Solver starting...")
        if not OPENROUTER_KEY:
            log("OPENAI_API_KEY not set")
            return

        self.connect()
        self.check_signed_in()

        self._current_diff = DIFFICULTY
        self._next_diff = None
        self._running = True
        current_difficulty = self._current_diff
        log(f"Difficulty: {current_difficulty} | Max retries: {MAX_RETRIES}")
        log("Press TAB to cycle difficulty (Easy/Medium/Hard) | Esc to stop")
        t = threading.Thread(target=self._keyboard_listener, daemon=True)
        t.start()

        stats = {"accepted": 0, "failed": 0, "total": 0}

        def fetch_problems():
            log(f"Fetching {current_difficulty} problem list...")
            probs = self.get_problem_list(current_difficulty)
            log(f"Got {len(probs)} unsolved {current_difficulty} problems")
            return probs

        try:
            problems = fetch_problems()
        except Exception as e:
            log(f"Failed: {e}")
            return

        idx = 0
        while self._running:
            if idx >= len(problems):
                log(f"No more {current_difficulty} problems. Re-fetching...")
                try:
                    problems = fetch_problems()
                    idx = 0
                    if not problems:
                        log("No unsolved problems found. Waiting 60s...")
                        time.sleep(60)
                        continue
                except Exception as e:
                    log(f"Fetch failed: {e}")
                    break

            problem = problems[idx]
            idx += 1
            stats["total"] += 1
            try:
                result = self.solve_problem(problem)
                stats[result] += 1
            except KeyboardInterrupt:
                log("Stopped")
                break
            except Exception as e:
                log(f"Error: {e}")
                stats["failed"] += 1

            log(f"Accepted: {stats['accepted']} | Failed: {stats['failed']} | Total: {stats['total']}")

            toggled = self._check_difficulty_toggle(current_difficulty)
            if toggled:
                log(f"Difficulty changed to {toggled}")
                current_difficulty = toggled
                self._current_diff = toggled
                try:
                    problems = fetch_problems()
                    idx = 0
                    continue
                except Exception as e:
                    log(f"Fetch failed after toggle: {e}")
                    break

            time.sleep(PROBLEM_DELAY)

        log(f"Done. Accepted: {stats['accepted']}/{stats['total']}")


def main():
    with sync_playwright() as p:
        LeetCodeAgent(p).run()


if __name__ == "__main__":
    main()
