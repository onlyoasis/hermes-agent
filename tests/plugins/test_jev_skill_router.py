"""Offline tests for the jev-skill-router plugin.

Covers ``plugins/jev-skill-router/`` with NO network access — the HTTP
client is always a scripted fake injected at the plugin seam:

  * ``catalog.py`` — compilation from an isolated HERMES_HOME, disabled /
    platform filtering, outbound subset, duplicate-id precedence,
    incomplete-scan semantics, stale-candidate recheck, body excerpts.
  * ``questions.py`` — two-phase question shapes, 255-option Choice cap,
    all-or-nothing answer validation, shortlist ranking.
  * ``client.py`` — error mapping, single-shot (no retry), structural
    rejection of invalid 200s, deadline handling.
  * ``policy.py`` — config validation/degradation, deterministic gate
    ordering, dedup bounds, budget reserve/settle, advice contract,
    decision records, conservative token estimates.
  * ``__init__.py`` — hook-only registration; off/shadow/recommend
    behaviour end-to-end with scripted vendor responses; every failure
    path falls back to ``None`` without breaking the turn.

Config caches (skill_utils raw-config cache, config readonly cache) are
keyed by path + mtime, so per-test tmp HERMES_HOME homes never collide;
the skill_utils cache is cleared explicitly after config rewrites.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for var in (
        "TYPESAFE_API_KEY",
        "HERMES_SESSION_SOURCE",
        "HERMES_CRON_SESSION",
        "HERMES_SESSION_PLATFORM",
        "HERMES_PLATFORM",
        "HERMES_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield hermes_home
    from agent.skill_utils import _external_dirs_cache_clear

    _external_dirs_cache_clear()


# ---------------------------------------------------------------------------
# Module loading (bundled plugins load as hermes_plugins.<slug>, same
# namespace-package pattern as test_security_guidance_plugin.py)
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def plugin():
    plugin_dir = _repo_root() / "plugins" / "jev-skill-router"
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.jev_skill_router",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.jev_skill_router"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.jev_skill_router"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_skill(home: Path, name: str, description: str,
                platforms: str = "", body: str = "Body text.\n") -> Path:
    skill_dir = home / "skills" / name.replace(" ", "-").lower()
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    front = f"name: {name}\ndescription: {description}\n"
    if platforms:
        front += f"platforms: [{platforms}]\n"
    skill_md.write_text(f"---\n{front}---\n\n{body}", encoding="utf-8")
    return skill_md


def _write_config(home: Path, mapping: dict) -> None:
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump(mapping), encoding="utf-8")
    from agent.skill_utils import _external_dirs_cache_clear

    _external_dirs_cache_clear()


def _plugin_config(**overrides) -> dict:
    entry = {
        "mode": "recommend",
        "allowed_platforms": ["telegram"],
        "min_any_match": 0.3,
        "min_choice_prob": 0.3,
        "min_fit": 0.3,
        # Enabling requires explicit numbers + declared outbound scope.
        "max_input_tokens": 50000,
        "daily_budget_usd": 1.0,
        "outbound_catalog_full": True,
    }
    if "outbound_catalog" in overrides:
        # An exact id list IS the declaration; don't also claim full.
        entry.pop("outbound_catalog_full", None)
    entry.update(overrides)
    return {"plugins": {"entries": {"jev-skill-router": entry}}}


class FakeJevClient:
    """Scripted stand-in for JevClient sharing the fixture's response queue."""

    def __init__(self, api_key: str, model: str, scripted: list):
        assert api_key, "client must receive a non-empty key"
        self.model = model
        self.calls: list = []
        self._scripted = scripted  # by reference: late appends visible

    def ask(self, state, questions, timeout_ms):
        self.calls.append({"state": state, "questions": questions,
                           "timeout_ms": timeout_ms})
        if not self._scripted:
            raise AssertionError("scripted vendor responses exhausted")
        action = self._scripted.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class _ScriptedResponses:
    """Queue of scripted vendor responses plus the clients created."""

    def __init__(self):
        self.queue: list = []
        self.created: list = []

    def append(self, item) -> None:
        self.queue.append(item)

    def __len__(self) -> int:
        return len(self.queue)


@pytest.fixture
def fake_client(monkeypatch, plugin):
    """Returns the shared scripted-response queue; ``queue.created`` holds
    the FakeJevClient instances the orchestrator constructed."""
    scripted = _ScriptedResponses()

    def factory(api_key, model, base_url="", transport=None):
        client = FakeJevClient(api_key, model, scripted.queue)
        scripted.created.append(client)
        return client

    monkeypatch.setattr(plugin.client_mod, "JevClient", factory)
    _reset_dedup(plugin)
    return scripted


def _reset_dedup(plugin):
    plugin._DEDUP = plugin.policy.TurnDedup()


def _phase_one_answer(choice: str, probs: dict, any_match: float) -> dict:
    return {
        "answers": {
            "which": {"type": "choice", "choice": choice, "probabilities": probs},
            "any_match": {"type": "noul", "noul": any_match},
        },
        "usage": {"input_tokens": 500, "output_tokens": 20},
    }


def _phase_two_answer(winner: str, probs: dict, fits: dict) -> dict:
    return {
        "answers": {
            "which": {"type": "choice", "choice": winner, "probabilities": probs},
            **{f"fits::{sid}": {"type": "noul", "noul": v} for sid, v in fits.items()},
        },
        "usage": {"input_tokens": 200, "output_tokens": 10},
    }


def _turn_kwargs(plugin, home, message="帮我生成一张图片", platform="telegram"):
    _reset_dedup(plugin)
    return {
        "session_id": "s1",
        "task_id": "t1",
        "turn_id": "turn-1",
        "user_message": message,
        "conversation_history": [],
        "is_first_turn": True,
        "model": "test-model",
        "platform": platform,
        "parent_session_id": "",
        "sender_id": "u1",
    }


def _decisions(home: Path) -> list:
    path = home / "jev-skill-router" / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------------------
# catalog.py
# ---------------------------------------------------------------------------

