"""Reproducible binary Layer benchmark for the current Laya screen classifier.

This evaluates only the classifier decision. It does not call route_agent,
Planner, a provider, or any tools. Abstentions are reported separately and are
excluded from precision/recall/F1; coverage makes that exclusion visible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_paths import RUNTIME_ROOT  # noqa: E402

CASES = Path(__file__).with_name("cases.v1.jsonl")
OUTPUTS = RUNTIME_ROOT / "benchmark-runs" / "layer_behavior"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [r for r in rows if r["predicted_tool"] is not None]
    tp = sum(r["expected_tool"] and r["predicted_tool"] for r in evaluated)
    fp = sum((not r["expected_tool"]) and r["predicted_tool"] for r in evaluated)
    fn = sum(r["expected_tool"] and (not r["predicted_tool"]) for r in evaluated)
    tn = sum((not r["expected_tool"]) and (not r["predicted_tool"]) for r in evaluated)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    expected_positive = sum(bool(r["expected_tool"]) for r in rows)
    expected_negative = len(rows) - expected_positive
    positive_abstentions = sum(r["expected_tool"] and r["predicted_tool"] is None for r in rows)
    negative_abstentions = sum((not r["expected_tool"]) and r["predicted_tool"] is None for r in rows)
    return {
        "total": len(rows), "classified": len(evaluated),
        "abstained": len(rows) - len(evaluated),
        "coverage": round(len(evaluated) / len(rows), 4) if rows else 0.0,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision_tool": round(precision, 4),
        "recall_tool": round(recall, 4), "f1_tool": round(f1, 4),
        "accuracy_classified": round((tp + tn) / len(evaluated), 4) if evaluated else 0.0,
        # Abstentions fall back to the downstream Router in production. This conservative
        # recall treats them as unresolved action requests so they cannot disappear from FN.
        "positive_abstentions": positive_abstentions,
        "negative_abstentions": negative_abstentions,
        "tool_recall_over_all_action_cases": round(tp / expected_positive, 4)
        if expected_positive else 0.0,
        "action_miss_case_ids": [r["id"] for r in rows
                                 if r["expected_tool"] and r["predicted_tool"] is not True],
        "false_positive_case_ids": [r["id"] for r in rows
                                    if not r["expected_tool"] and r["predicted_tool"] is True],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--output", type=Path, default=None,
                        help="JSON output path (default: timestamped file under var/benchmark-runs/layer_behavior/)")
    args = parser.parse_args()
    cases_path = args.cases.resolve()
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8-sig").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]

    import runtime.laya_router as laya

    if not laya.laya_router_enabled():
        print("Layer benchmark unavailable: FORGE_LAYA is disabled or laya/torch is missing.",
              file=sys.stderr)
        return 2

    router = laya.laya_router()
    checkpoint_env = os.getenv("FORGE_LAYA_CHECKPOINT", "").strip()
    configured_checkpoint = checkpoint_env or str(
        ROOT / "data" / "laya_forge" / "forge_finetuned_5000_xpu_fixed_arith"
    )
    checkpoint = Path(configured_checkpoint)
    config_files = [p for p in (checkpoint / "rl_agent_config.json",
                                checkpoint / "config.json") if p.is_file()]
    checkpoint_config_hash = _sha256(config_files[0]) if config_files else None
    rows: list[dict[str, Any]] = []
    for case in cases:
        decision = router.screen(case["text"])
        details = getattr(router, "last_screen_details", {}) or {}
        predicted = True if decision == "tool_needed" else False if decision == "direct_text" else None
        rows.append({
            "id": case["id"], "category": case["category"],
            "expected_tool": bool(case["expected_tool"]),
            "predicted_tool": predicted,
            "decision": decision,
            "confidence": details.get("confidence"),
            "threshold": details.get("threshold"),
            "error": details.get("error"),
            "text": case["text"],
        })

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row)

    report = {
        "schema_version": 1,
        "benchmark": "layer_behavior_binary.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"path": str(cases_path), "sha256": _sha256(cases_path), "cases": len(cases)},
        "classifier": {
            "backend": router.backend,
            "checkpoint": str(checkpoint),
            "checkpoint_config_sha256": checkpoint_config_hash,
            "confidence_threshold": laya.laya_router_confidence_threshold(),
            "git_commit": os.getenv("GIT_COMMIT", ""),
        },
        "semantics": {
            "positive_label": "requires an action/tool (including ASK_USER cases)",
            "abstention": "None; excluded from confusion counts and reported as coverage",
            "scope": "Layer screen() only; not downstream routing or end-to-end behavior",
        },
        "overall": _metrics(rows),
        "by_category": {key: _metrics(value) for key, value in sorted(by_category.items())},
        "cases": rows,
    }
    out = args.output.resolve() if args.output else OUTPUTS / (
        datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out), "dataset_sha256": report["dataset"]["sha256"],
                      "overall": report["overall"], "by_category": report["by_category"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
