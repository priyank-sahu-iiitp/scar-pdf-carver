#!/usr/bin/env python3
"""
mcnemar.py -- paired McNemar test, SCAR vs each baseline, from per-document
recovery outcomes (per_document_recovery.csv).

For a pair of tools over the same set of documents, build the discordant
counts:
  b = documents SCAR recovered but the baseline did not
  c = documents the baseline recovered but SCAR did not
McNemar's null hypothesis is that b and c come from the same distribution
(the two tools are equally likely to be the one that succeeds when they
disagree). We use the EXACT binomial test (two-sided) on min(b,c) ~
Binomial(b+c, 0.5), which is correct for small discordant counts rather than
the chi-square approximation. Concordant pairs (both right / both wrong)
carry no information and are excluded, as McNemar requires.

We report, per corpus and pooled: b, c, the exact two-sided p-value, and the
recovery counts for context.

Usage:
  py mcnemar.py --in per_document_recovery.csv --out mcnemar_results.csv
"""
import argparse
import csv
import math
from collections import defaultdict

BASELINES = ["cohen", "chen", "foremost", "scalpel", "photorec"]


def binom_two_sided(k, n, p=0.5):
    """Exact two-sided binomial p-value for observing k of n (or more
    extreme), under Binomial(n, p). For p=0.5 the distribution is symmetric,
    so two-sided = 2 * one-sided tail, capped at 1."""
    if n == 0:
        return 1.0

    def pmf(i):
        return math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))

    k = min(k, n - k)  # lower tail
    tail = sum(pmf(i) for i in range(0, k + 1))
    return min(1.0, 2 * tail)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # recovered[(corpus, image, document)][tool] = 0/1
    rec = defaultdict(dict)
    with open(args.inp, newline="") as fd:
        for row in csv.DictReader(fd):
            key = (row["Corpus"], row["Image"], row["Document"])
            rec[key][row["Tool"]] = int(row["Recovered"])

    # organise items per corpus (+ pooled)
    by_corpus = defaultdict(list)
    for key, tools in rec.items():
        by_corpus[key[0]].append(tools)
    corpora = sorted(by_corpus)

    rows = []
    for corpus in corpora + ["ALL"]:
        items = ([t for c in corpora for t in by_corpus[c]] if corpus == "ALL"
                 else by_corpus[corpus])
        for base in BASELINES:
            b = c = both = neither = 0
            scar_tot = base_tot = n = 0
            for tools in items:
                if "scar" not in tools or base not in tools:
                    continue
                s, x = tools["scar"], tools[base]
                n += 1
                scar_tot += s
                base_tot += x
                if s and not x:
                    b += 1
                elif x and not s:
                    c += 1
                elif s and x:
                    both += 1
                else:
                    neither += 1
            p = binom_two_sided(min(b, c), b + c)
            rows.append(dict(
                Corpus=corpus, Comparison="scar_vs_%s" % base, N=n,
                SCAR_recovered=scar_tot, Base_recovered=base_tot,
                b_scar_only=b, c_base_only=c, both=both, neither=neither,
                p_value=("%.2e" % p if p < 1e-3 else "%.4f" % p),
                significant_0_05=("yes" if p < 0.05 else "no"),
            ))

    cols = ["Corpus", "Comparison", "N", "SCAR_recovered", "Base_recovered",
            "b_scar_only", "c_base_only", "both", "neither", "p_value",
            "significant_0_05"]
    with open(args.out, "w", newline="") as fd:
        w = csv.DictWriter(fd, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # console: the SCAR-vs-Cohen headline per corpus
    print("SCAR vs Cohen (paired McNemar, exact):")
    for r in rows:
        if r["Comparison"] == "scar_vs_cohen":
            print("  %-9s N=%-4d SCAR=%-4d Cohen=%-4d  b=%-3d c=%-3d  p=%s  %s"
                  % (r["Corpus"], r["N"], r["SCAR_recovered"], r["Base_recovered"],
                     r["b_scar_only"], r["c_base_only"], r["p_value"],
                     "*" if r["significant_0_05"] == "yes" else ""))
    print("\n-> %s" % args.out)


if __name__ == "__main__":
    main()