class TestCatalog:
    def test_compiles_sorted_entries(self, plugin, _isolate_env):
        _make_skill(_isolate_env, "beta-skill", "Beta does b.")
        _make_skill(_isolate_env, "alpha-skill", "Alpha does a.")
        compiled = plugin.catalog_mod.compile_catalog(None)
        assert compiled.complete is True
        assert compiled.ids() == ["alpha-skill", "beta-skill"]
        entry = compiled.find("alpha-skill")
        assert entry.description == "Alpha does a."
        assert entry.skill_md.name == "SKILL.md"

    def test_outbound_subset_hides_unlisted(self, plugin, _isolate_env):
        _make_skill(_isolate_env, "alpha-skill", "Alpha does a.")
        _make_skill(_isolate_env, "secret-skill", "Secret stays local.")
        compiled = plugin.catalog_mod.compile_catalog({"alpha-skill"})
        assert compiled.ids() == ["alpha-skill"]

    def test_disabled_skill_excluded(self, plugin, _isolate_env):
        _make_skill(_isolate_env, "alpha-skill", "Alpha does a.")
        _make_skill(_isolate_env, "beta-skill", "Beta does b.")
        _write_config(_isolate_env, {"skills": {"disabled": ["beta-skill"]}})
        compiled = plugin.catalog_mod.compile_catalog(None)
        assert compiled.ids() == ["alpha-skill"]

    def test_platform_mismatch_excluded(self, plugin, _isolate_env):
        _make_skill(_isolate_env, "alpha-skill", "Alpha does a.")
        _make_skill(_isolate_env, "win-skill", "Windows only.", platforms="windows")
        compiled = plugin.catalog_mod.compile_catalog(None)
        assert compiled.ids() == ["alpha-skill"]

    def test_local_root_wins_duplicate_ids(self, plugin, _isolate_env, tmp_path):
        _make_skill(_isolate_env, "dup-skill", "Local variant.")
        external = tmp_path / "ext-skills"
        ext_dir = external / "dup-skill"
        ext_dir.mkdir(parents=True)
        (ext_dir / "SKILL.md").write_text(
            "---\nname: dup-skill\ndescription: External variant.\n---\n", encoding="utf-8"
        )
        _write_config(_isolate_env, {"skills": {"external_dirs": [str(external)]}})
        compiled = plugin.catalog_mod.compile_catalog(None)
        assert compiled.ids() == ["dup-skill"]
        assert compiled.find("dup-skill").description == "Local variant."

    def test_scan_failure_marks_incomplete(self, plugin, _isolate_env, monkeypatch):
        _make_skill(_isolate_env, "alpha-skill", "Alpha does a.")

        def boom(root, filename):
            raise OSError("disk vanished")

        monkeypatch.setattr(plugin.catalog_mod, "iter_skill_index_files", boom)
        compiled = plugin.catalog_mod.compile_catalog(None)
        assert compiled.complete is False
        assert compiled.scan_error
        assert compiled.entries == []

    def test_entry_still_available_detects_changes(self, plugin, _isolate_env):
        skill_md = _make_skill(_isolate_env, "alpha-skill", "Alpha does a.")
        compiled = plugin.catalog_mod.compile_catalog(None)
        entry = compiled.find("alpha-skill")
        assert plugin.catalog_mod.entry_still_available(entry) is True

        _write_config(_isolate_env, {"skills": {"disabled": ["alpha-skill"]}})
        assert plugin.catalog_mod.entry_still_available(entry) is False

        _write_config(_isolate_env, {})
        skill_md.unlink()
        assert plugin.catalog_mod.entry_still_available(entry) is False

    def test_body_excerpt_respects_limit(self, plugin, _isolate_env):
        _make_skill(_isolate_env, "alpha-skill", "Alpha does a.", body="A" * 200 + "\n")
        compiled = plugin.catalog_mod.compile_catalog(None)
        excerpt = plugin.catalog_mod.body_excerpt(compiled.find("alpha-skill").skill_md, 50)
        assert len(excerpt) == 51  # 50 chars + ellipsis
        assert excerpt.endswith("…")

    def test_detail_text_joins_excerpt(self, plugin, _isolate_env):
        _make_skill(_isolate_env, "alpha-skill", "Alpha does a.")
        compiled = plugin.catalog_mod.compile_catalog(None)
        assert compiled.find("alpha-skill").detail_text("EXCERPT") == "Alpha does a. — EXCERPT"
        assert compiled.find("alpha-skill").detail_text("") == "Alpha does a."


# ---------------------------------------------------------------------------
# questions.py
# ---------------------------------------------------------------------------

class TestQuestions:
    def _entries(self, plugin, count):
        return [
            plugin.catalog_mod.CatalogEntry(
                skill_id=f"skill-{i}", name=f"skill-{i}",
                description=f"Skill number {i}.", category=None,
                skill_md=Path(f"/tmp/skill-{i}/SKILL.md"),
            )
            for i in range(count)
        ]

    def test_phase_one_shape(self, plugin):
        questions = plugin.questions_mod.build_phase_one(
            "request text", self._entries(plugin, 2))
        which = questions["which"]
        assert which["type"] == "choice"
        assert set(which["criteria"]) == {"skill-0", "skill-1", "none"}
        assert which["criteria"]["none"]
        assert questions["any_match"]["type"] == "noul"

    def test_phase_one_respects_choice_cap(self, plugin):
        questions = plugin.questions_mod.build_phase_one("r", self._entries(plugin, 254))
        assert len(questions["which"]["criteria"]) == 255  # 254 + none

    def test_interpret_phase_one_happy(self, plugin):
        answers = _phase_one_answer(
            "skill-1", {"skill-1": 0.8, "skill-0": 0.15, "none": 0.05}, 0.9)
        top, top_prob, any_match, probs = plugin.questions_mod.interpret_phase_one(
            answers["answers"], ["skill-0", "skill-1"]
        )
        assert top == "skill-1"
        assert top_prob == pytest.approx(0.8)
        assert any_match == pytest.approx(0.9)
        assert probs["skill-0"] == pytest.approx(0.15)

    def test_interpret_phase_one_unknown_option_rejected(self, plugin):
        answers = _phase_one_answer("skill-0", {"ghost": 0.9, "skill-0": 0.1}, 0.9)
        with pytest.raises(plugin.questions_mod.InvalidAnswersError):
            plugin.questions_mod.interpret_phase_one(answers["answers"], ["skill-0"])

    def test_interpret_phase_one_missing_question_rejected(self, plugin):
        answers = {"which": {"type": "choice", "choice": "skill-0",
                             "probabilities": {"skill-0": 1.0}}}
        with pytest.raises(plugin.questions_mod.InvalidAnswersError):
            plugin.questions_mod.interpret_phase_one(answers, ["skill-0"])

    def test_interpret_phase_one_bad_noul_rejected(self, plugin):
        answers = _phase_one_answer("skill-0", {"skill-0": 1.0}, 1.5)
        with pytest.raises(plugin.questions_mod.InvalidAnswersError):
            plugin.questions_mod.interpret_phase_one(answers["answers"], ["skill-0"])

    def test_shortlist_ranking_excludes_none(self, plugin):
        probs = {"none": 0.5, "b": 0.3, "a": 0.6, "c": 0.3}
        assert plugin.questions_mod.shortlist_from_probabilities(probs, 2) == ["a", "b"]
        assert plugin.questions_mod.shortlist_from_probabilities(probs, 99) == ["a", "b", "c"]

    def test_phase_two_shape(self, plugin):
        entries = self._entries(plugin, 2)
        shortlist = [(entries[0], "detail-0"), (entries[1], "detail-1")]
        questions = plugin.questions_mod.build_phase_two("request", shortlist)
        assert set(questions["which"]["criteria"]) == {"skill-0", "skill-1", "none"}
        assert questions["which"]["criteria"]["skill-0"] == "detail-0"
        assert questions["fits::skill-0"]["type"] == "noul"
        assert "detail-1" in questions["fits::skill-1"]["instructions"]

    def test_interpret_phase_two_missing_fit_rejected(self, plugin):
        answers = {
            "which": {"type": "choice", "choice": "skill-0",
                      "probabilities": {"skill-0": 0.9, "none": 0.1}},
            "fits::skill-0": {"type": "noul", "noul": 0.8},
            # fits::skill-1 missing -> whole response rejected
        }
        with pytest.raises(plugin.questions_mod.InvalidAnswersError):
            plugin.questions_mod.interpret_phase_two(answers, ["skill-0", "skill-1"])

    def test_interpret_phase_two_none_winner_ok(self, plugin):
        answers = _phase_two_answer("none", {"none": 0.7, "skill-0": 0.3},
                                    {"skill-0": 0.1})
        winner, prob, fits = plugin.questions_mod.interpret_phase_two(
            answers["answers"], ["skill-0"]
        )
        assert winner == "none"
        assert fits == {"skill-0": pytest.approx(0.1)}


