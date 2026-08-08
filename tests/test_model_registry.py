"""Registry parsing and per-host filtering."""

from __future__ import annotations

from src import host_profile, model_registry


def test_resolves_hostname_match(monkeypatch, write_config):
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc":  {"platform": "win32", "hostname": "TEST-PC", "enabled": ["qwen"]},
            "mac": {"platform": "darwin", "default": True, "enabled": []},
        },
        "models": {
            "qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081},
            "other": {"display_name": "other-model", "backend": "openai", "port": 8082},
        },
    })
    monkeypatch.delenv("LOCAL_LLM_HUB_HOST", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "test-pc")

    prof = host_profile.resolve()
    assert prof.id == "pc"
    assert prof.enabled == ["qwen"]

    ids = {m.id for m in model_registry.enabled_models()}
    assert ids == {"qwen"}   # only the enabled row surfaces


def test_env_override_wins(monkeypatch, write_config):
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc":  {"platform": "win32", "default": True, "enabled": ["qwen", "glm"]},
            "mac": {"platform": "darwin", "enabled": ["qwen"]},
        },
        "models": {
            "qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081},
            "glm":  {"display_name": "glm-4.5-air", "backend": "openai", "port": 8082},
        },
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "mac")

    prof = host_profile.resolve()
    assert prof.id == "mac"
    ids = {m.id for m in model_registry.enabled_models()}
    assert "glm" not in ids
    assert "qwen" in ids


def test_desired_model_ids_filters_to_launchable_eager_rows(monkeypatch, write_config):
    """#430: the desired set derives from the registry — eager ∧ launchable.
    Virtual aliases, other-host rows, and unknown ids can never appear; an
    unowned eager row (no ``host:`` = local everywhere) is included."""
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc": {"platform": "win32", "default": True,
                   "enabled": ["qwen", "qwen_virtual", "whisp", "remote"]},
            "mac": {"platform": "darwin", "enabled": ["remote"]},
        },
        "models": {
            # Unowned (no host:) → local everywhere it is enabled.
            "qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081},
            "qwen_virtual": {
                "display_name": "qwen3.5-9b-nothink", "backend": "openai",
                "port": 8081, "virtual": True,
            },
            "whisp": {"display_name": "w", "backend": "whisper",
                      "engine": "whisper-server", "port": 8090},
            # Cross-enabled here but owned by mac — never desired here.
            "remote": {"display_name": "r", "backend": "openai", "port": 8082,
                       "host": "mac"},
            "disabled": {"display_name": "gemma", "backend": "openai", "port": 8087},
        },
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")

    # Order follows the YAML `models:` mapping — safe_dump sorts keys here.
    assert model_registry.desired_model_ids() == ["qwen", "whisp"]


def test_launchable_local_ids_excludes_remote_virtual_and_nonspawnable(monkeypatch, write_config):
    """The bulk launchers (run_all.*) enumerate this. It must honour every
    rule run_backend enforces: only enabled rows, only spawnable backends
    (openai/whisper), drop virtual aliases, and drop rows owned by another
    host (cross-enabled but not run here).
    """
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc":  {"platform": "win32", "default": True,
                    "enabled": ["local_llm", "virt", "whisp", "remote"]},
            "mac": {"platform": "darwin",
                    "enabled": ["local_llm", "virt", "whisp", "remote"]},
        },
        "models": {
            "local_llm": {"display_name": "local", "backend": "openai", "port": 8081},
            "virt": {"display_name": "virt", "backend": "openai", "port": 8081,
                     "virtual": True},
            "whisp": {"display_name": "w", "backend": "whisper", "engine": "whisper-server",
                      "port": 8090},
            # Cross-enabled here but owned by mac — never spawned locally.
            "remote": {"display_name": "r", "backend": "openai", "port": 8082,
                       "host": "mac"},
        },
    })

    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")
    ids = model_registry.launchable_local_ids()
    assert ids == ["local_llm", "whisp"]          # virt/remote both dropped

    # On the owning host, the previously-remote row becomes launchable.
    # (Order follows the YAML `models:` mapping — safe_dump sorts keys here.)
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "mac")
    assert model_registry.launchable_local_ids() == ["local_llm", "remote", "whisp"]


def test_resolve_by_alias(monkeypatch, write_config):
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc": {"platform": "win32", "default": True, "enabled": ["qwen"]},
        },
        "models": {
            "qwen": {
                "display_name": "qwen3.5-9b",
                "backend": "openai",
                "port": 8081,
                "aliases": ["qwen", "qwen3.5"],
            },
        },
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")

    m = model_registry.resolve("qwen3.5")
    assert m is not None
    assert m.id == "qwen"
    assert model_registry.resolve("nonexistent") is None


