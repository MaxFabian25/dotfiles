"""Pattern checks on the prose tasks, for conditions a pattern can settle. Prints one row per run.

Usage: mech_checks.py [--work DIR] LABEL
They back up the grading; they are not the grade.
"""

import argparse
import json
import re

from harness import TASKS, seat_dirs, work_dir


def words(s):
    return re.findall(r"[a-z0-9£%.,'-]+", s.lower())


def longest_copied_run(src, out):
    a, b = words(src), words(out)
    grams = {tuple(a[i : i + 12]) for i in range(len(a) - 11)}
    best = 0
    for i in range(len(b) - 11):
        if tuple(b[i : i + 12]) in grams:
            j = 12
            while i + j < len(b) and tuple(b[i + j - 11 : i + j + 1]) in grams:
                j += 1
            best = max(best, j)
    return best


def body(text):
    """The saved files, or the final message when nothing was saved."""
    files = re.split(r"^## Final message$", text, flags=re.M)[0]
    return files if "## File:" in files else text


CHECKS = {
    "staff-guide": lambda t: {
        "owner/version/date mention": bool(
            re.search(
                r"\b(owner|owned by|version|last (updated|reviewed)|review date|"
                r"publish|maintained by)\b",
                t,
                re.I,
            )
        ),
        "highest list number": max(
            (int(n) for n in re.findall(r"^\s*(\d+)\. ", t, re.M)), default=0
        ),
    },
    "kasse-guide": lambda t: {
        "support linked to accessibility": bool(
            re.search(
                r"(accessib\w+[^\n]{0,160}(support@|\+45)|"
                r"(support@|\+45)[^\n]{0,160}accessib)",
                t,
                re.I,
            )
        ),
    },
    "kasse-conformance": lambda t: {
        "EN version named": re.findall(r"V\d\.\d\.\d|EN 301 549 v\d", t, re.I),
        "WCAG version named": re.findall(r"WCAG \d\.\d", t),
        "version queried": bool(
            re.search(
                r"(version|edition)[^\n]{0,80}(\?|confirm|TODO|\[|query|which)", t, re.I
            )
        ),
    },
    "ledgerly-help": lambda t: {
        "key combos": sorted(
            set(
                re.findall(
                    r"\b(?:Fn|Control|Option|Shift|Command|Cmd|Ctrl|Opt)[-+ ]\S+", t
                )
            )
        ),
        "right-click": len(re.findall(r"right[- ]click", t, re.I)),
        "Control-click": len(re.findall(r"Control-click", t)),
        "Centre/Center": (t.count("Control Centre"), t.count("Control Center")),
    },
    "fieldnote-widget": lambda t: {
        "Customise/Customize": (t.count("Customis"), t.count("Customiz")),
        "Centre/Center": (t.count("Control Centre"), t.count("Control Center")),
        "possessive product": re.findall(r"\b(?:iPhone|iPad|Mac)'s\b", t),
        "FaceTime as verb": re.findall(
            r"FaceTime (?:your|them|a colleague|him|her)", t
        ),
    },
    "tidepool-backup": lambda t: {
        "key combos": sorted(
            set(
                re.findall(
                    r"\b(?:Fn|Control|Option|Shift|Command|Cmd|Ctrl|Opt)[-+ ]\S+", t
                )
            )
        ),
        "right-click": len(re.findall(r"right[- ]click", t, re.I)),
        "bold labels": re.findall(r"\*\*[^*]+\*\*", t)[:6],
        "Centre/Center": (t.count("Control Centre"), t.count("Control Center")),
        "cellular/mobile data": (
            t.lower().count("cellular"),
            t.lower().count("mobile data"),
        ),
        "possessive product": re.findall(r"\b(?:iPhone|iPad|Mac)'s\b", t),
    },
    "harbourline-reservation": lambda t: {
        "doc-range MAC": bool(
            re.search(r"00[:-]00[:-]5e[:-]00[:-]53[:-][0-9a-f]{2}", t, re.I)
        ),
        "author MAC kept": "3c:22:fb:91:0a:7e" in t,
        "other MACs": sorted(
            set(
                m
                for m in re.findall(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", t, re.I)
                if not re.match(r"00[:-]00[:-]5e[:-]00[:-]53", m, re.I)
            )
        ),
        "doc-range IPv4": bool(
            re.search(r"\b(192\.0\.2|198\.51\.100|203\.0\.113)\.\d+", t)
        ),
        "10.20.0.57 kept": "10.20.0.57" in t,
    },
    "harbourline-notes": lambda t: {
        "Before this update": len(re.findall(r"before this (update|release)", t, re.I)),
        "Previously": len(re.findall(r"\bpreviously\b", t, re.I)),
        "Fixed an issue": len(re.findall(r"fixed an issue", t, re.I)),
        "future release named": re.findall(r"3\.5|3\.4\.1|March", t),
    },
    "fenmoor-summary": lambda t: {
        "quoted labels": re.findall(r"\((?:the )?[\"“][A-Z][^\"”]*[\"”]\)", t),
        "FRSL": t.count("FRSL"),
        "Issuer+Company": ("Issuer" in t, bool(re.search(r"\bthe Company\b", t))),
        "unsecured count": len(re.findall(r"unsecured", t, re.I)),
        "generic risk kept": bool(
            re.search(r"risks generally associated|material adverse effect", t, re.I)
        ),
    },
    "fenmoor-conversion": lambda t: {
        "capitalised terms": sorted(
            set(
                re.findall(
                    r"Closing Price|Dealing Days?|Conversion (?:Price|Date)|"
                    r"Maturity Date|\bOffer\b",
                    t,
                )
            )
        ),
        "worked example £": bool(
            re.search(r"£\s?16\.25|£\s?1[3-9][.,]\d\d|for example|example", t, re.I)
        ),
    },
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label")
    ap.add_argument("--work")
    a = ap.parse_args()
    for seat_dir in seat_dirs(work_dir(a.work), a.label):
        meta = json.loads((seat_dir / "meta.json").read_text())
        check = CHECKS.get(meta["task"])
        if check is None:
            continue
        t = body((seat_dir / "deliverable.md").read_text())
        row = check(t)
        if meta["task"] == "fenmoor-conversion":
            src = (TASKS / "fenmoor-conversion" / "terms-condition-7.md").read_text()
            row["longest copied run (words)"] = longest_copied_run(src, t)
        print(f"{meta['task']}/{meta['seat']}: {row}")


if __name__ == "__main__":
    main()