# ---------------------------------------------------------------------------
# client.py
# ---------------------------------------------------------------------------

class TestClient:
    def _client(self, plugin, transport):
        return plugin.client_mod.JevClient(
            "k-test", "jev-1.13.0", base_url="https://unit.test", transport=transport)

    def test_ask_happy_path(self, plugin):
        seen = {}

        def transport(url, headers, payload, timeout_s):
            seen.update(url=url, headers=headers, payload=payload, timeout_s=timeout_s)
            return 200, _phase_one_answer("a", {"a": 1.0}, 0.5), "{}"

        result = self._client(plugin, transport).ask({"request": "r"}, {"q": {}}, 1500)
        assert seen["url"] == "https://unit.test/v1/systemone"
        assert seen["headers"]["Authorization"] == "Bearer k-test"
        assert seen["payload"]["model"] == "jev-1.13.0"
        assert seen["timeout_s"] == pytest.approx(1.5)
        assert result["answers"]["which"]["choice"] == "a"

    @pytest.mark.parametrize("status,exc_name", [
        (401, "JevAuthError"), (403, "JevAuthError"), (429, "JevRateLimited"),
        (529, "JevOverloaded"), (422, "JevBadRequest"), (500, "JevTransportError"),
    ])
    def test_status_mapping(self, plugin, status, exc_name):
        exc_type = getattr(plugin.client_mod, exc_name)

        def transport(url, headers, payload, timeout_s):
            return status, None, "err"

        with pytest.raises(exc_type):
            self._client(plugin, transport).ask({}, {}, 100)

    def test_invalid_200_rejected_whole(self, plugin):
        body = {"answers": {"q": {"type": "noul", "noul": 1.5}}}

        def transport(url, headers, payload, timeout_s):
            return 200, body, json.dumps(body)

        with pytest.raises(plugin.client_mod.JevInvalidResponseError):
            self._client(plugin, transport).ask({}, {"q": {}}, 100)

    def test_no_retry_single_call(self, plugin):
        calls = []

        def transport(url, headers, payload, timeout_s):
            calls.append(1)
            raise plugin.client_mod.JevTimeoutError("boom")

        with pytest.raises(plugin.client_mod.JevTimeoutError):
            self._client(plugin, transport).ask({}, {}, 100)
        assert len(calls) == 1

    def test_zero_deadline_short_circuits(self, plugin):
        calls = []

        def transport(url, headers, payload, timeout_s):
            calls.append(1)
            return 200, {}, "{}"

        with pytest.raises(plugin.client_mod.JevTimeoutError):
            self._client(plugin, transport).ask({}, {}, 0)
        assert calls == []

    def test_transport_exception_normalised(self, plugin):
        def transport(url, headers, payload, timeout_s):
            raise ConnectionError("reset")

        with pytest.raises(plugin.client_mod.JevTransportError):
            self._client(plugin, transport).ask({}, {}, 100)


# ---------------------------------------------------------------------------
# policy.py — config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_defaults_are_off(self, plugin):
        cfg, errors = plugin.policy.config_from_mapping({})
        assert cfg.mode == "off"
        assert cfg.enabled is False
        assert errors == []
        assert cfg.outbound_catalog is None  # undeclared — never "full"
        assert cfg.outbound_catalog_full is False
        assert cfg.allowed_platforms == frozenset()

    def test_invalid_mode_degrades_to_off(self, plugin):
        cfg, errors = plugin.policy.config_from_mapping({"mode": "yolo"})
        assert cfg.mode == "off"
        assert any("mode" in e for e in errors)

    def test_invalid_fields_reported(self, plugin):
        cfg, errors = plugin.policy.config_from_mapping({
            "daily_budget_usd": -5, "shortlist_size": 0,
            "min_any_match": 3.0, "allowed_platforms": "telegram",
        })
        assert cfg.daily_budget_usd is None
        assert cfg.shortlist_size == 3  # safe default restored
        assert cfg.min_any_match == 0.5
        assert cfg.allowed_platforms == frozenset()
        assert len(errors) == 4

    def test_load_router_config_from_yaml(self, plugin, _isolate_env):
        _write_config(_isolate_env, _plugin_config(mode="shadow",
                                                   outbound_catalog=["a", "b"]))
        cfg, errors = plugin.policy.load_router_config()
        assert errors == []
        assert cfg.mode == "shadow"
        assert cfg.enabled is True
        assert cfg.outbound_catalog == frozenset({"a", "b"})

    def test_load_router_config_missing_entry(self, plugin, _isolate_env):
        _write_config(_isolate_env, {"skills": {"disabled": []}})
        cfg, errors = plugin.policy.load_router_config()
        assert cfg.mode == "off"
        assert errors == []


# ---------------------------------------------------------------------------
# policy.py — gate
# ---------------------------------------------------------------------------

