"""Seeded, reproducible stress variants kept separate from regression truth."""

from __future__ import annotations

import argparse
import json
import random

from evals.generalization_runner import evaluate_case, load_corpus


DEFAULT_SEED = 20260921


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--count", type=int, default=60)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    eligible = [case for case in load_corpus() if "negative_contrast" not in case.tags]
    if args.count > len(eligible):
        parser.error("count exceeds eligible persisted cases")
    prefixes = ("请问，", "麻烦看一下，", "帮我确认下，")
    selected = rng.sample(eligible, args.count)
    results = []
    for case in selected:
        query = rng.choice(prefixes) + case.query.rstrip("。？！?!") + rng.choice(("。", "？"))
        result = evaluate_case(case.model_copy(update={"query": query}))
        results.append(result)
    report = {
        "seed": args.seed, "total": len(results),
        "passed": sum(result["passed"] for result in results),
        "failed": sum(not result["passed"] for result in results),
        "failures": [result for result in results if not result["passed"]],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
