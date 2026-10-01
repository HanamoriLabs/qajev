"""Live MCP check: a Jev goal and a smoke crawl through `qajev mcp`, against the local fixture.

    python tests/fixtures/serve.py &        # http://127.0.0.1:8765
    uv run python scripts/mcp_live.py [ENV_FILE]
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters, stdio_client

SITE = "http://127.0.0.1:8765"


def data(result):
    return result.structured_content or json.loads(result.content[0].text)


async def main():
    home = tempfile.mkdtemp(prefix="qajev-mcp-home-")
    env_file = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Development/jev-ultrafast/.env")
    env = {**os.environ, "QAJEV_HOME": home, "QAJEV_ENV_FILE": env_file}
    params = StdioServerParameters(command=sys.executable, args=["-m", "qajev", "mcp"], env=env)
    events = []

    async def on_progress(progress, total, message):
        events.append((progress, total, message))

    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("qa_check", {
                    "url": SITE + "/", "goal": "Find out how much the Pro plan costs. Stop when its monthly price "
                                             "is visible.",
                    "expect_text": ["$29 per month"], "expect_url": "/pricing.html", "profile": "mcptest",
                    "headless": True, "cost_cap": 0.05, "max_actions": 8,
                }, progress_callback=on_progress)
                d = data(res)
                sc = d["scenarios"][0]
                print("qa_check:", d["gate"], sc["outcome"], "|", sc["reason"], "| cost", d["cost"]["usd"],
                      "| decisions", d["cost"]["calls"]["typesafe"], "| tool error:", res.is_error)
                print("progress events:", events)
                print("report_md exists:", os.path.exists(d["report_md"]))

                d = data(await s.call_tool("qa_smoke", {"url": SITE + "/", "max_pages": 6, "profile": "mcptest",
                                                        "headless": True}))
                print("qa_smoke:", d["gate"], d["counts"], "findings", len(d["findings"]), "cost", d["cost"]["usd"])

                status = data(await s.call_tool("qa_browser", {"action": "status"}))["result"]
                print("browser status:", [(b["profile"], b["port"], b["headless"], b["alive"]) for b in status])
                print("browser stop:", data(await s.call_tool("qa_browser", {"action": "stop",
                                                                            "profile": "mcptest"}))["result"])
                print("status after stop:", data(await s.call_tool("qa_browser", {"action": "status"}))["result"])
    finally:
        shutil.rmtree(home, ignore_errors=True)


asyncio.run(main())