class TestGate:
    def _cfg(self, plugin, **kw):
        return plugin.policy.RouterConfig(
            mode="recommend", allowed_platforms=frozenset({"telegram"}), **kw)

    def _surface(self, plugin, platform="telegram", source="", cron=False):
        return plugin.policy.SurfaceInfo(platform=platform, source=source,
                                          cron_session=cron)

    def test_mode_off_first(self, plugin):
        decision = plugin.policy.evaluate_turn_gate(
            plugin.policy.RouterConfig(), session_id="", turn_id="",
            user_message=None, parent_session_id="", surface=self._surface(plugin),
        )
        assert decision.should_run is False
        assert decision.reason_code == "mode_off"

    def test_order_missing_ids_then_non_text(self, plugin):
        kwargs = dict(session_id="", turn_id="", user_message="hi",
                      parent_session_id="", surface=self._surface(plugin))
        assert plugin.policy.evaluate_turn_gate(
            self._cfg(plugin), **kwargs).reason_code == "missing_ids"
        kwargs.update(session_id="s", turn_id="t", user_message=123)
        assert plugin.policy.evaluate_turn_gate(
            self._cfg(plugin), **kwargs).reason_code == "non_text_message"

    def test_delegated_excluded(self, plugin):
        assert plugin.policy.evaluate_turn_gate(
            self._cfg(plugin), session_id="s", turn_id="t", user_message="hi",
            parent_session_id="parent", surface=self._surface(plugin),
        ).reason_code == "delegated_turn"

    @pytest.mark.parametrize("surface,reason", [
        ({"cron": True}, "non_interactive_surface"),
        ({"source": "kanban"}, "non_interactive_surface"),
        ({"source": "tool"}, "non_interactive_surface"),
        ({"platform": "wechat"}, "platform_not_allowed"),
        ({"platform": ""}, "platform_not_allowed"),
    ])
    def test_surface_exclusions(self, plugin, surface, reason):
        info = self._surface(
            plugin,
            platform=surface.get("platform", "telegram"),
            source=surface.get("source", ""),
            cron=surface.get("cron", False),
        )
        assert plugin.policy.evaluate_turn_gate(
            self._cfg(plugin), session_id="s", turn_id="t", user_message="hi",
            parent_session_id="", surface=info,
        ).reason_code == reason

    def test_duplicate_turn(self, plugin):
        dedup = plugin.policy.TurnDedup()
        kwargs = dict(session_id="s", turn_id="t", user_message="hi",
                      parent_session_id="", surface=self._surface(plugin), dedup=dedup)
        assert plugin.policy.evaluate_turn_gate(self._cfg(plugin), **kwargs).should_run
        dedup.mark(plugin.policy.TurnDedup.key("default", "s", "t"))
        assert plugin.policy.evaluate_turn_gate(
            self._cfg(plugin), **kwargs).reason_code == "duplicate_turn"

    def test_dedup_bounded_and_cleared(self, plugin):
        dedup = plugin.policy.TurnDedup()
        for i in range(600):
            dedup.mark(plugin.policy.TurnDedup.key("p", "s", f"t{i}"))
        assert len(dedup._seen) == plugin.policy.DEDUP_MAX_ENTRIES
        dedup.mark(plugin.policy.TurnDedup.key("p", "s2", "t"))
        dedup.clear_session("s2")
        assert not dedup.seen(plugin.policy.TurnDedup.key("p", "s2", "t"))

    def test_mentions_explicit_skill(self, plugin):
        assert plugin.policy.mentions_explicit_skill("请用 alpha-skill 帮我", ["alpha-skill"])
        assert plugin.policy.mentions_explicit_skill(
            "请用 alpha-skill-2 帮我", ["alpha-skill"]) is False
        assert plugin.policy.mentions_explicit_skill("没有提到", ["alpha-skill"]) is False


# ---------------------------------------------------------------------------
# policy.py — budget & advice
# ---------------------------------------------------------------------------

class TestBudgetAndAdvice:
    def test_reserve_settle_flow(self, plugin, tmp_path):
        ledger = plugin.policy.BudgetLedger(tmp_path, daily_budget_usd=0.01)
        assert ledger.reserve(0.004) is True
        assert ledger.reserve(0.004) is True
        assert ledger.reserve(0.004) is False  # 0.008 booked, cap 0.01
        cost = ledger.settle(0.004, {"input_tokens": 100, "output_tokens": 0}, 0.042, 0.0)
        assert cost == pytest.approx(100 / 1e6 * 0.042)
        snapshot = ledger.snapshot()
        assert snapshot["reserved_usd"] == pytest.approx(0.004)
        assert snapshot["spent_usd"] == pytest.approx(cost)
        assert snapshot["requests"] == 1

    def test_no_cap_always_reservable(self, plugin, tmp_path):
        ledger = plugin.policy.BudgetLedger(tmp_path, daily_budget_usd=None)
        assert ledger.reserve(10_000.0) is True

    def test_validate_advice_contract(self, plugin):
        good = {"schema_version": "jev-skill-advice-v1", "status": "suggested",
                "skill_id": "a", "reason_code": "two_phase_confirmed", "elapsed_ms": 5}
        plugin.policy.validate_advice_payload(good, {"a"})
        with pytest.raises(plugin.policy.AdviceValidationError):
            plugin.policy.validate_advice_payload({**good, "skill_id": "ghost"}, {"a"})
        with pytest.raises(plugin.policy.AdviceValidationError):
            plugin.policy.validate_advice_payload({**good, "skill_id": ""}, {"a"})
        with pytest.raises(plugin.policy.AdviceValidationError):
            plugin.policy.validate_advice_payload({**good, "status": "abstain"}, {"a"})
        with pytest.raises(plugin.policy.AdviceValidationError):
            plugin.policy.validate_advice_payload({**good, "schema_version": "v0"}, {"a"})
        abstain = {"schema_version": "jev-skill-advice-v1", "status": "abstain",
                   "skill_id": "", "reason_code": "phase2_none", "elapsed_ms": 5}
        plugin.policy.validate_advice_payload(abstain, {"a"})

    def test_render_advice_fixed_template(self, plugin):
        text = plugin.policy.render_advice_context("alpha-skill")
        assert "alpha-skill" in text
        assert "相关性建议" in text

    def test_decision_record_minimal(self, plugin, tmp_path):
        plugin.policy.append_decision_record(tmp_path, {"status": "skipped",
                                                        "reason_code": "mode_off"})
        lines = (tmp_path / "jev-skill-router" / "decisions.jsonl").read_text().splitlines()
        assert json.loads(lines[0])["reason_code"] == "mode_off"


# ---------------------------------------------------------------------------
# __init__.py — registration & end-to-end behaviour
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_registers_hooks_only(self, plugin):
        hooks, tools = [], []

        class Ctx:
            @staticmethod
            def register_hook(name, cb):
                hooks.append(name)

            @staticmethod
            def register_tool(*a, **k):
                tools.append(a)

        plugin.register(Ctx())
        assert sorted(hooks) == ["on_session_end", "pre_llm_call"]
        assert tools == []


