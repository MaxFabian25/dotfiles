"""Copy runs into shuffled, unlabelled folders for grading.

Usage: blind.py [--work DIR] ROUND SEED LABEL[,LABEL] TASK [TASK ...]
Writes judge/ROUND/TASK/<letter>/ with reply.md, files/, renders/ and inputs.json, judge/ROUND/TASK/request.txt,
and judge/ROUND/key.json, which maps each letter back to its run. Grade before opening the key.
"""

import argparse
import json
import random
import shutil

from harness import TASKS, seat_dirs, work_dir

IGNORE = shutil.ignore_patterns(
    ".venv", "*venv*", "site-packages", "node_modules", "__pycache__", ".agents"
)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("round")
    ap.add_argument("seed", type=int)
    ap.add_argument("labels")
    ap.add_argument("tasks", nargs="+")
    ap.add_argument("--work")
    a = ap.parse_args()
    work = work_dir(a.work)
    rng = random.Random(a.seed)
    root = work / "judge" / a.round
    key_path = root / "key.json"
    key = json.loads(key_path.read_text()) if key_path.exists() else {}
    for task in a.tasks:
        runs = [
            d for label in a.labels.split(",") for d in seat_dirs(work, label, task)
        ]
        rng.shuffle(runs)
        if (root / task).exists():
            shutil.rmtree(root / task)
        key[task] = {}
        for i, src in enumerate(runs):
            letter = chr(ord("A") + i)
            dst = root / task / letter
            dst.mkdir(parents=True)
            shutil.copy(src / "final.md", dst / "reply.md")
            shutil.copy(src / "inputs.json", dst / "inputs.json")
            shutil.copytree(
                src / "workspace", dst / "files", ignore=IGNORE, symlinks=True
            )
            renders = work / "renders" / src.parent.parent.name / task / src.name
            if renders.exists():
                shutil.copytree(renders, dst / "renders")
            key[task][letter] = f"{src.parent.parent.name}/{src.name}"
        shutil.copy(TASKS / task / "prompt.txt", root / task / "request.txt")
    key_path.write_text(json.dumps(key, indent=2) + "\n")
    print(
        f"{sum(len(v) for v in key.values())} runs blinded in {root}; the key is in {key_path.name}"
    )


if __name__ == "__main__":
    main()
