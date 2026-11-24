import json

from tests.harness import SCRIPTED, agent, scripted, text, until


def call(tool: str, **args) -> str:
    return f"call {tool} {json.dumps(args)}"


def kinds(editor) -> list[str]:
    return [u.session_update for u in editor.updates]


@scripted
async def test_read_tool_call(env):
    (env.work / "note.txt").write_text("remember the milk\n")
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        reply = await conn.prompt(
            session_id=session.session_id, prompt=[text(call("read", path="note.txt"))]
        )
        assert reply.stop_reason == "end_turn"
        (started,) = editor.of("tool_call")
        (done,) = editor.of("tool_call_update")
        assert started.kind == "read"
        assert started.status == "pending"
        assert started.title == "read: note.txt"
        assert started.raw_input == {"path": "note.txt"}
        assert [loc.path for loc in started.locations] == [str(env.work / "note.txt")]
        assert started.tool_call_id.endswith(":c1")
        assert done.tool_call_id == started.tool_call_id
        assert done.status == "completed"
        assert "remember the milk" in done.content[0].content.text
        order = kinds(editor)
        assert order.index("tool_call") < order.index("tool_call_update")
        assert editor.text().startswith("calling")


@scripted
async def test_failed_tool_call(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        await conn.prompt(
            session_id=session.session_id, prompt=[text(call("read", path="missing.txt"))]
        )
        (done,) = editor.of("tool_call_update")
        assert done.status == "failed"
        assert "missing.txt" in done.content[0].content.text


@scripted
async def test_bash_tool_call_kind(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        await conn.prompt(
            session_id=session.session_id, prompt=[text(call("bash", command="echo from-bash"))]
        )
        (started,) = editor.of("tool_call")
        done = editor.of("tool_call_update")[-1]
        assert started.kind == "execute"
        assert started.title == "bash: echo from-bash"
        assert done.status == "completed"
        assert "from-bash" in done.content[0].content.text


ASK = ("-e", SCRIPTED, "--set", 'permissions.ask=["bash"]')


@scripted
async def test_permission_allow_once_runs_the_tool(env):
    async with agent(env, *ASK, choice="allow_once") as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        reply = await conn.prompt(
            session_id=session.session_id, prompt=[text(call("bash", command="touch marker"))]
        )
        assert reply.stop_reason == "end_turn"
        assert (env.work / "marker").exists()
        ((asked, options),) = editor.permissions
        (started,) = editor.of("tool_call")
        assert asked.tool_call_id == started.tool_call_id
        assert [(o.option_id, o.kind) for o in options] == [
            ("yes", "allow_once"),
            ("always", "allow_always"),
            ("no", "reject_once"),
        ]
        assert editor.of("tool_call_update")[-1].status == "completed"


@scripted
async def test_permission_reject_fails_the_call(env):
    async with agent(env, *ASK, choice="reject_once") as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        reply = await conn.prompt(
            session_id=session.session_id, prompt=[text(call("bash", command="touch marker"))]
        )
        assert reply.stop_reason == "end_turn"
        assert not (env.work / "marker").exists()
        done = editor.of("tool_call_update")[-1]
        assert done.status == "failed"
        assert "refused" in done.content[0].content.text


@scripted
async def test_permission_cancelled_outcome_declines(env):
    async with agent(env, *ASK, choice=None) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        await conn.prompt(
            session_id=session.session_id, prompt=[text(call("bash", command="touch marker"))]
        )
        assert not (env.work / "marker").exists()
        assert editor.of("tool_call_update")[-1].status == "failed"


@scripted
async def test_permission_allow_always_is_remembered(env):
    async with agent(env, *ASK, choice="allow_always") as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        for _ in range(2):  # "always" allows exactly this call, so the same call is not asked again
            await conn.prompt(
                session_id=session.session_id,
                prompt=[text(call("bash", command="echo again >> log"))],
            )
        assert (env.work / "log").read_text() == "again\nagain\n"
        assert len(editor.permissions) == 1


@scripted
async def test_cancel_during_the_reply(env):
    env.configure(delay_ms=400, reply="one two three four five six seven eight nine ten")
    async with agent(env) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        turn = __import__("asyncio").ensure_future(
            conn.prompt(session_id=session.session_id, prompt=[text("go")])
        )
        await until(lambda: editor.of("agent_message_chunk"), "the first words")
        await conn.cancel(session_id=session.session_id)
        reply = await turn
        assert reply.stop_reason == "cancelled"
        assert len(editor.text().split()) < 10


@scripted
async def test_cancel_during_a_tool(env):
    async with agent(env, "-e", SCRIPTED) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        turn = __import__("asyncio").ensure_future(
            conn.prompt(
                session_id=session.session_id, prompt=[text(call("bash", command="sleep 60"))]
            )
        )
        await until(lambda: editor.of("tool_call"), "the tool call")
        await conn.cancel(session_id=session.session_id)
        reply = await turn
        assert reply.stop_reason == "cancelled"
        assert editor.of("tool_call_update")[-1].status == "failed"


# The permission question names no call: tau-acp must tie it to the call τ asks about.


def batch(*calls: tuple[str, dict]) -> str:
    return "\n".join(call(tool, **args) for tool, args in calls)


@scripted
async def test_permission_is_asked_about_the_call_that_asks(env):
    """Two writes in one batch: the first is allowed by a rule, the second asks. The editor must be
    shown the second (it used to be shown the first, and approved the wrong file)."""
    serve = (
        *("-e", SCRIPTED),
        *("--set", 'permissions.allow=["write:*safe.txt"]'),
        *("--set", 'permissions.ask=["write"]'),
    )
    async with agent(env, *serve, choice="reject_once") as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        both = batch(
            ("write", {"path": "safe.txt", "content": "ok"}),
            ("write", {"path": "secret.txt", "content": "pwn"}),
        )
        await conn.prompt(session_id=session.session_id, prompt=[text(both)])
        ((asked, _options),) = editor.permissions
        assert asked.tool_call_id.endswith(":c2")
        assert asked.raw_input["path"] == "secret.txt"
        assert "secret.txt" in asked.title
        assert f"write: {env.work / 'secret.txt'}" in asked.content[0].content.text
        assert (env.work / "safe.txt").exists()
        assert not (env.work / "secret.txt").exists()  # rejected


@scripted
async def test_identical_calls_are_asked_about_one_each(env):
    async with agent(env, *ASK) as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        twice = batch(("bash", {"command": "echo hi"}), ("bash", {"command": "echo hi"}))
        await conn.prompt(session_id=session.session_id, prompt=[text(twice)])
        assert len(editor.permissions) == 2
        ids = [asked.tool_call_id for asked, _options in editor.permissions]
        assert sorted(i.rsplit(":", 1)[1] for i in ids) == ["c1", "c2"]
        assert all("bash: echo hi" in a.content[0].content.text for a, _o in editor.permissions)


@scripted
async def test_permission_shows_the_resolved_path_tau_asks_about(env):
    """τ's question names the real path a symlink leads to: so does the request."""
    serve = (*("-e", SCRIPTED), *("--set", 'permissions.ask=["write"]'))
    async with agent(env, *serve, choice="reject_once") as (conn, editor, _process):
        session = await conn.new_session(cwd=str(env.work), mcp_servers=[])
        link = env.work / "link"
        link.symlink_to(env.work / "real.txt")
        await conn.prompt(
            session_id=session.session_id,
            prompt=[text(call("write", path="link", content="x"))],
        )
        ((asked, _options),) = editor.permissions
        assert asked.tool_call_id.endswith(":c1")
        assert f"write: {env.work / 'real.txt'}" in asked.content[0].content.text