class TestEndToEnd:
    def test_mode_off_returns_none_without_calls(self, plugin, _isolate_env, fake_client):
        _write_config(_isolate_env, {"skills": {"disabled": []}})
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        assert len(fake_client) == 0
        assert fake_client.created == []
        assert _decisions(_isolate_env) == []

    def _recommend_env(self, plugin, home, monkeypatch, **cfg_overrides):
        monkeypatch.setenv("TYPESAFE_API_KEY", "k-test")
        _make_skill(home, "alpha-skill", "生成图片的技能。")
        _make_skill(home, "beta-skill", "翻译文本的技能。")
        _write_config(home, _plugin_config(**cfg_overrides))

    def test_recommend_full_two_phase(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch)
        fake_client.append(_phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.08, "none": 0.02}, 0.8))
        fake_client.append(_phase_two_answer(
            "alpha-skill", {"alpha-skill": 0.85, "beta-skill": 0.1, "none": 0.05},
            {"alpha-skill": 0.9, "beta-skill": 0.2}))

        result = plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env))
        assert result == {"context": plugin.policy.render_advice_context("alpha-skill")}

        # Outbound state carries ONLY the current request — no history,
        # no sender id, no session ids (plan §3.2 input minimisation).
        calls = fake_client.created[0].calls
        assert calls[0]["state"] == {"request": "用户本轮请求：帮我生成一张图片"}
        assert calls[0]["timeout_ms"] > 0
        assert set(calls[0]["questions"]) == {"which", "any_match"}
        assert set(calls[1]["questions"]) == {"which", "fits::alpha-skill", "fits::beta-skill"}

        decisions = _decisions(_isolate_env)
        assert len(decisions) == 1
        record = decisions[0]
        assert record["status"] == "suggested"
        assert record["shortlist_or_skill"] == "alpha-skill"
        assert record["usage"]["input_tokens"] == 200  # phase-2 usage
        assert record["question_version"] == plugin.questions_mod.QUESTION_VERSION
        assert record["catalog_revision"].startswith("n2-")
        # No user text or skill bodies in the audit trail (plan §5).
        assert "帮我生成一张图片" not in json.dumps(record)
        assert "生成图片的技能" not in json.dumps(record)

    def test_same_turn_deduped(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch)
        kwargs = _turn_kwargs(plugin, _isolate_env)
        fake_client.append(_phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.05, "none": 0.05}, 0.8))
        fake_client.append(_phase_two_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.05, "none": 0.05},
            {"alpha-skill": 0.9, "beta-skill": 0.1}))
        assert plugin._on_pre_llm_call(**kwargs) is not None
        fake_client.append(_phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.05, "none": 0.05}, 0.8))
        assert plugin._on_pre_llm_call(**kwargs) is None  # duplicate_turn
        last = _decisions(_isolate_env)[-1]
        assert last["status"] == "skipped"
        assert last["reason_code"] == "duplicate_turn"

    def test_shadow_never_injects(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch, mode="shadow")
        fake_client.append(_phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.08, "none": 0.02}, 0.8))
        fake_client.append(_phase_two_answer(
            "alpha-skill", {"alpha-skill": 0.85, "beta-skill": 0.1, "none": 0.05},
            {"alpha-skill": 0.9, "beta-skill": 0.2}))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        assert _decisions(_isolate_env)[-1]["status"] == "suggested"

    def test_timeout_falls_back_with_record(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch)
        fake_client.append(plugin.client_mod.JevTimeoutError("deadline"))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        record = _decisions(_isolate_env)[-1]
        assert record["status"] == "unavailable"
        assert record["reason_code"] == "phase1_JevTimeoutError"

    def test_invalid_response_rejected_whole(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch)
        fake_client.append(_phase_one_answer(
            "ghost-skill", {"ghost-skill": 0.9, "none": 0.1}, 0.8))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        record = _decisions(_isolate_env)[-1]
        assert record["status"] == "unavailable"
        assert record["reason_code"].startswith("invalid_phase_one")

    def test_none_winner_abstains_after_phase2(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch)
        fake_client.append(_phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.08, "none": 0.02}, 0.8))
        fake_client.append(_phase_two_answer(
            "none", {"alpha-skill": 0.2, "beta-skill": 0.1, "none": 0.7},
            {"alpha-skill": 0.3, "beta-skill": 0.1}))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        record = _decisions(_isolate_env)[-1]
        assert record["status"] == "abstain"
        assert record["reason_code"] == "phase2_none"

    def test_low_any_match_abstains_sending_once(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch)
        fake_client.append(_phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.08, "none": 0.02}, 0.1))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        record = _decisions(_isolate_env)[-1]
        assert record["status"] == "abstain"
        assert record["reason_code"] == "any_match_below_threshold"
        assert len(fake_client.created[0].calls) == 1  # phase 2 never sent

    def test_explicit_skill_mention_skips(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch)
        result = plugin._on_pre_llm_call(**_turn_kwargs(
            plugin, _isolate_env, message="直接用 alpha-skill 处理"))
        assert result is None
        assert fake_client.created == []
        assert _decisions(_isolate_env)[-1]["reason_code"] == "explicit_skill_mention"

    def test_missing_api_key_skips(self, plugin, _isolate_env, fake_client):
        _make_skill(_isolate_env, "alpha-skill", "生成图片的技能。")
        _write_config(_isolate_env, _plugin_config())  # TYPESAFE_API_KEY unset
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        record = _decisions(_isolate_env)[-1]
        assert record["status"] == "skipped"
        assert record["reason_code"] == "missing_api_key"
        assert fake_client.created == []

    def test_budget_exhausted_skips_before_send(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch, daily_budget_usd=0.0000001)
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        record = _decisions(_isolate_env)[-1]
        assert record["reason_code"] == "budget_exhausted"
        assert fake_client.created == []

    def test_budget_ledger_written_on_success(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch, daily_budget_usd=1.0)
        fake_client.append(_phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.08, "none": 0.02}, 0.8))
        fake_client.append(_phase_two_answer(
            "alpha-skill", {"alpha-skill": 0.85, "beta-skill": 0.1, "none": 0.05},
            {"alpha-skill": 0.9, "beta-skill": 0.2}))
        plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env))
        usage_files = list((_isolate_env / "jev-skill-router" / "usage").glob("*.json"))
        assert usage_files, "daily usage ledger must be written"
        state = json.loads(usage_files[0].read_text())
        assert state["requests"] == 2
        assert state["spent_usd"] > 0
        assert state["reserved_usd"] == 0.0  # fully settled

    def test_platform_not_allowed_skips(self, plugin, _isolate_env, fake_client):
        _make_skill(_isolate_env, "alpha-skill", "生成图片的技能。")
        _write_config(_isolate_env, _plugin_config())
        assert plugin._on_pre_llm_call(**_turn_kwargs(
            plugin, _isolate_env, platform="wechat")) is None
        assert _decisions(_isolate_env)[-1]["reason_code"] == "platform_not_allowed"

    def test_cron_surface_skips(self, plugin, _isolate_env, monkeypatch, fake_client):
        _make_skill(_isolate_env, "alpha-skill", "生成图片的技能。")
        _write_config(_isolate_env, _plugin_config())
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        assert _decisions(_isolate_env)[-1]["reason_code"] == "non_interactive_surface"

    def test_outbound_subset_limits_offered_ids(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch,
                            outbound_catalog=["beta-skill"])
        fake_client.append(_phase_one_answer(
            "beta-skill", {"beta-skill": 0.9, "none": 0.1}, 0.8))
        fake_client.append(_phase_two_answer(
            "beta-skill", {"beta-skill": 0.9, "none": 0.1}, {"beta-skill": 0.9}))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is not None
        criteria = fake_client.created[0].calls[0]["questions"]["which"]["criteria"]
        assert set(criteria) == {"beta-skill", "none"}  # alpha never offered

    def test_stale_candidate_not_injected(self, plugin, _isolate_env, monkeypatch, fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch)
        # Winner goes stale between catalog compile and injection.
        monkeypatch.setattr(plugin.catalog_mod, "entry_still_available", lambda entry: False)
        fake_client.append(_phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.08, "none": 0.02}, 0.8))
        fake_client.append(_phase_two_answer(
            "alpha-skill", {"alpha-skill": 0.85, "beta-skill": 0.1, "none": 0.05},
            {"alpha-skill": 0.9, "beta-skill": 0.2}))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        record = _decisions(_isolate_env)[-1]
        assert record["status"] == "skipped"
        assert record["reason_code"] == "stale_candidate"

    def test_unexpected_exception_falls_back_silently(self, plugin, _isolate_env, monkeypatch,
                                                      fake_client):
        self._recommend_env(plugin, _isolate_env, monkeypatch)

        def boom(**kwargs):
            raise RuntimeError("unexpected orchestrator bug")

        monkeypatch.setattr(plugin, "_route_turn", boom)
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None

    # -- 返修 #1: per-request reserve/settle; phase-2 budget short -> no send --

    def test_phase2_budget_short_not_sent(self, plugin, _isolate_env, monkeypatch, fake_client):
        """Phase 2 must reserve ITS OWN budget slice before sending; when
        the daily cap cannot cover a second reservation, no second request
        goes out and no suggestion is produced."""
        self._recommend_env(plugin, _isolate_env, monkeypatch, daily_budget_usd=0.045,
                            max_input_tokens=2_000_000)
        monkeypatch.setattr(plugin.policy, "conservative_token_estimate",
                            lambda *texts: 1_000_000)  # flat estimate -> $0.042/request
        # Phase-1 response books $0.0042 of real usage (100k input tokens),
        # leaving 0.045 - 0.0042 = $0.0408 — short of phase 2's $0.042.
        phase1 = _phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.08, "none": 0.02}, 0.8)
        phase1["usage"] = {"input_tokens": 100_000, "output_tokens": 20}
        fake_client.append(phase1)
        fake_client.append(_phase_two_answer(  # must NEVER be consumed
            "alpha-skill", {"alpha-skill": 0.85, "beta-skill": 0.1, "none": 0.05},
            {"alpha-skill": 0.9, "beta-skill": 0.2}))

        result = plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env))
        assert result is None  # no suggestion without the phase-2 verification
        assert len(fake_client.created[0].calls) == 1  # phase 2 never sent
        record = _decisions(_isolate_env)[-1]
        assert record["status"] == "skipped"
        assert record["reason_code"] == "phase2_budget_exhausted"
        # Phase-1 spend was still settled honestly (not the whole estimate).
        state = json.loads(
            list((_isolate_env / "jev-skill-router" / "usage").glob("*.json"))[0].read_text())
        assert state["requests"] == 1
        assert state["spent_usd"] == pytest.approx(100_000 / 1e6 * 0.042)
        assert state["reserved_usd"] == 0.0

    def test_failed_request_settles_conservatively(self, plugin, _isolate_env, monkeypatch,
                                                   fake_client):
        """A failed request must NOT release its reservation as $0 — the
        estimate stays booked until actual cost is provable."""
        self._recommend_env(plugin, _isolate_env, monkeypatch, daily_budget_usd=1.0,
                            max_input_tokens=2_000_000)
        monkeypatch.setattr(plugin.policy, "conservative_token_estimate",
                            lambda *texts: 1_000_000)
        fake_client.append(plugin.client_mod.JevTimeoutError("deadline"))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        state = json.loads(
            list((_isolate_env / "jev-skill-router" / "usage").glob("*.json"))[0].read_text())
        assert state["spent_usd"] == pytest.approx(1_000_000 / 1e6 * 0.042)
        assert state["reserved_usd"] == 0.0

    # -- 返修 #3: single-request mode must never emit a recommendation --

    def test_single_request_mode_skips_entirely(self, plugin, _isolate_env, monkeypatch,
                                                fake_client):
        """v1 has no verified single-phase decision path: with
        max_requests_per_turn < 2 the turn is skipped BEFORE any spend."""
        self._recommend_env(plugin, _isolate_env, monkeypatch, max_requests_per_turn=1)
        fake_client.append(_phase_one_answer(  # must never be consumed
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.08, "none": 0.02}, 0.8))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        assert fake_client.created == []
        assert len(fake_client) == 1  # scripted response untouched
        record = _decisions(_isolate_env)[-1]
        assert record["status"] == "skipped"
        assert record["reason_code"] == "single_request_mode"


