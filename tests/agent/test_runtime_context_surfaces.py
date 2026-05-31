from agent.runtime_context import RuntimeContext, use_runtime_context


def _capture_surface_calls(monkeypatch):
    import agent.runtime_context as runtime_context_mod

    calls = []

    def _capture(surface_name):
        ctx = runtime_context_mod.get_current_runtime_context()
        calls.append((surface_name, ctx))
        return ctx

    monkeypatch.setattr(runtime_context_mod, "get_runtime_context_for_surface", _capture)
    return calls


def test_native_tool_surfaces_observe_bound_runtime_context(monkeypatch):
    calls = _capture_surface_calls(monkeypatch)
    ctx = RuntimeContext(mode="agentops", org_id="org-surfaces", conversation_id="conv-surfaces")

    from tools.memory_tool import memory_tool
    from tools.skills_tool import skills_list
    from tools.skill_manager_tool import skill_manage
    from tools.session_search_tool import session_search
    from tools.cronjob_tools import cronjob
    from tools.send_message_tool import send_message_tool

    class EmptySessionDB:
        def list_sessions(self, limit=10):
            return []

    with use_runtime_context(ctx):
        memory_tool(action="read", store=None)
        skills_list(category="definitely-missing-category")
        skill_manage(action="definitely-not-an-action", name="noop")
        session_search(db=EmptySessionDB())
        cronjob(action="definitely-not-an-action")
        send_message_tool({"action": "send"})

    observed = {surface: observed_ctx for surface, observed_ctx in calls}
    assert observed["memory"] == ctx
    assert observed["skills"] == ctx
    assert observed["session_state"] == ctx
    assert observed["cron"] == ctx
    assert observed["delivery"] == ctx


def test_credential_resolution_observes_bound_runtime_context(monkeypatch):
    calls = _capture_surface_calls(monkeypatch)
    ctx = RuntimeContext(mode="agentops", org_id="org-creds", conversation_id="conv-creds")

    import agent.credential_pool as credential_pool

    monkeypatch.setattr(credential_pool, "read_credential_pool", lambda provider: [])
    monkeypatch.setattr(credential_pool, "write_credential_pool", lambda provider, entries: None)
    monkeypatch.setattr(credential_pool, "_seed_from_singletons", lambda provider, entries: (False, set()))
    monkeypatch.setattr(credential_pool, "_seed_from_env", lambda provider, entries: (False, set()))
    monkeypatch.setattr(credential_pool, "_prune_stale_seeded_entries", lambda entries, active_sources: False)
    monkeypatch.setattr(credential_pool, "_normalize_pool_priorities", lambda provider, entries: False)

    with use_runtime_context(ctx):
        pool = credential_pool.load_pool("openai")

    assert pool.provider == "openai"
    assert ("credential_resolution", ctx) in calls


def test_conversation_loop_exposes_runtime_context_to_logging_audit_surface():
    import inspect
    from agent import conversation_loop

    source = inspect.getsource(conversation_loop.run_conversation)
    assert 'get_runtime_context_for_surface("logging_audit")' in source


def test_cron_scheduler_passes_job_payload_as_runtime_work_item():
    import inspect
    from cron import scheduler

    source = inspect.getsource(scheduler)
    assert "runtime_work_item=job" in source


def test_cron_agent_initialization_receives_runtime_context_from_job():
    ctx_payload = {"mode": "agentops", "org_id": "org-cron", "conversation_id": "conv-cron"}
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    from agent.runtime_context import get_runtime_context_for_surface, resolve_runtime_context

    cron_ctx = resolve_runtime_context(work_item={"runtime_context": ctx_payload})
    with use_runtime_context(cron_ctx):
        FakeAIAgent(runtime_context=cron_ctx)
        assert get_runtime_context_for_surface("cron") == cron_ctx

    assert captured["runtime_context"].org_id == "org-cron"
    assert captured["runtime_context"].conversation_id == "conv-cron"
