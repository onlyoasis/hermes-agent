#!/usr/bin/env python3
"""CLI entry for the jev-skill-router offline evaluation (plan §6-§7, D2).

Runs the reproducible harness in ``plugins/jev-skill-router/evaluate.py``
against a case file held OUTSIDE the repository (real D2 gold set), or
against the small synthetic fixtures for harness self-verification.

The default responder is ``scripted`` (no network, no credentials): it
verifies plumbing only and the report says so. ``--responder live`` is
the separate explicit entry for real vendor calls and requires BOTH
``--acknowledge-cost`` and the API-key env var (plan §6: 真实 API 评测为
单独显式入口). Arm A (existing agent) is always reported as unverified —
never simulated.

Example (synthetic self-check):

    python3 scripts/jev_skill_eval.py \
        --cases tests/plugins/jev_eval_fixtures/cases.json \
        --responses tests/plugins/jev_eval_fixtures/responses.json \
        --skills-dir tests/plugins/jev_eval_fixtures/skills \
        --out-dir /Volumes/ExternalPrivate/Runtime/hermes-agent/jev-debug/eval-syn
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "plugins" / "jev-skill-router"


def _load_eval_module():
    """Load the plugin package the way the runtime does (synthetic
    namespace under hermes_plugins), then import its evaluate module."""
    if "hermes_plugins" not in sys.modules:
        namespace = types.ModuleType("hermes_plugins")
        namespace.__path__ = []
        sys.modules["hermes_plugins"] = namespace
    if "hermes_plugins.jev_skill_router" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "hermes_plugins.jev_skill_router",
            PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "hermes_plugins.jev_skill_router"
        module.__path__ = [str(PLUGIN_DIR)]
        sys.modules["hermes_plugins.jev_skill_router"] = module
        spec.loader.exec_module(module)
    return importlib.import_module("hermes_plugins.jev_skill_router.evaluate")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="jev_skill_eval",
        description="Reproducible offline evaluation for jev-skill-router (plan §6).",
    )
    parser.add_argument("--cases", required=True, type=Path,
                        help="case JSON (schema jev-skill-eval-cases-v1); keep real "
                             "gold sets OUTSIDE the repository")
    parser.add_argument("--responses", type=Path, default=None,
                        help="scripted responses JSON (required for --responder scripted)")
    parser.add_argument("--skills-dir", required=True, type=Path,
                        help="frozen skill catalog directory (one SKILL.md folder per skill)")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="output directory for report.json / cases.jsonl (use the "
                             "external drive for real runs)")
    parser.add_argument("--responder", choices=("scripted", "live"), default="scripted",
                        help="scripted = deterministic fixtures, no network (default); "
                             "live = real vendor calls, requires --acknowledge-cost + key")
    parser.add_argument("--acknowledge-cost", action="store_true",
                        help="confirm that --responder live sends case text to the vendor "
                             "and spends real budget")
    parser.add_argument("--api-key-env", default="TYPESAFE_API_KEY",
                        help="env var holding the vendor API key for live runs")
    parser.add_argument("--split", action="append", default=None, metavar="SPLIT",
                        help="restrict to one split (repeatable); default runs all")
    parser.add_argument("--platform", default="telegram")
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--min-any-match", type=float, default=0.5)
    parser.add_argument("--min-fit", type=float, default=0.5)
    parser.add_argument("--min-choice-prob", type=float, default=0.5)
    parser.add_argument("--kw-min-score", type=int, default=2,
                        help="keyword-baseline threshold (arm B parameter)")
    parser.add_argument("--daily-budget-usd", type=float, default=1.0)
    parser.add_argument("--max-input-tokens", type=int, default=200000)
    parser.add_argument("--total-deadline-ms", type=int, default=2000,
                        help="overall Jev routing deadline per turn; use to compare latency tradeoffs")
    return parser.parse_args(argv)


def _print_summary(report) -> None:
    run = report["run"]
    print(f"report: {report['schema_version']}  responder={run['responder']}")
    if run.get("scripted_note"):
        print(f"note:   {run['scripted_note']}")
    print(f"arm A (existing_agent): {report['arms']['existing_agent']['status']} — "
          f"NOT simulated (no offline evidence)")
    for arm in ("keyword_baseline", "jev_router"):
        print(f"arm ({arm}):")
        by_split = report["arms"][arm]["by_split"]
        for split, origins in by_split.items():
            for origin, agg in origins.items():
                if not agg["denominator"]:
                    continue
                acc = "n/a" if agg["accuracy"] is None else f"{agg['accuracy']:.4f}"
                ci = agg["accuracy_ci95"]
                ci_text = "" if ci[0] is None else f" (95% CI {ci[0]}–{ci[1]})"
                print(f"  {split}/{origin}: n={agg['denominator']} accuracy={acc}{ci_text} "
                      f"counts={agg['counts']} calls={agg['calls_total']} "
                      f"cost=${agg['cost_usd_total']:.6f}")
                if "boundary_violation" in agg["counts"]:
                    print(f"    !! BOUNDARY VIOLATION x{agg['counts']['boundary_violation']} "
                          f"(forbidden skill suggested — zero tolerance, plan §6)")
                if "runner_error" in agg["counts"]:
                    print(f"    !! runner errors x{agg['counts']['runner_error']} "
                          f"(kept in the denominator; inspect per_case)")


def main(argv=None) -> int:
    args = _parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT))
    evaluate = _load_eval_module()
    try:
        report = evaluate.run_evaluation(
            cases_path=args.cases,
            skills_dir=args.skills_dir,
            out_dir=args.out_dir,
            responses_path=args.responses,
            responder=args.responder,
            platform=args.platform,
            model=args.model,
            min_any_match=args.min_any_match,
            min_fit=args.min_fit,
            min_choice_prob=args.min_choice_prob,
            daily_budget_usd=args.daily_budget_usd,
            max_input_tokens=args.max_input_tokens,
            total_deadline_ms=args.total_deadline_ms,
            kw_min_score=args.kw_min_score,
            split_filter=set(args.split) if args.split else None,
            acknowledge_cost=args.acknowledge_cost,
            api_key_env=args.api_key_env,
        )
    except evaluate.EvalInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_summary(report)
    print(f"written: {args.out_dir / 'report.json'}")
    print(f"written: {args.out_dir / 'cases.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