# ---------------------------------------------------------------------------
# 返修 #4: Choice distribution validation (questions) + usage guards (client)
# ---------------------------------------------------------------------------

class TestChoiceDistribution:
    """A Choice answer is only trustworthy as a WHOLE distribution."""

    def _phase_one(self, plugin, choice, probs, offered):
        answers = {
            "which": {"type": "choice", "choice": choice, "probabilities": probs},
            "any_match": {"type": "noul", "noul": 0.5},
        }
        return plugin.questions_mod.interpret_phase_one(answers, offered)

    def test_missing_offered_option_rejected(self, plugin):
        # offered a,b but probabilities only cover a + none (b missing)
        with pytest.raises(plugin.questions_mod.InvalidAnswersError):
            self._phase_one(plugin, "a", {"a": 0.6, "none": 0.4}, ["a", "b"])

    def test_sum_above_one_rejected(self, plugin):
        with pytest.raises(plugin.questions_mod.InvalidAnswersError):
            self._phase_one(plugin, "a", {"a": 0.9, "b": 0.8, "none": 0.7}, ["a", "b"])

    def test_sum_below_one_rejected(self, plugin):
        with pytest.raises(plugin.questions_mod.InvalidAnswersError):
            self._phase_one(plugin, "a", {"a": 0.2, "b": 0.2, "none": 0.1}, ["a", "b"])

    def test_choice_not_argmax_rejected(self, plugin):
        # choice "a" at 0.3 while "b" holds 0.6 — inconsistent answer
        with pytest.raises(plugin.questions_mod.InvalidAnswersError):
            self._phase_one(plugin, "a", {"a": 0.3, "b": 0.6, "none": 0.1}, ["a", "b"])

    def test_phase2_winner_not_argmax_rejected(self, plugin):
        answers = _phase_two_answer("none", {"none": 0.2, "s": 0.8}, {"s": 0.9})
        with pytest.raises(plugin.questions_mod.InvalidAnswersError):
            plugin.questions_mod.interpret_phase_two(answers["answers"], ["s"])

    def test_argmax_tie_accepted(self, plugin):
        top, prob, _, _ = self._phase_one(
            plugin, "a", {"a": 0.5, "b": 0.5, "none": 0.0}, ["a", "b"])
        assert top == "a"
        assert prob == pytest.approx(0.5)

    def test_client_rejects_invalid_usage(self, plugin):
        for bad_usage in ({"input_tokens": -5, "output_tokens": 0},
                          {"input_tokens": "many", "output_tokens": 0},
                          {"input_tokens": 10}):
            body = {"answers": {"q": {"type": "noul", "noul": 0.5}}, "usage": bad_usage}

            def transport(url, headers, payload, timeout_s, body=body):
                return 200, body, json.dumps(body)

            with pytest.raises(plugin.client_mod.JevInvalidResponseError):
                plugin.client_mod.JevClient(
                    "k", "m", base_url="https://u.t", transport=transport
                ).ask({}, {"q": {}}, 100)

    def test_client_allows_missing_usage(self, plugin):
        """No usage block is structurally fine — the LEDGER then charges
        the conservative estimate (see TestBudgetAtomicity)."""
        body = {"answers": {"q": {"type": "noul", "noul": 0.5}}}

        def transport(url, headers, payload, timeout_s):
            return 200, body, json.dumps(body)

        result = plugin.client_mod.JevClient(
            "k", "m", base_url="https://u.t", transport=transport
        ).ask({}, {"q": {}}, 100)
        assert "usage" not in result


