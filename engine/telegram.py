"""nloop Telegram bot — notifications, loop control, and agent chat (port of
telegram_bot.py + agent_run.sh from dtc-agent, rewritten async so it lives inside
the nloop event loop).

Capabilities:
- Run-finished notifications (succeeded/failed/stopped) to every chat in the allow-list.
- Control: /loops, /status, /new, /stop, /reset — gated by the allow-list (fails closed).
- Freeform chat → a REAL per-chat Claude Code session (--resume, fresh retry when
  the session is stale), model tiering (short greeting → cheap, substantive → big
  + thinking budget), photos/documents downloaded to incoming/ for the agent to Read.
- Secret redaction on everything going out (defense-in-depth: a token that leaks
  into an echo must never reach Telegram in plaintext).

Secrets come from env / .env: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS
(comma-separated). DON'T put them in config.yaml (that gets committed).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
import uuid

import httpx

from engine import claude_cli, config, grounding

log = logging.getLogger("nloop.telegram")

POLL_TIMEOUT = 50      # getUpdates long-poll seconds
TG_MAX = 3900          # chunk cap below Telegram's hard 4096 limit
MAX_AUTO_CONTINUES = 2  # error_max_turns/timeout -> resume the same session N times
TERMINAL = ("succeeded", "failed", "stopped")
SESSIONS_DIR = ".sessions"
INCOMING_DIR = "incoming"

STATUS_EMOJI = {"succeeded": "✅", "failed": "❌", "stopped": "⏹",
                "running": "🔄", "queued": "⏳"}

# ---- secret redaction (1:1 port from dtc telegram_bot.py) --------------------

_SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS_ACCESS_KEY_ID"),
    (re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])(?=.{0,40}(aws|secret))", re.IGNORECASE), "AWS_SECRET_KEY"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "GITHUB_TOKEN"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "SLACK_TOKEN"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "GOOGLE_API_KEY"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "API_SECRET_KEY"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "JWT"),
    (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{20,}=*"), "BEARER_TOKEN"),
    (re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]+?-----END[ A-Z]*PRIVATE KEY-----"), "PRIVATE_KEY_BLOCK"),
    # Labeled heuristic: an identifier that CONTAINS key/token/secret/password followed
    # by a long opaque value — catches provider tokens whose naming pattern we don't know.
    (re.compile(
        r"(?i)\b([A-Za-z][A-Za-z0-9_-]*(?:key|token|secret|password|passwd|credential)[A-Za-z0-9_-]*\s*[:=]\s*)"
        r"[\"']?([A-Za-z0-9_\-/+.]{16,})[\"']?"
    ), "LABELED_CREDENTIAL"),
]


def redact_secrets(text: str) -> str:
    """Mask credential-shaped substrings in outgoing text. Better to over-redact a
    short opaque string than to let a real secret slip through."""
    def repl(m, label):
        if m.re.groups >= 2:
            return f"{m.group(1)}[REDACTED:{label}]"
        return f"[REDACTED:{label}]"

    for pattern, label in _SECRET_PATTERNS:
        text = pattern.sub(lambda m, label=label: repl(m, label), text)
    return text


# ---- markdown → Telegram HTML (ported from dtc, trimmed) ---------------------

_CODE_BLOCK_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_TABLE_BLOCK_RE = re.compile(r"(?:^[ \t]*\|.*\|[ \t]*$\n?){2,}", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\|?[ \t]*:?-{2,}:?[ \t]*(\|[ \t]*:?-{2,}:?[ \t]*)*\|?$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s*(.+)$")
_BULLET_RE = re.compile(r"(?m)^[ \t]*[-*]\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def _format_table(block_text: str) -> str | None:
    """Markdown table → aligned monospace grid (Telegram has no <table>)."""
    lines = [l.strip() for l in block_text.strip("\n").split("\n") if l.strip()]
    rows = []
    for i, line in enumerate(lines):
        if i == 1 and _TABLE_SEP_RE.match(line.replace(" ", "")):
            continue
        rows.append([c.strip() for c in line.strip("|").split("|")])
    if not rows:
        return None
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = [max(len(r[c]) for r in rows) for c in range(ncols)]
    out = []
    for ri, r in enumerate(rows):
        out.append("  ".join(cell.ljust(widths[ci]) for ci, cell in enumerate(r)).rstrip())
        if ri == 0:
            out.append("  ".join("-" * widths[ci] for ci in range(ncols)))
    return "\n".join(out)


def md_to_tg_html(text: str) -> str:
    """Best-effort markdown → Telegram HTML. Code spans are stashed first so that
    `*`/`_` inside code (e.g. **kwargs) doesn't get read as emphasis."""
    text = html.escape(text, quote=False)

    blocks: list[str] = []

    def stash_block(m):
        blocks.append(f"<pre>{m.group(1).strip(chr(10))}</pre>")
        return f"\x00B{len(blocks) - 1}\x00"
    text = _CODE_BLOCK_RE.sub(stash_block, text)

    def stash_table(m):
        formatted = _format_table(m.group(0))
        if formatted is None:
            return m.group(0)
        blocks.append(f"<pre>{formatted}</pre>")
        return f"\x00B{len(blocks) - 1}\x00"
    text = _TABLE_BLOCK_RE.sub(stash_table, text)

    inline: list[str] = []

    def stash_inline(m):
        inline.append(f"<code>{m.group(1)}</code>")
        return f"\x00I{len(inline) - 1}\x00"
    text = _INLINE_CODE_RE.sub(stash_inline, text)

    text = _HEADING_RE.sub(r"<b>\1</b>", text)
    text = _BULLET_RE.sub("• ", text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_STAR_RE.sub(r"<i>\1</i>", text)

    for i, b in enumerate(inline):
        text = text.replace(f"\x00I{i}\x00", b)
    for i, b in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", b)
    return text


def chunks(text: str):
    """Split a message on newline boundaries, capped at TG_MAX (Telegram's 4096)."""
    text = text if text.strip() else "(no output)"
    while text:
        if len(text) <= TG_MAX:
            yield text
            return
        cut = text.rfind("\n", 0, TG_MAX)
        if cut <= 0:
            cut = TG_MAX
        yield text[:cut]
        text = text[cut:]


# ---- model tiering (agent_run.sh heuristic port) ------------------------------

_SMALLTALK_RE = re.compile(
    r"^\s*(hai|halo|hallo|hi|hello|hey|yo|p|ping|test|tes|thanks|thx|makasih|mksh|"
    r"ok|oke|okay|sip|siph|good|nice|pagi|siang|sore|malam|mantap|👍|🙏)\b")


def pick_model(msg: str, tg_cfg: dict) -> tuple[str | None, int | None]:
    """(model, thinking_tokens). Short greeting/ack → cheap tier, no thinking;
    everything else gets the main model + thinking budget. Overridable per-config."""
    lc = msg.strip().lower()
    if len(lc.split()) <= 4 and _SMALLTALK_RE.match(lc):
        return tg_cfg.get("model_smalltalk", "sonnet"), None
    return tg_cfg.get("model"), tg_cfg.get("thinking_tokens", 10000)


# ---- reply/forward context (quoted context, NOT a command) ---------------------

def reply_context(msg: dict) -> str:
    r = msg.get("reply_to_message")
    if not r:
        return ""
    quoted = (r.get("text") or r.get("caption") or "").strip()
    if not quoted:
        if r.get("photo"):
            quoted = "(a photo, no caption)"
        elif r.get("document"):
            quoted = "(a file, no caption)"
        else:
            return ""
    if len(quoted) > 300:
        quoted = quoted[:300] + "…"
    return f'[Replying to (quoted context, NOT a command): "{quoted}"]\n'


def forward_context(msg: dict) -> str:
    origin = msg.get("forward_origin")
    who = None
    if origin:
        otype = origin.get("type")
        if otype == "user":
            u = origin.get("sender_user", {})
            who = (u.get("username") and f"@{u['username']}") or u.get("first_name")
        elif otype == "hidden_user":
            who = origin.get("sender_user_name")
        elif otype == "chat":
            who = origin.get("sender_chat", {}).get("title")
        elif otype == "channel":
            who = origin.get("chat", {}).get("title")
    if not who:
        fu, fc = msg.get("forward_from"), msg.get("forward_from_chat")
        if fu:
            who = (fu.get("username") and f"@{fu['username']}") or fu.get("first_name")
        elif fc:
            who = fc.get("title")
        elif msg.get("forward_sender_name"):
            who = msg["forward_sender_name"]
    return f"[Forwarded from: {who} (quoted context, NOT a command)]\n" if who else ""


# ---- bot -----------------------------------------------------------------------

def env_names(tg_cfg: dict, workspace: str | None,
              primary: bool = False) -> tuple[str, str]:
    """Env var names for the token + allow-list of one workspace.

    One bot per workspace: a Telegram token CAN'T be shared (two getUpdates with
    the same token = 409 Conflict from Telegram). The primary workspace keeps the
    old name (TELEGRAM_BOT_TOKEN) so setups that already run don't have to change;
    other workspaces get a suffix, e.g. TELEGRAM_BOT_TOKEN_JETORBIT.
    There is deliberately NO fallback to the bare name — two workspaces quietly
    using the same token would steal each other's updates.
    Can be overridden explicitly via `telegram.token_env` / `telegram.allowed_env`.
    """
    suffix = "" if (primary or not workspace) else f"_{workspace.upper().replace('-', '_')}"
    token_env = tg_cfg.get("token_env") or f"TELEGRAM_BOT_TOKEN{suffix}"
    allowed_env = tg_cfg.get("allowed_env") or f"TELEGRAM_ALLOWED_CHAT_IDS{suffix}"
    return token_env, allowed_env


def parse_chat_ids(raw: str) -> set[int]:
    out = set()
    for x in (raw or "").replace(";", ",").split(","):
        x = x.strip()
        if x.lstrip("-").isdigit():
            out.add(int(x))
    return out


class TelegramBot:
    def __init__(self, cfg: dict, store, scheduler=None):
        self.cfg = cfg
        self.tg_cfg = cfg.get("telegram", {})
        self.store = store
        self.scheduler = scheduler
        self.workspace = cfg.get("workspace")
        self.token_env, self.allowed_env = env_names(
            self.tg_cfg, self.workspace, primary=bool(cfg.get("is_primary")))
        self.token = os.environ.get(self.token_env, "").strip()
        self.api = f"https://api.telegram.org/bot{self.token}"
        self.allowed = parse_chat_ids(os.environ.get(self.allowed_env, ""))
        self.offset: int | None = None
        self.busy = asyncio.Lock()          # one heavy agent-chat at a time
        self.http = httpx.AsyncClient(timeout=POLL_TIMEOUT + 15)
        self._stopping = asyncio.Event()

    def authorized(self, chat_id: int) -> bool:
        return chat_id in self.allowed      # fails closed

    # ---- Telegram I/O ----

    async def send(self, chat_id: int, text: str, parse_mode: str | None = "HTML") -> None:
        for chunk in chunks(text):
            await self._send_one(chat_id, chunk, parse_mode)

    async def _send_one(self, chat_id: int, text: str, parse_mode: str | None) -> None:
        payload: dict = {"chat_id": chat_id, "text": text,
                         "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            r = await self.http.post(f"{self.api}/sendMessage", json=payload)
            if not r.json().get("ok"):
                raise ValueError(r.json().get("description", "telegram error"))
        except (httpx.HTTPError, ValueError) as e:
            if parse_mode:
                # bad markup: strip tags, send plain — the message must not get lost
                log.warning("send HTML failed (%s), retry plain", e)
                await self._send_one(chat_id, html.unescape(_TAG_RE.sub("", text)), None)
            else:
                log.error("send failed: %s", e)

    async def send_reply(self, chat_id: int, raw_markdown: str) -> None:
        await self.send(chat_id, md_to_tg_html(redact_secrets(raw_markdown)))

    async def send_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self.http.post(f"{self.api}/sendChatAction",
                                 json={"chat_id": chat_id, "action": action})
        except httpx.HTTPError:
            pass

    async def notify(self, text: str) -> None:
        """Broadcast to every chat in the allow-list (used for run-finished notifs)."""
        for cid in self.allowed:
            await self.send(cid, text)

    async def notify_run_finished(self, run: dict, payload: dict) -> None:
        emoji = STATUS_EMOJI.get(run["status"], "❔")
        goal = run["goal"].splitlines()[0][:120]
        reason = payload.get("reason", "")
        await self.notify(
            f"{emoji} loop <b>{run['status']}</b> ({html.escape(reason)})\n"
            f"<code>{run['id']}</code> — {html.escape(goal)}\n"
            f"iterations {run['iterations_done']}/{run['max_iterations']}, "
            f"cost ${run['cost_total']:.2f}"
        )

    async def download_file(self, file_id: str, chat_id: int) -> str:
        r = await self.http.get(f"{self.api}/getFile", params={"file_id": file_id})
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        ext = os.path.splitext(file_path)[1] or ".jpg"
        os.makedirs(INCOMING_DIR, exist_ok=True)
        local = os.path.join(os.path.abspath(INCOMING_DIR),
                             f"{chat_id}_{int(time.time())}{ext}")
        resp = await self.http.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}")
        resp.raise_for_status()
        with open(local, "wb") as f:
            f.write(resp.content)
        return local

    # ---- chat agent (per-chat session, agent_run.sh pattern) ----

    def _sid_path(self, chat_id: int) -> str:
        """Sessions are split per workspace: the same chat id talking to two
        different bots must not mix context (different workdir & role)."""
        d = os.path.join(SESSIONS_DIR, self.workspace) if self.workspace else SESSIONS_DIR
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f"{chat_id}.sid")

    def reset_session(self, chat_id: int) -> None:
        try:
            os.remove(self._sid_path(chat_id))
        except OSError:
            pass

    async def run_agent(self, chat_id: int, prompt: str) -> None:
        if self.busy.locked():
            await self.send(chat_id, "⏳ Hang on, I'm still on the last one — won't be long.")
            return
        async with self.busy:
            stop_typing = asyncio.Event()

            async def typer():
                while not stop_typing.is_set():
                    await self.send_action(chat_id)
                    try:
                        await asyncio.wait_for(stop_typing.wait(), 4)
                    except TimeoutError:
                        pass
            typer_task = asyncio.create_task(typer())
            try:
                reply = await self._invoke(chat_id, prompt)
            finally:
                stop_typing.set()
                await typer_task
        await self.send_reply(chat_id, reply)

    def _progress_reporter(self, chat_id: int, interval: int):
        """on_event for claude_cli.run: send throttled progress updates to the chat.

        So the user knows the agent is still working (not hung) without spamming:
        at most one message per `interval` seconds, containing the latest action.
        Fast replies (< interval) get no update at all. interval <= 0 = off.
        """
        state = {"t0": time.monotonic(), "last": time.monotonic(), "tools": 0, "note": ""}

        async def on_event(type_: str, payload: dict) -> None:
            if interval <= 0:
                return
            if type_ == "tool":
                state["tools"] += 1
                name = payload.get("name") or "?"
                state["note"] = f"{name} {payload.get('input') or ''}".strip()[:160]
            elif type_ == "turn":
                text = (payload.get("text") or "").strip()
                if text:
                    state["note"] = text[:200]
            else:
                return
            now = time.monotonic()
            if now - state["last"] < interval:
                return
            state["last"] = now
            mins = int((now - state["t0"]) // 60)
            await self.send_reply(
                chat_id,
                f"⏳ still working ({mins}m, {state['tools']} tool calls) — {state['note']}")

        return on_event

    async def _invoke(self, chat_id: int, prompt: str, retried: bool = False,
                      continues: int = 0) -> str:
        tg = self.tg_cfg
        claude_cfg = self.cfg.get("claude", {})
        model, thinking = pick_model(prompt, tg)
        workdir = tg.get("agent_workdir", ".")

        sid_path = self._sid_path(chat_id)
        resume = None
        session_id = None
        if os.path.isfile(sid_path) and os.path.getsize(sid_path) > 0:
            with open(sid_path) as f:
                resume = f.read().strip()
        else:
            session_id = str(uuid.uuid4())
            with open(sid_path, "w") as f:
                f.write(session_id)

        system_prompt = await grounding.build_system_prompt(
            self.cfg, role=tg.get("role"), context_cmd=tg.get("context_cmd"),
            workdir=workdir)

        res = await claude_cli.run(
            prompt,
            cwd=workdir,
            resume=resume,
            session_id=session_id,
            model=model,
            # Telegram chat has its own turn budget: default None = no limit
            # (chat tasks tend to be big; the guardrail is cmd_timeout_sec + continue).
            max_turns=tg.get("max_turns"),
            allowed_tools=tg.get(
                "allowed_tools", "Bash,Read,Edit,Write,Glob,Grep,WebSearch,WebFetch,Task"),
            permission_mode=claude_cfg.get("permission_mode", "acceptEdits"),
            timeout_sec=tg.get("cmd_timeout_sec", 900),
            system_prompt=system_prompt,
            max_thinking_tokens=thinking,
            lock_file=claude_cfg.get("lock_file"),
            on_event=self._progress_reporter(
                chat_id, tg.get("progress_interval_sec", 60)),
        )
        if res.ok:
            return res.result_text.strip() or "(empty)"
        # Running out of max_turns / timeout ≠ a broken session: the work is already
        # half done. Continue the SAME session (the sid is still in the file), don't
        # reset — a reset throws the progress away and redoes it from zero, paying twice.
        if res.subtype in ("error_max_turns", "timeout"):
            if continues < MAX_AUTO_CONTINUES:
                log.warning("%s chat=%s, auto-continue %d/%d",
                            res.subtype, chat_id, continues + 1, MAX_AUTO_CONTINUES)
                await self.send_reply(
                    chat_id, f"⏳ Not done yet ({res.subtype}), I'll keep going...")
                return await self._invoke(
                    chat_id, "continue the previous task until it is finished",
                    retried=retried, continues=continues + 1)
            log.error("%s chat=%s after %d continues, giving up",
                      res.subtype, chat_id, continues)
            return ("❌ Task too big: still not finished even after being "
                    f"continued {continues}x ({res.subtype}). "
                    "Try splitting it into smaller tasks, or send 'continue' "
                    "to pick it up again.")
        # A stale session makes --resume fail: drop the sid, try once more fresh.
        if resume and not retried:
            log.warning("resume failed chat=%s (%s), retry fresh", chat_id, res.subtype)
            self.reset_session(chat_id)
            return await self._invoke(chat_id, prompt, retried=True)
        err = (res.stderr_tail or res.subtype or "unknown error").strip()[:800]
        log.error("agent invoke failed chat=%s: %s", chat_id, err)
        return f"❌ Agent failed: {err}"

    # ---- commands ----

    async def handle(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        caption = (msg.get("caption") or "").strip()

        # Photo/document: resolve to a local path → the agent can Read it. Don't
        # filter on mime here (non-image docs silently dropped = old dtc bug).
        photo, doc = msg.get("photo"), msg.get("document")
        if photo or doc:
            if not await self._require_auth(chat_id):
                return
            try:
                file_id = photo[-1]["file_id"] if photo else doc["file_id"]
                local = await self.download_file(file_id, chat_id)
            except Exception as e:  # noqa: BLE001
                await self.send(chat_id, f"❌ Failed to download the file: {e}")
                return
            kind = "image" if photo else "file"
            label = "Image" if photo else f"File ({(doc or {}).get('mime_type', 'unknown')})"
            prompt = (forward_context(msg) + reply_context(msg)
                      + (caption or f"What is this {kind}? Explain/read what's in it.")
                      + f"\n\n[{label} received via Telegram, saved at: {local}"
                        " — use the Read tool to view/read it]")
            asyncio.create_task(self.run_agent(chat_id, prompt))
            return

        if not text:
            return
        cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""

        if cmd in ("/start", "/help"):
            await self.send(chat_id, self.help_text(chat_id))
            return
        if cmd in ("/whoami", "/id"):
            ok = "✅ authorized" if self.authorized(chat_id) else "🚫 NOT in allow-list"
            await self.send(chat_id, f"Your chat ID: <code>{chat_id}</code>\n{ok}")
            return

        if not await self._require_auth(chat_id, text):
            return

        if cmd == "/loops":
            await self.send(chat_id, self.loops_text())
            return
        if cmd == "/status":
            await self.send(chat_id, self.status_text())
            return
        if cmd == "/stop":
            arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            await self._cmd_stop(chat_id, arg)
            return
        if cmd == "/new":
            arg = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
            await self._cmd_new(chat_id, arg)
            return
        if cmd == "/reset":
            self.reset_session(chat_id)
            await self.send(chat_id, "🧹 Okay, starting our chat from scratch again.")
            return

        # Default: freeform → agent (it's the one deciding what to do)
        asyncio.create_task(
            self.run_agent(chat_id, forward_context(msg) + reply_context(msg) + text))

    async def _require_auth(self, chat_id: int, text: str = "") -> bool:
        if self.authorized(chat_id):
            return True
        await self.send(
            chat_id,
            f"🚫 Not authorized. Your chat ID is <code>{chat_id}</code> — "
            f"add it to <code>{self.allowed_env}</code> in .env and restart nloop.")
        log.warning("denied chat_id=%s: %s", chat_id, text[:60])
        return False

    def _my_runs(self, limit: int | None = None) -> list[dict]:
        """The bot only sees its own workspace's runs — other tenants aren't its business."""
        return self.store.list_runs(workspace=self.workspace, limit=limit)

    async def _cmd_stop(self, chat_id: int, run_id: str) -> None:
        run = self.store.get_run(run_id) if run_id else None
        if run is None or (self.workspace and run.get("workspace") != self.workspace):
            await self.send(chat_id, "Usage: /stop <run_id> (see /loops)")
            return
        self.store.request_stop(run_id)
        await self.send(chat_id, f"⏹ stop requested for <code>{run_id}</code> "
                                 "(the loop checks the flag between iterations).")

    async def _cmd_new(self, chat_id: int, arg: str) -> None:
        parts = [p.strip() for p in arg.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            await self.send(chat_id,
                            "Usage: /new goal | verify_cmd [| workdir]\n"
                            "example: <code>/new fix the tests | npm test | /opt/app</code>")
            return
        goal, verify_cmd = parts[0], parts[1]
        workdir = parts[2] if len(parts) > 2 and parts[2] else None
        if workdir is None:
            workdir = os.path.join(config.scratch_dir(self.cfg), uuid.uuid4().hex[:8])
            os.makedirs(workdir, exist_ok=True)
        elif not os.path.isdir(workdir):
            await self.send(chat_id, f"❌ workdir does not exist: {workdir}")
            return
        loops_cfg = self.cfg["loops"]
        run_id = self.store.create_run(
            goal, verify_cmd, workdir,
            model=self.cfg["claude"].get("model"),
            max_iterations=loops_cfg["max_iterations"],
            max_cost_usd=loops_cfg["max_cost_usd"],
            workspace=self.workspace,
        )
        await self.send(chat_id, f"🚀 loop <code>{run_id}</code> queued.\n"
                                 f"goal: {html.escape(goal)}\nworkdir: <code>{workdir}</code>")

    def loops_text(self) -> str:
        runs = self._my_runs(limit=8)
        if not runs:
            return "no runs yet."
        lines = []
        for r in runs:
            emoji = STATUS_EMOJI.get(r["status"], "❔")
            goal = html.escape(r["goal"].splitlines()[0][:48])
            lines.append(f"{emoji} <code>{r['id']}</code> {r['status']}"
                         f" · it {r['iterations_done']}/{r['max_iterations']}"
                         f" · ${r['cost_total']:.2f}\n   {goal}")
        return "\n".join(lines)

    def status_text(self) -> str:
        runs = self._my_runs()
        counts: dict[str, int] = {}
        for r in runs:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        parts = [f"{s}: {n}" for s, n in sorted(counts.items())] or ["empty"]
        scheds = ", ".join((self.cfg.get("schedules") or {}).keys()) or "—"
        tasks_ = ", ".join((self.cfg.get("tasks") or {}).keys()) or "—"
        return (f"🗂 workspace: <b>{self.workspace or '—'}</b>\n"
                f"🧮 runs — {' · '.join(parts)}\n"
                f"🗓 schedules: {scheds}\n"
                f"⚡ tasks: {tasks_}\n"
                f"👥 allow-list: {len(self.allowed)} chats")

    def help_text(self, chat_id: int) -> str:
        auth = "✅" if self.authorized(chat_id) else "🚫 not allowed yet"
        return (
            f"<b>nloop</b> — loop engine, workspace <b>{self.workspace or '—'}</b>. "
            "Just chat to put the agent to work, "
            f"or use a command. ({auth})\n\n"
            "• /loops — list the latest runs\n"
            "• /new goal | verify_cmd [| workdir] — queue a new loop\n"
            "• /stop &lt;run_id&gt; — stop a loop\n"
            "• /status — engine summary\n"
            "• /reset — forget the chat context\n"
            "• /whoami — your chat ID"
        )

    # ---- main loop ----

    async def run_forever(self) -> None:
        log.info("telegram bot up [ws=%s, token=%s]; allow-list=%s",
                 self.workspace, self.token_env,
                 sorted(self.allowed) or "EMPTY (onboarding only)")
        while not self._stopping.is_set():
            try:
                params: dict = {"timeout": POLL_TIMEOUT}
                if self.offset is not None:
                    params["offset"] = self.offset
                r = await self.http.get(f"{self.api}/getUpdates", params=params)
                r.raise_for_status()
                for upd in r.json().get("result", []):
                    self.offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message")
                    if msg and "chat" in msg:
                        try:
                            await self.handle(msg)
                        except Exception:  # noqa: BLE001
                            log.exception("handle error")
            except httpx.HTTPError as e:
                log.warning("poll error: %s; backoff 5s", e)
                await self._sleep(5)
            except Exception:  # noqa: BLE001
                log.exception("loop error; backoff 5s")
                await self._sleep(5)

    async def stop(self) -> None:
        self._stopping.set()
        await self.http.aclose()

    async def _sleep(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), sec)
        except TimeoutError:
            pass
