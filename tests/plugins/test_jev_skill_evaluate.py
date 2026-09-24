"""Offline tests for the jev-skill-router evaluation harness (plan §6-§7, D2).

The runner accepts clearly-labeled Chinese case JSON held OUTSIDE the repo
(real D2); these tests use the small synthetic fixture set in
``tests/plugins/jev_eval_fixtures/`` to verify the harness itself:

  * case loading + integrity (splits, origins, rewrite-group containment,
    gold-id membership in the frozen catalog);
  * outcome classification for every expectation kind × outcome pair;
  * aggregation with denominators preserved (failures/skips never dropped);
  * arm A ("existing agent") reported as UNVERIFIED, never simulated;
  * arm B keyword baseline (deterministic, mention-gated);
  * arm C drives the REAL production routing pipeline with a scripted
    responder injected at the client seam — no network, ever;
  * live responder is a separate explicit entry and refuses to run
    without acknowledgement + credentials (plan §6: 真实 API 评测为单独
    显式入口，不混入默认单元测试).
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
    for var in ("TYPESAFE_API_KEY", "HERMES_SESSION_SOURCE", "HERMES_CRON_SESSION",
                "HERMES_SESSION_PLATFORM", "HERMES_PLATFORM", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    yield hermes_home
    from agent.skill_utils import _external_dirs_cache_clear

    _external_dirs_cache_clear()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


FIXTURES = Path(__file__).parent / "jev_eval_fixtures"
CASES_PATH = FIXTURES / "cases.json"
RESPONSES_PATH = FIXTURES / "responses.json"
SKILLS_DIR = FIXTURES / "skills"


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


@pytest.fixture(scope="module")
def evaluate(plugin):
    return importlib.import_module("hermes_plugins.jev_skill_router.evaluate")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _case(evaluate, kind, gold="", forbidden=(), split="dev", case_id="c1",
          group_id="g1", origin="synthetic"):
    return evaluate.EvalCase(
        case_id=case_id, group_id=group_id, split=split, origin=origin,
        user_message="样本请求", kind=kind, gold_skill_id=gold,
        forbidden_skill_ids=frozenset(forbidden),
    )


def _record(status, reason="", suggested=""):
    return {"status": status, "reason_code": reason, "shortlist_or_skill": suggested}


def _write_cases(path: Path, cases: list, schema="jev-skill-eval-cases-v1") -> None:
    payload = {"schema_version": schema, "cases": cases}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _entry(evaluate, case_dict):
    return {"case_id": case_dict["case_id"], "group_id": "g1", "split": "dev",
            "origin": "synthetic", "user_message": "样本请求",
            "expectation": case_dict["expectation"]}


# ---------------------------------------------------------------------------
# Case loading & integrity
# ---------------------------------------------------------------------------

class TestCaseLoading:
    def test_fixture_set_loads(self, evaluate):
        cases = evaluate.load_cases(CASES_PATH)
        assert len(cases) == 12
        assert all(c.origin == "synthetic" for c in cases)
        by_split = {}
        for c in cases:
            by_split.setdefault(c.split, []).append(c)
        assert {k: len(v) for k, v in by_split.items()} == {
            "dev": 6, "threshold_validation": 2, "final_test": 4}

    def test_duplicate_case_id_rejected(self, evaluate, tmp_path):
        path = tmp_path / "cases.json"
        _write_cases(path, [_entry(evaluate, {"case_id": "a", "expectation": {"kind": "no_skill"}}),
                            _entry(evaluate, {"case_id": "a", "expectation": {"kind": "no_skill"}})])
        with pytest.raises(evaluate.EvalInputError, match="duplicate"):
            evaluate.load_cases(path)

    def test_unknown_kind_rejected(self, evaluate, tmp_path):
        path = tmp_path / "cases.json"
        _write_cases(path, [_entry(evaluate, {"case_id": "a", "expectation": {"kind": "maybe"}})])
        with pytest.raises(evaluate.EvalInputError, match="kind"):
            evaluate.load_cases(path)

    def test_recommend_without_gold_rejected(self, evaluate, tmp_path):
        path = tmp_path / "cases.json"
        _write_cases(path, [_entry(evaluate, {"case_id": "a", "expectation": {"kind": "recommend"}})])
        with pytest.raises(evaluate.EvalInputError, match="skill_id"):
            evaluate.load_cases(path)

    def test_no_skill_with_gold_rejected(self, evaluate, tmp_path):
        path = tmp_path / "cases.json"
        _write_cases(path, [_entry(evaluate, {"case_id": "a",
                                              "expectation": {"kind": "no_skill",
                                                              "skill_id": "x"}})])
        with pytest.raises(evaluate.EvalInputError, match="skill_id"):
            evaluate.load_cases(path)

    def test_group_spanning_splits_rejected(self, evaluate, tmp_path):
        path = tmp_path / "cases.json"
        _write_cases(path, [
            _entry(evaluate, {"case_id": "a", "expectation": {"kind": "no_skill"}}),
            {**_entry(evaluate, {"case_id": "b", "expectation": {"kind": "no_skill"}}),
             "split": "final_test"},
        ])
        with pytest.raises(evaluate.EvalInputError, match="g1"):
            evaluate.load_cases(path)

    def test_bad_split_and_origin_rejected(self, evaluate, tmp_path):
        path = tmp_path / "cases.json"
        _write_cases(path, [{**_entry(evaluate, {"case_id": "a",
                                                 "expectation": {"kind": "no_skill"}}),
                             "split": "prod"}])
        with pytest.raises(evaluate.EvalInputError, match="split"):
            evaluate.load_cases(path)
        path = tmp_path / "cases2.json"
        _write_cases(path, [{**_entry(evaluate, {"case_id": "a",
                                                 "expectation": {"kind": "no_skill"}}),
                             "origin": "wild"}])
        with pytest.raises(evaluate.EvalInputError, match="origin"):
            evaluate.load_cases(path)

    def test_responses_unknown_case_rejected(self, evaluate, tmp_path):
        cases = evaluate.load_cases(CASES_PATH)
        known = {c.case_id for c in cases}
        path = tmp_path / "responses.json"
        path.write_text(json.dumps({
            "schema_version": "jev-skill-eval-responses-v1",
            "responses": {
                "syn-dev-001": {"phases": []},
                "ghost-case": {"phases": []},
            }}), encoding="utf-8")
        with pytest.raises(evaluate.EvalInputError, match="ghost-case"):
            evaluate.load_responses(path, known)

    def test_responses_bad_error_name_rejected(self, evaluate, tmp_path):
        path = tmp_path / "responses.json"
        path.write_text(json.dumps({
            "schema_version": "jev-skill-eval-responses-v1",
            "responses": {"c1": {"phases": [{"error": "ValueError"}]}}}), encoding="utf-8")
        with pytest.raises(evaluate.EvalInputError, match="JevError"):
            evaluate.load_responses(path, {"c1"})

    def test_gold_id_must_exist_in_catalog(self, evaluate, tmp_path):
        path = tmp_path / "cases.json"
        _write_cases(path, [_entry(evaluate, {"case_id": "a", "expectation":
                                              {"kind": "recommend", "skill_id": "ghost-skill"}})])
        responses = tmp_path / "responses.json"
        responses.write_text(json.dumps({
            "schema_version": "jev-skill-eval-responses-v1", "responses": {}}),
            encoding="utf-8")
        with pytest.raises(evaluate.EvalInputError, match="ghost-skill"):
            evaluate.run_evaluation(
                cases_path=path, skills_dir=SKILLS_DIR, out_dir=tmp_path / "out",
                responses_path=responses)


# ---------------------------------------------------------------------------
# Classification matrix
# ---------------------------------------------------------------------------

class TestClassify:
    def test_recommend_matrix(self, evaluate):
        c = _case(evaluate, "recommend", gold="gold-skill")
        assert evaluate.classify_outcome(c, _record("suggested", "two_phase_confirmed", "gold-skill")) == "correct"
        assert evaluate.classify_outcome(c, _record("suggested", "two_phase_confirmed", "other")) == "wrong_load"
        assert evaluate.classify_outcome(c, _record("abstain", "phase2_none")) == "missed"
        assert evaluate.classify_outcome(c, _record("unavailable", "phase1_JevTimeoutError")) == "unavailable_failure"
        assert evaluate.classify_outcome(c, _record("skipped", "budget_exhausted")) == "infra_skipped"

    def test_no_skill_and_unsupported_matrix(self, evaluate):
        for kind, bad_label in (("no_skill", "extra_load"), ("unsupported", "wrong_load")):
            c = _case(evaluate, kind)
            assert evaluate.classify_outcome(c, _record("suggested", "r", "any")) == bad_label
            assert evaluate.classify_outcome(c, _record("abstain", "phase1_none")) == "correct"
            assert evaluate.classify_outcome(c, _record("unavailable", "phase1_JevAuthError")) == "unavailable_failure"
            assert evaluate.classify_outcome(c, _record("skipped", "input_too_large")) == "infra_skipped"

    def test_explicit_skill_matrix(self, evaluate):
        c = _case(evaluate, "explicit_skill", gold="gold-skill")
        assert evaluate.classify_outcome(c, _record("skipped", "explicit_skill_mention")) == "correct"
        assert evaluate.classify_outcome(c, _record("suggested", "r", "gold-skill")) == "extra_load"
        assert evaluate.classify_outcome(c, _record("skipped", "missing_api_key")) == "infra_skipped"
        assert evaluate.classify_outcome(c, _record("unavailable", "invalid_phase_one:x")) == "unavailable_failure"

    def test_boundary_violation_outranks_everything(self, evaluate):
        c = _case(evaluate, "recommend", gold="gold-skill", forbidden={"profile-x-secret"})
        assert evaluate.classify_outcome(
            c, _record("suggested", "two_phase_confirmed", "profile-x-secret")) == "boundary_violation"

    def test_runner_error_label(self, evaluate):
        c = _case(evaluate, "recommend", gold="gold-skill")
        assert evaluate.classify_outcome(
            c, {"status": "runner_error", "reason_code": "RuntimeError: boom"}) == "runner_error"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_wilson_interval(self, evaluate):
        assert evaluate.wilson_interval(0, 0) == (None, None)
        lo, hi = evaluate.wilson_interval(5, 10)
        assert lo < 0.5 < hi
        assert 0.0 <= lo <= hi <= 1.0
        assert evaluate.wilson_interval(10, 10)[1] == pytest.approx(1.0)

    def test_aggregate_preserves_denominator(self, evaluate):
        rows = [
            {"label": "correct", "status": "suggested", "elapsed_ms": 100.0, "calls": 2,
             "input_tokens": 1100, "output_tokens": 50, "cost_usd": 0.00005},
            {"label": "missed", "status": "abstain", "elapsed_ms": 80.0, "calls": 1,
             "input_tokens": 800, "output_tokens": 30, "cost_usd": 0.00004},
            {"label": "unavailable_failure", "status": "unavailable", "elapsed_ms": 2000.0,
             "calls": 1, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.00004},
            {"label": "infra_skipped", "status": "skipped", "elapsed_ms": 0.0, "calls": 0,
             "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
        ]
        agg = evaluate.aggregate_rows(rows)
        assert agg["denominator"] == 4
        assert sum(agg["counts"].values()) == 4  # nobody dropped
        assert agg["counts"]["correct"] == 1
        assert agg["accuracy"] == pytest.approx(0.25)
        assert agg["suggested"] == 1 and agg["abstain"] == 1
        assert agg["calls_total"] == 4
        assert agg["usage"]["input_tokens_total"] == 1900
        assert agg["cost_usd_total"] == pytest.approx(0.00013)
        assert agg["latency_ms"]["p95"] == 2000.0  # nearest-rank over all cases
        assert agg["latency_ms_sendpath"]["p95"] == 2000.0

    def test_aggregate_zero_group(self, evaluate):
        agg = evaluate.aggregate_rows([])
        assert agg["denominator"] == 0
        assert agg["accuracy"] is None

    def test_percentile_nearest_rank(self, evaluate):
        values = [10, 20, 30, 40]
        assert evaluate._percentile(values, 50) == 20
        assert evaluate._percentile(values, 95) == 40
        assert evaluate._percentile([], 50) is None


# ---------------------------------------------------------------------------
# Arm B: keyword baseline
# ---------------------------------------------------------------------------

class TestKeywordBaseline:
    def _install_fixture_skills(self, home):
        import shutil

        for src in sorted(SKILLS_DIR.iterdir()):
            shutil.copytree(src, home / "skills" / src.name, dirs_exist_ok=True)

    def test_explicit_mention_gates_to_no_suggestion(self, evaluate, _isolate_env):
        self._install_fixture_skills(_isolate_env)
        entries = evaluate.catalog_mod.compile_catalog(None).entries
        msg = "直接用 syn-translator 把这句话翻成英文"
        assert evaluate.keyword_baseline_suggestion(msg, entries) == ""

    def test_suggests_on_bigram_overlap(self, evaluate, _isolate_env):
        self._install_fixture_skills(_isolate_env)
        entries = evaluate.catalog_mod.compile_catalog(None).entries
        assert evaluate.keyword_baseline_suggestion(
            "帮我翻译这段中文成英文", entries) == "syn-translator"
        # One shared bigram (风景) stays below min_score — no suggestion.
        assert evaluate.keyword_baseline_suggestion(
            "帮我画一张日落风景图", entries) == ""

    def test_min_score_threshold(self, evaluate, _isolate_env):
        self._install_fixture_skills(_isolate_env)
        entries = evaluate.catalog_mod.compile_catalog(None).entries
        assert evaluate.keyword_baseline_suggestion(
            "帮我翻译这段中文成英文", entries, min_score=99) == ""

    def test_tie_broken_deterministically(self, evaluate):
        def entry(skill_id):
            return evaluate.catalog_mod.CatalogEntry(
                skill_id=skill_id, name=skill_id, description="翻译英文内容",
                category=None, skill_md=Path(f"/tmp/{skill_id}/SKILL.md"))

        entries = [entry("b-skill"), entry("a-skill")]
        assert evaluate.keyword_baseline_suggestion(
            "帮我翻译英文", entries) == "a-skill"


# ---------------------------------------------------------------------------
# Runner end-to-end (scripted) — real pipeline, injected responder
# ---------------------------------------------------------------------------

class TestRunnerScriptedE2E:
    def _run(self, evaluate, tmp_path, **kw):
        return evaluate.run_evaluation(
            cases_path=CASES_PATH, skills_dir=SKILLS_DIR,
            out_dir=tmp_path / "out", responses_path=RESPONSES_PATH, **kw)

    def test_report_shape_and_arms(self, evaluate, tmp_path):
        report = self._run(evaluate, tmp_path)
        assert report["schema_version"] == "jev-skill-eval-report-v1"
        run = report["run"]
        assert run["responder"] == "scripted"
        assert run["latency_valid"] is False  # fixture timing says nothing
        assert run["catalog_revision"].startswith("n4-")
        assert run["inputs"][0]["sha256"]
        arm_a = report["arms"]["existing_agent"]
        assert arm_a["status"] == "unverified"
        assert "reason" in arm_a
        assert report["arms"]["jev_router"]["status"] == "computed"
        assert report["arms"]["keyword_baseline"]["status"] == "computed"

    def test_jev_arm_exact_counts_per_split(self, evaluate, tmp_path):
        report = self._run(evaluate, tmp_path)
        by = report["arms"]["jev_router"]["by_split"]
        dev = by["dev"]["synthetic"]
        assert dev["denominator"] == 6
        assert dev["counts"] == {"correct": 4, "missed": 1, "unavailable_failure": 1}
        assert dev["accuracy"] == pytest.approx(4 / 6)
        assert dev["suggested"] == 1 and dev["abstain"] == 3
        assert by["threshold_validation"]["synthetic"]["counts"] == {"correct": 2}
        assert by["final_test"]["synthetic"]["counts"] == {"correct": 4}
        # natural segment present but empty — never merged with synthetic
        assert by["dev"]["natural"]["denominator"] == 0

    def test_keyword_arm_denominators_hold(self, evaluate, tmp_path):
        report = self._run(evaluate, tmp_path)
        by = report["arms"]["keyword_baseline"]["by_split"]
        for split in ("dev", "threshold_validation", "final_test"):
            agg = by[split]["synthetic"]
            assert agg["denominator"] == ({"dev": 6, "threshold_validation": 2,
                                           "final_test": 4})[split]
            assert sum(agg["counts"].values()) == agg["denominator"]
            assert agg["calls_total"] == 0 and agg["cost_usd_total"] == 0.0

    def test_per_case_rows_calls_usage_cost(self, evaluate, tmp_path):
        report = self._run(evaluate, tmp_path)
        rows = {r["case_id"]: r for r in report["arms"]["jev_router"]["per_case"]}
        row = rows["syn-dev-001"]
        assert row["label"] == "correct"
        assert row["calls"] == 2
        assert row["input_tokens"] == 800 + 300
        assert row["cost_usd"] > 0
        assert rows["syn-dev-006"]["calls"] == 0  # explicit mention skips pre-spend
        assert rows["syn-dev-005"]["label"] == "unavailable_failure"

    def test_ledger_settles_fully(self, evaluate, tmp_path):
        report = self._run(evaluate, tmp_path)
        ledger = report["run"]["final_ledger"]
        assert ledger["requests"] == 17
        assert ledger["reserved_usd"] == pytest.approx(0.0)
        assert ledger["spent_usd"] > 0

    def test_report_carries_no_user_text(self, evaluate, tmp_path):
        self._run(evaluate, tmp_path)
        report_text = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
        cases_text = (tmp_path / "out" / "cases.jsonl").read_text(encoding="utf-8")
        for text in (report_text, cases_text):
            assert "日落" not in text and "帮我画" not in text

    def test_deterministic_labels_across_runs(self, evaluate, tmp_path):
        first = self._run(evaluate, tmp_path / "r1")
        second = self._run(evaluate, tmp_path / "r2")
        key = lambda r: [(c["case_id"], c["label"]) for c in r["arms"]["jev_router"]["per_case"]]
        assert key(first) == key(second)

    def test_split_filter_restricts_run(self, evaluate, tmp_path):
        report = self._run(evaluate, tmp_path, split_filter={"dev"})
        ids = [c["case_id"] for c in report["arms"]["jev_router"]["per_case"]]
        assert ids and all(i.startswith("syn-dev") for i in ids)


# ---------------------------------------------------------------------------
# Failure honesty & live-mode guard
# ---------------------------------------------------------------------------

class TestFailureHonesty:
    def test_missing_scripted_response_is_runner_error_in_denominator(
            self, evaluate, tmp_path):
        cases_path = tmp_path / "cases.json"
        _write_cases(cases_path, [
            {"case_id": "c1", "group_id": "g1", "split": "dev", "origin": "synthetic",
             "user_message": "帮我画一张图", "expectation":
                 {"kind": "recommend", "skill_id": "syn-image-tools"}},
        ])
        responses_path = tmp_path / "responses.json"
        responses_path.write_text(json.dumps({
            "schema_version": "jev-skill-eval-responses-v1",
            "responses": {}}), encoding="utf-8")
        report = evaluate.run_evaluation(
            cases_path=cases_path, skills_dir=SKILLS_DIR, out_dir=tmp_path / "out",
            responses_path=responses_path)
        agg = report["arms"]["jev_router"]["by_split"]["dev"]["synthetic"]
        assert agg["denominator"] == 1
        assert agg["counts"] == {"runner_error": 1}  # fixture bug is loud, not dropped

    def test_live_mode_refuses_without_ack_and_key(self, evaluate, tmp_path):
        with pytest.raises(evaluate.EvalInputError, match="acknowledge"):
            evaluate.run_evaluation(
                cases_path=CASES_PATH, skills_dir=SKILLS_DIR, out_dir=tmp_path / "out",
                responder="live", acknowledge_cost=False)
        with pytest.raises(evaluate.EvalInputError, match="TYPESAFE_API_KEY"):
            evaluate.run_evaluation(
                cases_path=CASES_PATH, skills_dir=SKILLS_DIR, out_dir=tmp_path / "out",
                responder="live", acknowledge_cost=True)
        assert not (tmp_path / "out").exists()  # refused before touching anything

    def test_unknown_responder_rejected(self, evaluate, tmp_path):
        with pytest.raises(evaluate.EvalInputError, match="responder"):
            evaluate.run_evaluation(
                cases_path=CASES_PATH, skills_dir=SKILLS_DIR, out_dir=tmp_path / "out",
                responder="dream")