def test_gemma_per_host_filtering(monkeypatch, write_config):
    """Both gemma4 rows must show on tower and stay hidden on mac-mini-m4."""
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "tower":     {"platform": "win32", "default": True, "enabled": ["qwen", "glm", "gemma4_e4b", "gemma4_26b"]},
            "mac-mini-m4": {"platform": "darwin", "enabled": ["qwen"]},
        },
        "models": {
            "qwen":       {"display_name": "qwen3.5-9b",        "backend": "openai", "port": 8081},
            "glm":        {"display_name": "glm-4.5-air",       "backend": "openai", "port": 8082},
            "gemma4_e4b": {"display_name": "gemma4-e4b-it",     "backend": "openai", "port": 8086},
            "gemma4_26b": {"display_name": "gemma4-26b-a4b-it", "backend": "openai", "port": 8087},
        },
    })

    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "tower")
    names_pc = {m.display_name for m in model_registry.enabled_models()}
    assert {"qwen3.5-9b", "glm-4.5-air", "gemma4-e4b-it", "gemma4-26b-a4b-it"} <= names_pc
    assert model_registry.resolve("gemma4-e4b-it").port == 8086
    assert model_registry.resolve("gemma4-26b-a4b-it").port == 8087

    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "mac-mini-m4")
    names_mac = {m.display_name for m in model_registry.enabled_models()}
    assert "gemma4-e4b-it" not in names_mac
    assert "gemma4-26b-a4b-it" not in names_mac
    assert model_registry.resolve("gemma4-e4b-it") is None
    assert model_registry.resolve("gemma4-26b-a4b-it") is None


def test_whisper_entry(monkeypatch, write_config):
    """Whisper is a distinct backend; runs on 8090, surfaces on tower only."""
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "tower":     {"platform": "win32", "default": True, "enabled": ["qwen", "whisper"]},
            "mac-mini-m4": {"platform": "darwin", "enabled": ["qwen"]},
        },
        "models": {
            "qwen":    {"display_name": "qwen3.5-9b",    "backend": "openai",  "port": 8081},
            "whisper": {
                "display_name": "whisper-large-v3-turbo",
                "backend": "whisper",
                "engine": "whisper-server",
                "port": 8090,
                "hf_repo": "ggerganov/whisper.cpp",
                "hf_pattern": "ggml-large-v3-turbo.bin",
                "model_path": "models/ggml-large-v3-turbo.bin",
                "args": ["--threads", "4", "--gpu", "1"],
            },
        },
    })

    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "tower")
    m = model_registry.resolve("whisper-large-v3-turbo")
    assert m is not None
    assert m.id == "whisper"
    assert m.backend == "whisper"
    assert m.engine == "whisper-server"
    assert m.port == 8090
    assert m.url == "http://127.0.0.1:8090/v1"
    assert "--gpu" in m.args

    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "mac-mini-m4")
    assert model_registry.resolve("whisper-large-v3-turbo") is None


def test_resolve_by_registry_id(monkeypatch, write_config):
    """Regression: the SPA Playground dropdown sends ``m.id`` (the YAML
    key), not ``display_name``. resolve() must accept it — otherwise
    rows whose id is not also listed under aliases (qwen35_4b,
    gemma4_e4b, gemma4_26b, gemini_flash_lite in the real registry)
    400 on the Playground.
    """
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc": {"platform": "win32", "default": True,
                   "enabled": ["qwen35_4b", "gemma4_26b"]},
        },
        "models": {
            # id != display_name, and id NOT in aliases — same shape as
            # the real qwen35_4b / gemma4_26b rows.
            "qwen35_4b": {
                "display_name": "qwen3.5-4b",
                "backend": "openai",
                "port": 8088,
                "aliases": ["agentic_light"],
            },
            "gemma4_26b": {
                "display_name": "gemma4-26b-a4b-it",
                "backend": "openai",
                "port": 8087,
                "aliases": ["agentic_heavy"],
            },
        },
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")

    # Resolve via every channel: id, display_name, alias — all three
    # land on the same model.
    by_id = model_registry.resolve("qwen35_4b")
    by_display = model_registry.resolve("qwen3.5-4b")
    by_alias = model_registry.resolve("agentic_light")
    assert by_id is not None and by_display is not None and by_alias is not None
    assert by_id.id == by_display.id == by_alias.id == "qwen35_4b"

    # The gemma row's id also resolves, even though it's not in aliases.
    assert model_registry.resolve("gemma4_26b").id == "gemma4_26b"

    # ``all_names`` now includes the id so /v1/models lists every handle.
    qwen = model_registry.resolve("qwen35_4b")
    assert "qwen35_4b" in qwen.all_names
    assert "qwen3.5-4b" in qwen.all_names
    assert "agentic_light" in qwen.all_names


