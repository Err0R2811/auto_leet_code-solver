# LeetCode Auto-Solver

An automated agent that solves LeetCode problems end-to-end: it fetches unsolved problems, generates Java solutions with an LLM, submits them through your logged-in browser session, and retries with feedback until they're accepted — all controlled from a web dashboard.

## How it works

1. **Browser session reuse** — Connects to your existing Brave/Chrome browser over Chrome DevTools Protocol (port 9222), so it uses your real LeetCode login. No cookies or credentials are stored.
2. **Problem discovery** — Queries LeetCode's GraphQL API for unsolved problems at the selected difficulty (Easy / Medium / Hard).
3. **LLM solving** — Sends the problem statement and the Java code template to an LLM via OpenRouter. The model returns JSON with an approach, code, and complexity.
4. **Submit & judge** — Submits through LeetCode's real submit endpoint and polls for the verdict.
5. **Retry loop** — On failure, the compiler/runtime errors and failing test cases are fed back to the model (up to 4 attempts per problem).
6. **Skip list** — Problems that fail all retries are saved to `skip_problems.json` so they aren't retried in future runs.

## Features

- Web dashboard (`dashboard.py`) with live logs (SSE), stats, profile info, and start/stop controls
- Manual solve mode — paste a problem slug, URL, or number to solve a specific problem (`/manual`)
- Submission history page (`/history`) with code, approach, verdicts, and complexity
- Difficulty switching at runtime (dashboard button, `Tab` key in CLI, or a `difficulty.txt` file)
- Rate-limit handling for both LeetCode submissions and the OpenRouter API

## Requirements

- Windows (uses `msvcrt` for keyboard shortcuts in CLI mode; the dashboard works cross-platform)
- Python 3.9+
- Brave or Chrome browser, logged in to LeetCode
- An [OpenRouter](https://openrouter.ai) API key

## Setup

```bash
pip install playwright>=1.40.0 flask requests
playwright install chromium
```

Set your OpenRouter key:

```powershell
$env:OPENAI_API_KEY = "sk-or-..."   # used as the OpenRouter key
```

## Usage

### Web dashboard (recommended)

```bash
python dashboard.py
```

Then open http://localhost:5000

- **Start** begins the solve loop; **Stop** halts it after the current problem.
- Switch difficulty at any time — the change applies after the current problem.
- **Manual** tab lets you queue specific problems by number, slug, or URL.
- **History** shows every attempt with the generated solution.

### CLI mode

```bash
python agent.py
```

Controls while running:
- `Tab` — cycle difficulty (Easy → Medium → Hard)
- `Esc` — stop

## Configuration

Constants at the top of `agent.py`:

| Constant | Default | Purpose |
|---|---|---|
| `DIFFICULTY` | `Hard` | Starting difficulty |
| `LANG` / `LANG_SLUG` | `java` | Submission language |
| `MODEL` | `openai/gpt-oss-120b:free` | OpenRouter model |
| `MAX_RETRIES` | `4` | Attempts per problem |
| `PROBLEM_DELAY` | `5` | Seconds between problems |

## Project layout

```
agent.py            Core solver agent (GraphQL, submission, LLM retry loop)
dashboard.py        Flask dashboard: state, logs SSE, manual queue, skip list
templates/          dashboard.html, history.html, manual.html
skip_problems.json  Persisted list of failed problems to skip
requirements.txt
```

## Notes

- The browser must expose CDP on port 9222. The agent launches it with `--remote-debugging-port=9222` if it can't connect, or start it yourself.
- Submissions are real — they count toward your LeetCode account stats. Use responsibly.
- `requirements.txt` currently only lists `playwright`; you also need `flask` and `requests` for the dashboard.