# ---------------------------------------------------------------------------
# 返修 #2: budget ledger atomicity, fail-closed behaviour, conservative settle
# ---------------------------------------------------------------------------

class TestBudgetAtomicity:
    EST_USD = 0.1
    CAP_USD = 1.0  # exactly 10 reservations of EST_USD

    def test_concurrent_reserves_never_exceed_cap(self, plugin, tmp_path):
        """Separate ledger instances (one per concurrent turn) must still
        serialise on the PROFILE's day file — the cap bounds successes."""
        import threading

        barrier = threading.Barrier(24)
        outcomes: list = []

        def worker():
            ledger = plugin.policy.BudgetLedger(tmp_path, self.CAP_USD)
            barrier.wait()
            outcomes.append(ledger.reserve(self.EST_USD))

        threads = [threading.Thread(target=worker) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(outcomes) == 10  # exactly the cap; never more

    def test_concurrent_reserve_settle_consistent(self, plugin, tmp_path):
        """Lost updates across concurrent turns would under-count spend."""
        import threading

        barrier = threading.Barrier(16)
        usage = {"input_tokens": 1000, "output_tokens": 0}

        def worker():
            ledger = plugin.policy.BudgetLedger(tmp_path, None)
            barrier.wait()
            if ledger.reserve(0.05):
                ledger.settle(0.05, usage, 0.042, 0.0)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        state = plugin.policy.BudgetLedger(tmp_path, None).snapshot()
        assert state["requests"] == 16  # every settle must be recorded
        assert state["spent_usd"] == pytest.approx(16 * (1000 / 1e6 * 0.042))
        assert state["reserved_usd"] == 0.0

    def test_corrupt_ledger_fails_closed(self, plugin, tmp_path):
        import time as _time

        usage_dir = tmp_path / "jev-skill-router" / "usage"
        usage_dir.mkdir(parents=True)
        (usage_dir / f"{_time.strftime('%Y-%m-%d')}.json").write_text("{corrupt!!!")
        ledger = plugin.policy.BudgetLedger(tmp_path, 100.0)
        assert ledger.reserve(0.01) is False  # unreadable state -> refuse to spend

    def test_unwritable_ledger_fails_closed(self, plugin, tmp_path):
        import os as _os

        usage_dir = tmp_path / "jev-skill-router" / "usage"
        usage_dir.mkdir(parents=True)
        usage_dir.chmod(0o500)
        try:
            ledger = plugin.policy.BudgetLedger(tmp_path, 100.0)
            assert ledger.reserve(0.01) is False  # cannot persist -> refuse
        finally:
            usage_dir.chmod(0o700)

    def test_settle_without_usage_charges_estimate(self, plugin, tmp_path):
        """Missing usage must not settle to $0 — the reservation is the
        conservative upper bound until real usage is provable."""
        ledger = plugin.policy.BudgetLedger(tmp_path, 1.0)
        assert ledger.reserve(0.01) is True
        charged = ledger.settle(0.01, None, 0.042, 0.0)
        assert charged == pytest.approx(0.01)
        state = ledger.snapshot()
        assert state["spent_usd"] == pytest.approx(0.01)
        assert state["reserved_usd"] == 0.0

    def test_settle_with_invalid_usage_charges_estimate(self, plugin, tmp_path):
        ledger = plugin.policy.BudgetLedger(tmp_path, 1.0)
        assert ledger.reserve(0.01) is True
        charged = ledger.settle(0.01, {"input_tokens": "oops"}, 0.042, 0.0)
        assert charged == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# Real plugin-discovery path (PluginManager) — registration & hook invocation
# ---------------------------------------------------------------------------

class TestPluginDiscoveryRealPath:
    def test_loads_registers_and_invokes_via_plugin_manager(self, plugin, _isolate_env,
                                                            monkeypatch):
        """Enable the plugin in config.yaml and drive the REAL discovery +
        hook-invocation path used by the host."""
        import yaml

        config = {"plugins": {"enabled": ["jev-skill-router"]}}
        (_isolate_env / "config.yaml").write_text(yaml.safe_dump(config))

        # Wipe cached plugin state so this worker rediscovers from disk.
        for key in list(sys.modules):
            if key.startswith(("hermes_plugins", "hermes_cli.plugins")):
                del sys.modules[key]

        from hermes_cli.plugins import _ensure_plugins_discovered

        mgr = _ensure_plugins_discovered(force=True)
        assert "jev-skill-router" in set(mgr._plugins.keys())
        assert "pre_llm_call" in mgr._hooks
        assert "on_session_end" in mgr._hooks

        # Default mode off: the real hook call must produce NOTHING —
        # no context, no decision record, no outbound request.
        from hermes_cli.lifecycle import invoke_hook

        results = invoke_hook(
            "pre_llm_call",
            session_id="s", task_id="t", turn_id="u", user_message="hi",
            conversation_history=[], is_first_turn=True, model="m",
            platform="telegram", parent_session_id="", sender_id="user",
        )
        assert all(r is None or r == {} for r in results)
        assert not (_isolate_env / "jev-skill-router" / "decisions.jsonl").exists()
        assert not (_isolate_env / "jev-skill-router" / "usage").exists()


# ---------------------------------------------------------------------------
# 边界返修 #1: 启用必填项（plan §4「缺失则不启用」）— fail closed
# ---------------------------------------------------------------------------

_REQUIRED_FOR_ENABLE = {
    "mode": "shadow",
    "max_input_tokens": 20000,
    "daily_budget_usd": 0.5,
}


class TestEnablementGuard:
    def test_shadow_without_required_fields_stays_off(self, plugin):
        cfg, errors = plugin.policy.config_from_mapping({"mode": "shadow"})
        assert cfg.mode == "off"
        assert cfg.enabled is False
        assert any("max_input_tokens" in e for e in errors)
        assert any("daily_budget_usd" in e for e in errors)

    def test_recommend_missing_daily_budget_stays_off(self, plugin):
        cfg, errors = plugin.policy.config_from_mapping(
            {"mode": "recommend", "max_input_tokens": 20000})
        assert cfg.mode == "off"
        assert cfg.enabled is False
        assert any("daily_budget_usd" in e for e in errors)

    @pytest.mark.parametrize("bad", [float("inf"), float("nan")])
    def test_non_finite_daily_budget_stays_off(self, plugin, bad):
        cfg, errors = plugin.policy.config_from_mapping(
            {**_REQUIRED_FOR_ENABLE, "daily_budget_usd": bad})
        assert cfg.mode == "off"
        assert cfg.enabled is False
        assert any("daily_budget_usd" in e for e in errors)

    def test_off_mode_needs_nothing(self, plugin):
        """Default off stays off with zero config and zero errors."""
        cfg, errors = plugin.policy.config_from_mapping({})
        assert cfg.mode == "off"
        assert cfg.enabled is False
        assert errors == []

    def test_fully_configured_shadow_enables(self, plugin):
        cfg, errors = plugin.policy.config_from_mapping(
            {**_REQUIRED_FOR_ENABLE, "outbound_catalog_full": True})
        assert cfg.enabled is True
        assert errors == []


# ---------------------------------------------------------------------------
# 边界返修 #2: 目录外发范围必须显式声明（完整目录布尔或精确 ID 列表）
# ---------------------------------------------------------------------------

class TestOutboundDeclaration:
    def _cfg(self, plugin, **extra):
        return plugin.policy.config_from_mapping({**_REQUIRED_FOR_ENABLE, **extra})

    def test_undeclared_catalog_stays_off(self, plugin):
        cfg, errors = self._cfg(plugin)
        assert cfg.mode == "off"
        assert cfg.enabled is False
        assert any("outbound" in e for e in errors)

    def test_explicit_full_catalog_enables(self, plugin):
        cfg, errors = self._cfg(plugin, outbound_catalog_full=True)
        assert cfg.enabled is True
        assert cfg.outbound_catalog is None  # full catalog, no subset
        assert cfg.outbound_catalog_full is True
        assert errors == []

    def test_explicit_id_list_enables_subset(self, plugin):
        cfg, errors = self._cfg(plugin, outbound_catalog=["alpha-skill"])
        assert cfg.enabled is True
        assert cfg.outbound_catalog == frozenset({"alpha-skill"})
        assert cfg.outbound_catalog_full is False
        assert errors == []

    def test_full_bool_and_id_list_conflict_rejected(self, plugin):
        cfg, errors = self._cfg(plugin, outbound_catalog_full=True,
                                outbound_catalog=["alpha-skill"])
        assert cfg.mode == "off"
        assert cfg.enabled is False
        assert any("outbound" in e for e in errors)

    @pytest.mark.parametrize("bad", ["yes", 1, "true"])
    def test_full_bool_must_be_bool(self, plugin, bad):
        cfg, errors = self._cfg(plugin, outbound_catalog_full=bad)
        assert cfg.mode == "off"
        assert cfg.enabled is False
        assert any("outbound_catalog_full" in e for e in errors)

    def test_undeclared_catalog_sends_nothing(self, plugin, _isolate_env, monkeypatch,
                                              fake_client):
        """End-to-end: every required number set but NO outbound
        declaration → not enabled → zero requests, zero records."""
        monkeypatch.setenv("TYPESAFE_API_KEY", "k-test")
        _make_skill(_isolate_env, "alpha-skill", "生成图片的技能。")
        _write_config(_isolate_env, {
            "plugins": {"entries": {"jev-skill-router": dict(_REQUIRED_FOR_ENABLE)}}})
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        assert fake_client.created == []
        assert _decisions(_isolate_env) == []


# ---------------------------------------------------------------------------
# 边界返修 #3: 预留估算 = 完整请求体 UTF-8 字节数 + 固定协议余量（硬上界）
# ---------------------------------------------------------------------------

class TestByteBasedEstimate:
    def test_pure_ascii_bound(self, plugin):
        text = "a" * 1000  # 1000 bytes, ~250 real tokens — bound must hold
        est = plugin.policy.conservative_token_estimate(text)
        assert est == 1000 + plugin.policy.PROTOCOL_OVERHEAD_TOKENS
        assert est >= len(text.encode("utf-8"))

    def test_heavy_cjk_bound(self, plugin):
        text = "漢字" * 500  # 1000 chars -> 3000 UTF-8 bytes
        est = plugin.policy.conservative_token_estimate(text)
        assert est == 3000 + plugin.policy.PROTOCOL_OVERHEAD_TOKENS
        assert est >= len(text.encode("utf-8"))

    def test_mixed_payloads_summed(self, plugin):
        est = plugin.policy.conservative_token_estimate("a" * 100, "漢" * 50)
        assert est == (100 + 150) + plugin.policy.PROTOCOL_OVERHEAD_TOKENS

    def test_empty_payload_still_reserves_margin(self, plugin):
        assert plugin.policy.conservative_token_estimate("") \
            == plugin.policy.PROTOCOL_OVERHEAD_TOKENS

    def test_estimate_tracks_bytes_not_language_guesses(self, plugin):
        # Same byte length, same reservation — no per-language heuristic
        # that could undershoot real tokenisation (code, ids, rare script).
        assert plugin.policy.conservative_token_estimate("字" * 100) \
            == plugin.policy.conservative_token_estimate("a" * 300)


class TestInputLimitBoundary:
    """At exactly max_input_tokens the request goes out; one token over
    the estimate refuses BEFORE any spend."""

    def _env(self, plugin, home, monkeypatch, limit):
        monkeypatch.setenv("TYPESAFE_API_KEY", "k-test")
        _make_skill(home, "alpha-skill", "生成图片的技能。")
        _make_skill(home, "beta-skill", "翻译文本的技能。")
        _write_config(home, _plugin_config(max_input_tokens=limit,
                                           daily_budget_usd=1.0))
        monkeypatch.setattr(plugin.policy, "conservative_token_estimate",
                            lambda *texts: 1000)

    def test_at_exact_limit_request_sent(self, plugin, _isolate_env, monkeypatch,
                                         fake_client):
        self._env(plugin, _isolate_env, monkeypatch, limit=1000)
        fake_client.append(_phase_one_answer(
            "alpha-skill", {"alpha-skill": 0.9, "beta-skill": 0.08, "none": 0.02}, 0.1))
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        assert len(fake_client.created[0].calls) == 1  # any_match 0.1 -> abstain
        assert _decisions(_isolate_env)[-1]["reason_code"] == "any_match_below_threshold"

    def test_one_over_limit_refused_before_send(self, plugin, _isolate_env, monkeypatch,
                                                fake_client):
        self._env(plugin, _isolate_env, monkeypatch, limit=999)
        assert plugin._on_pre_llm_call(**_turn_kwargs(plugin, _isolate_env)) is None
        assert fake_client.created == []
        record = _decisions(_isolate_env)[-1]
        assert record["status"] == "skipped"
        assert record["reason_code"] == "input_too_large"


# ---------------------------------------------------------------------------
# 边界返修 #4: manifest 不得误署他人
# ---------------------------------------------------------------------------

class TestPluginManifest:
    def test_manifest_author_not_misattributed(self):
        import yaml

        manifest = (_repo_root() / "plugins" / "jev-skill-router" / "plugin.yaml")
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        author = str(data.get("author", ""))
        assert "NousResearch" not in author