def test_virtual_nothink_alias_shares_backend(monkeypatch, write_config):
    """The no-think alias (#161) is a virtual model: it shares qwen's :8088
    backend URL, carries an inject_extra overlay, and is flagged virtual so the
    admin UI never treats it as a startable process. Plain qwen35_4b stays a
    real, non-virtual, no-overlay row.
    """
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc": {"platform": "win32", "default": True,
                   "enabled": ["qwen35_4b", "qwen35_4b_nothink"]},
        },
        "models": {
            "qwen35_4b": {
                "display_name": "qwen3.5-4b",
                "backend": "openai",
                "port": 8088,
                "aliases": ["agentic_light"],
            },
            "qwen35_4b_nothink": {
                "display_name": "qwen3.5-4b-nothink",
                "backend": "openai",
                "port": 8088,                      # shared with qwen35_4b
                "virtual": True,
                "aliases": ["agentic_light_nothink"],
                "inject_extra": {"chat_template_kwargs": {"enable_thinking": False}},
            },
        },
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")

    # Resolves via id, display_name, and role alias — all to the same row,
    # pointing at qwen's running backend (no own port/process).
    by_id = model_registry.resolve("qwen35_4b_nothink")
    by_name = model_registry.resolve("qwen3.5-4b-nothink")
    by_alias = model_registry.resolve("agentic_light_nothink")
    assert by_id is not None and by_name is not None and by_alias is not None
    assert by_id.id == by_name.id == by_alias.id == "qwen35_4b_nothink"
    assert by_id.url == "http://127.0.0.1:8088/v1"   # shares qwen's :8088
    assert by_id.virtual is True
    assert by_id.inject_extra == {"chat_template_kwargs": {"enable_thinking": False}}

    # Plain qwen is untouched: real backend, no overlay, same :8088 process.
    plain = model_registry.resolve("agentic_light")
    assert plain.id == "qwen35_4b"
    assert plain.virtual is False
    assert plain.inject_extra is None
    assert plain.url == by_id.url                    # same single backend


def test_model_url_from_port(monkeypatch, write_config):
    write_config({
        "hub": {"port": 8000},
        "hosts": {"pc": {"platform": "win32", "default": True, "enabled": ["qwen"]}},
        "models": {
            "qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081},
        },
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")

    m = model_registry.resolve("qwen3.5-9b")
    assert m.url == "http://127.0.0.1:8081/v1"


def test_startup_and_idle_unload_fields(monkeypatch, write_config):
    """#422 schema: `startup: on_demand` + `idle_unload_minutes` parse; the
    default is eager; an unknown startup value normalizes to eager (a typo
    must degrade to always-on, never to a model that refuses to start)."""
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc": {"platform": "win32", "default": True,
                   "enabled": ["gemma", "qwen", "typo"]},
        },
        "models": {
            "gemma": {
                "display_name": "gemma4-26b-a4b-it", "backend": "openai",
                "port": 8087, "startup": "on_demand", "idle_unload_minutes": 30,
            },
            "qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081},
            "typo": {
                "display_name": "typo-model", "backend": "openai",
                "port": 8085, "startup": "lazy",
            },
        },
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")

    gemma = model_registry.resolve("gemma4-26b-a4b-it")
    assert gemma.startup == model_registry.STARTUP_ON_DEMAND
    assert gemma.idle_unload_minutes == 30

    qwen = model_registry.resolve("qwen3.5-9b")
    assert qwen.startup == model_registry.STARTUP_EAGER
    assert qwen.idle_unload_minutes is None

    typo = model_registry.resolve("typo-model")
    assert typo.startup == model_registry.STARTUP_EAGER


def test_desired_model_ids_excludes_on_demand_rows(monkeypatch, write_config):
    """#422/#430: an on_demand row is never in the desired set — hub
    autostart skips it. The first request loads it (src/on_demand.py owns
    that lifecycle)."""
    write_config({
        "hub": {"port": 8000},
        "hosts": {
            "pc": {"platform": "win32", "default": True, "enabled": ["qwen", "gemma"]},
        },
        "models": {
            "qwen": {"display_name": "qwen3.5-9b", "backend": "openai", "port": 8081},
            "gemma": {
                "display_name": "gemma4-26b-a4b-it", "backend": "openai",
                "port": 8087, "startup": "on_demand", "idle_unload_minutes": 30,
            },
        },
    })
    monkeypatch.setenv("LOCAL_LLM_HUB_HOST", "pc")

    assert model_registry.desired_model_ids() == ["qwen"]

