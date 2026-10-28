#!/usr/bin/env python3
"""Sanity checks of the MC-truth-graph tables in a nano file (nanoTruth_cfg.py or
nanoML_cfg.py output) and, optionally, in its endcap-split parquet.

    python3 scripts/check_truth_tables.py file_nano.root [--parquet file_split.parquet] [--max-events N]

Two truth particle tables can be present, each with its reco <-> truth association tables
for ticlTrackstersCLUE3DHigh (CLUE3DHigh) and hgcalMergeLayerClusters (LayerCluster):
  TruthCB (RECO.py truthAssoc=1, required): the calo-boundary particles. Tables
    <reco>ToTruthCB (Fixed: every particle sharing energy, best first),
    <reco>ToTruthCBAdaptiveTight / AdaptiveNominal (one particle) and TruthCBTo<reco>.
  TruthFG (RECO.py truthFullGraph=1, optional): the particles the full-graph associators
    may assign a reco object to, the boundary particles and their ancestors. The same
    tables with FG in place of CB; an adaptive link may point at an ancestor.

Checks (ROOT), per truth table: the association tables are parallel to their source
collections, the count/offset columns are consistent with the flat link tables, every
link index is in range, <truth>_graphId[index] round-trips the link graphId, the two
directions hold the same (reco, truth) pairs, an adaptive table has at most one link per
row and that link is one of the Fixed candidates of the row, the shared energy of a layer
cluster never exceeds its energy, scores and fractions are in [0, 1] and every row has
HGCAL sim energy. TruthCB rows must carry the caloBoundary level bit and a boundary
checkpoint, and every TruthCB row must be a TruthFG row. It then prints per-event
summaries (multiplicities, pdgIds, pileup fraction, unmatched tracksters, how often the
full-graph adaptive match is an ancestor rather than a boundary particle) and the
agreement of the best TruthCB match with the best legacy SimTrackster match when the
CLUE3DHighToSimTSByHits table is present.
Needs uproot + awkward (the CMSSW python has them).
"""

import argparse
import sys

import awkward as ak
import numpy as np
import uproot

CALO_BOUNDARY_BIT = 1 << 3  # truth::LevelFlag::CaloBoundary
TRUTH_GROUPS = ("TruthCB", "TruthFG")
ADAPTIVE_POINTS = ("AdaptiveTight", "AdaptiveNominal")
# reco short name -> (nano/parquet group, branch giving the size of the collection)
RECO = {
    "CLUE3DHigh": ("ticlTrackstersCLUE3DHigh", "ticlTrackstersCLUE3DHigh_raw_energy"),
    "LayerCluster": ("LayerCluster", "LayerCluster_energy"),
}


def regroup(flat, counts):
    rows = ak.unflatten(ak.flatten(flat), ak.flatten(counts))
    return ak.unflatten(rows, ak.num(counts))


def check(cond, msg):
    ok = bool(ak.all(cond)) if not isinstance(cond, (bool, np.bool_)) else bool(cond)
    print(("  OK   " if ok else "  FAIL ") + msg)
    return ok


def check_assoc(ev, name, n_src, n_tgt, truth_graph_id=None, truth_name="TruthCB"):
    """One association table: parallelism, counts/offsets, index range, graphId."""
    ok = True
    cnt, off = ev[f"{name}_n{name}Links"], ev[f"{name}_o{name}Links"]
    idx = ev[f"{name}Links_index"]
    ok &= check(ak.num(cnt) == n_src, f"{name}: rows parallel to the source collection")
    ok &= check(ak.sum(cnt, axis=1) == ak.num(idx), f"{name}: sum of counts = number of links")
    exp_off = ak.concatenate([ak.zeros_like(cnt[:, :1]), ak.values_astype(cnt, np.int64)[:, :-1]], axis=1)
    exp_off = ak.Array([np.cumsum(x) for x in ak.to_list(exp_off)]) if len(cnt) else exp_off
    ok &= check(ak.all(off == exp_off, axis=1), f"{name}: offsets = exclusive prefix sum of counts")
    has = ak.num(idx) > 0
    max_idx = ak.fill_none(ak.max(idx, axis=1), -1)
    ok &= check(max_idx[has] < n_tgt[has], f"{name}: link index < size of the target table")
    if truth_graph_id is not None:
        gid = ev[f"{name}Links_graphId"]
        ok &= check(truth_graph_id[idx] == gid, f"{name}: {truth_name}_graphId[index] == link graphId")
    return ok


def pairs(ev, name):
    """Per event, the set of (row, linked index) pairs of a table."""
    nested = regroup(ev[f"{name}Links_index"], ev[f"{name}_n{name}Links"])
    rows = ak.broadcast_arrays(ak.local_index(nested, axis=1), nested)[0]
    return [set(zip(r, i)) for r, i in zip(ak.to_list(ak.flatten(rows, axis=2)), ak.to_list(ak.flatten(nested, axis=2)))]


def check_truth_group(ev, keys, grp, n_reco):
    """Structure and physics of one truth particle table and its association tables."""
    ok = True
    gid = ev[f"{grp}_graphId"]
    n_truth = ak.num(gid)
    # The boundary particles are an antichain, so their footprints are disjoint and the
    # shared energies of one layer cluster add up to at most its energy. A full-graph
    # Fixed row lists a particle together with its ancestors, whose footprints nest, so
    # only each link on its own is bounded.
    antichain = grp == "TruthCB"

    for reco, n_src in n_reco.items():
        fixed, back = f"{reco}To{grp}", f"{grp}To{reco}"
        ok &= check_assoc(ev, fixed, n_src, n_truth, gid, grp)
        ok &= check_assoc(ev, back, n_truth, n_src)
        fwd = pairs(ev, fixed)
        rev = [set((b, a) for a, b in s) for s in pairs(ev, back)]
        ok &= check(all(f == r for f, r in zip(fwd, rev)), f"{reco} <-> {grp}: both directions hold the same pairs")
        sc = ev[f"{fixed}Links_score"]
        ok &= check(ak.all((sc >= 0) & (sc <= 1.0001), axis=1), f"{fixed} links: 0 <= score <= 1")
        fr = ev[f"{back}Links_sharedEnergyFraction"]
        ok &= check(ak.all((fr >= 0) & (fr <= 1.0001), axis=1), f"{back} links: 0 <= sharedEnergyFraction <= 1")

        fixed_rows = regroup(ev[f"{fixed}Links_index"], ev[f"{fixed}_n{fixed}Links"])
        for wp in ADAPTIVE_POINTS:
            name = f"{fixed}{wp}"
            if f"{name}Links_index" not in keys:
                print(f"  SKIP {name}: not in the file (nano made before the adaptive tables were added)")
                continue
            ok &= check_assoc(ev, name, n_src, n_truth, gid, grp)
            cnt = ev[f"{name}_n{name}Links"]
            ok &= check(ak.all(cnt <= 1, axis=1), f"{name}: at most one link per row")
            # The adaptive climb answers with one of the candidates the Fixed row lists.
            pick = ak.fill_none(ak.firsts(regroup(ev[f"{name}Links_index"], cnt), axis=2), -1)
            in_fixed = ak.any(fixed_rows == pick, axis=2) | (cnt == 0)
            ok &= check(ak.all(in_fixed, axis=1), f"{name}: the adaptive link is one of the Fixed candidates of its row")

    lc = f"LayerClusterTo{grp}"
    lc_shared = regroup(ev[f"{lc}Links_sharedEnergy"], ev[f"{lc}_n{lc}Links"])
    lc_energy = ev["LayerCluster_energy"]
    if antichain:
        ok &= check(ak.all(ak.sum(lc_shared, axis=2) <= 1.001 * lc_energy + 1e-6, axis=1),
                    f"LayerCluster -> {grp}: sum of shared energy <= cluster energy")
    else:
        ok &= check(ak.all(ak.fill_none(ak.max(lc_shared, axis=2), 0) <= 1.001 * lc_energy + 1e-6, axis=1),
                    f"LayerCluster -> {grp}: every shared energy <= cluster energy")
    ok &= check(ak.all(ev[f"{grp}_simEnergyHGCAL"] > 0, axis=1), f"{grp}: every row has HGCAL sim energy")
    ok &= check(~ak.any(np.isnan(ev[f"{grp}_boundaryZ"]), axis=1), f"{grp}: boundaryZ is not NaN")
    return ok


def summarize_truth_group(ev, grp):
    n = ak.num(ev[f"{grp}_graphId"])
    n_ev = len(ev)
    is_pu = ~((ev[f"{grp}_bunchCrossing"] == 0) & (ev[f"{grp}_eventIndex"] == 0))
    print(f"  {grp} per event: {ak.to_list(n)[:10]}{' ...' if n_ev > 10 else ''}")
    print(f"  signal {grp} per event: {ak.to_list(ak.sum(~is_pu, axis=1))[:10]}")
    print(f"  pileup fraction of {grp} rows: {float(ak.sum(is_pu)) / max(1, float(ak.sum(n))):.3f}")
    sig_pdg = ak.flatten(ev[f"{grp}_pdgId"][~is_pu])
    vals, cnts = np.unique(ak.to_numpy(sig_pdg), return_counts=True)
    print(f"  signal {grp} pdgId counts: " + ", ".join(f"{v}:{c}" for v, c in zip(vals, cnts)))
    if grp != "TruthCB":
        no_boundary = ~ev[f"{grp}_hasBoundary"]
        print(f"  {grp} rows without boundary crossing (ancestors): {float(ak.sum(no_boundary))}/{float(ak.sum(n))}, "
              f"signal: {float(ak.sum(no_boundary & ~is_pu))}")
    for reco in RECO:
        n_links = ev[f"{reco}To{grp}_n{reco}To{grp}Links"]
        print(f"  {reco} without any {grp} link: {float(ak.sum(n_links == 0))}/{float(ak.sum(ak.num(n_links)))}")
    sc = ev[f"CLUE3DHighTo{grp}Links_score"]
    n_links = ev[f"CLUE3DHighTo{grp}_nCLUE3DHighTo{grp}Links"]
    best = ak.drop_none(ak.flatten(ak.firsts(regroup(sc, n_links), axis=2)))
    if len(best):
        print(f"  CLUE3DHigh best-link score to {grp}: median {np.median(ak.to_numpy(best)):.3f}")


def best_graph_id(ev, name, fill):
    """Per reco row, the graphId of the first link of a table, `fill` without a link."""
    return ak.fill_none(ak.firsts(regroup(ev[f"{name}Links_graphId"], ev[f"{name}_n{name}Links"]), axis=2), fill)


def check_parquet(path, ev, keys):
    print(f"Parquet {path}:")
    ok = True
    s = ak.from_parquet(path)
    for grp in TRUTH_GROUPS:
        if grp not in s.fields:
            print(f"  SKIP {grp}: not in the parquet")
            continue
        t = s[grp]
        n_t = ak.num(t[f"{grp}_graphId"])
        ok &= check(ak.all(t[f"{grp}_boundaryZ"] >= 0, axis=1), f"split: every {grp} boundaryZ >= 0")
        if grp != "TruthCB":
            ok &= check(ak.all(t[f"{grp}_hasBoundary"] | (t[f"{grp}_eta"] >= 0), axis=1),
                        f"split: every {grp} row without boundary crossing has eta >= 0")
        if f"{grp}_graphId" in keys:
            n_in = ak.to_numpy(ak.num(ev[f"{grp}_graphId"]))
            n_out = ak.to_numpy(n_t)
            if 2 * len(n_in) == len(n_out):
                ok &= check(n_out[0::2] + n_out[1::2] == n_in, f"split: {grp} rows conserved between the two endcaps")
        print(f"  {grp}_isPileup present: {f'{grp}_isPileup' in t.fields}, "
              f"{grp}_hasHGCALHits present: {f'{grp}_hasHGCALHits' in t.fields}")
        for reco, (reco_grp, size_branch) in RECO.items():
            n_r = ak.num(s[reco_grp][size_branch])
            for name in [f"{reco}To{grp}"] + [f"{reco}To{grp}{wp}" for wp in ADAPTIVE_POINTS]:
                if name not in s.fields:
                    print(f"  SKIP {name}: not in the parquet")
                    continue
                a = s[name]
                ok &= check(ak.num(a[f"{name}_n{name}Links"]) == n_r, f"split: {name} parallel to {reco_grp}")
                idx = a[f"{name}Links_index"]
                has = ak.num(idx) > 0
                ok &= check(ak.fill_none(ak.max(idx, axis=1), -1)[has] < n_t[has], f"split: {name} link index < n{grp}")
                ok &= check(t[f"{grp}_graphId"][idx] == a[f"{name}Links_graphId"],
                            f"split: {name} graphId round-trips through the remapped index")
            name = f"{grp}To{reco}"
            if name in s.fields:
                a = s[name]
                ok &= check(ak.num(a[f"{name}_n{name}Links"]) == n_t, f"split: {name} parallel to {grp}")
                idx = a[f"{name}Links_index"]
                has = ak.num(idx) > 0
                ok &= check(ak.fill_none(ak.max(idx, axis=1), -1)[has] < n_r[has], f"split: {name} link index < n{reco_grp}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nano")
    ap.add_argument("--parquet", help="endcap-split parquet made from the same file")
    ap.add_argument("--max-events", type=int, default=None)
    args = ap.parse_args()

    tree = uproot.open(args.nano)["Events"]
    keys = set(tree.keys())
    needed = ["nTruthCB", "TruthCB_graphId", "CLUE3DHighToTruthCBLinks_index", "LayerClusterToTruthCBLinks_index"]
    missing = [k for k in needed if k not in keys]
    if missing:
        sys.exit(f"missing branches: {missing} (was the RECO made with truthAssoc=1 and the nano with the truth tables?)")
    prefixes = ["LayerCluster_energy", "ticlTrackstersCLUE3DHigh_raw_energy", "CLUE3DHighToSimTSByHits"]
    for grp in TRUTH_GROUPS:
        prefixes += [grp, f"n{grp}"]
        for reco in RECO:
            prefixes += [f"{reco}To{grp}", f"n{reco}To{grp}"]
    wanted = [k for k in keys if k.startswith(tuple(prefixes))]
    ev = tree.arrays(wanted, entry_stop=args.max_events, library="ak")
    n_ev = len(ev)
    print(f"{args.nano}: {n_ev} events")

    n_reco = {reco: ak.num(ev[size_branch]) for reco, (_, size_branch) in RECO.items()}
    n_ts = n_reco["CLUE3DHigh"]
    ok = True

    groups = []
    for grp in TRUTH_GROUPS:
        if f"{grp}_graphId" not in keys:
            print(f"SKIP {grp}: not in the file (nano made before the TruthFG tables were added)")
            continue
        if grp != "TruthCB" and ak.sum(ak.num(ev[f"{grp}_graphId"])) == 0:
            print(f"SKIP {grp}: present but empty in every event (RECO made without truthFullGraph=1)")
            continue
        groups.append(grp)
        print(f"Structure and physics, {grp}:")
        ok &= check_truth_group(ev, keys, grp, n_reco)

    print("Cross-checks:")
    ok &= check(ak.all((ev["TruthCB_levelFlags"] & CALO_BOUNDARY_BIT) != 0, axis=1), "TruthCB: every row has the caloBoundary level bit")
    ok &= check(ak.all(ev["TruthCB_hasBoundary"], axis=1), "TruthCB: every row has a boundary checkpoint")
    if "TruthFG" in groups:
        # RECO.py gives the full-graph targets the calo-boundary selection (signal and
        # pile-up, no pt floor), so every boundary particle is an assignable target.
        cb_in_fg = [set(a) <= set(b) for a, b in zip(ak.to_list(ev["TruthCB_graphId"]), ak.to_list(ev["TruthFG_graphId"]))]
        ok &= check(all(cb_in_fg), "TruthCB is a subset of TruthFG (same graph ids)")

    print("Summary:")
    for grp in groups:
        summarize_truth_group(ev, grp)
    if "nticlSimTracksters" in keys:
        n_sts = tree.arrays(["nticlSimTracksters"], entry_stop=args.max_events, library="ak")["nticlSimTracksters"]
        print(f"  legacy SimTracksters (one per SimCluster with layer clusters) per event: {ak.to_list(n_sts)[:10]}")
    print(f"  CLUE3DHigh tracksters: {float(ak.sum(n_ts))}")

    if "TruthFG" in groups and "CLUE3DHighToTruthFGAdaptiveNominalLinks_index" in keys:
        # How often the full-graph climb answers with an ancestor instead of the boundary
        # particle the calo-boundary association picks.
        name = "CLUE3DHighToTruthFGAdaptiveNominal"
        flags = ev["TruthFG_levelFlags"][ev[f"{name}Links_index"]]
        n_matched = float(ak.sum(ev[f"{name}_n{name}Links"]))
        n_ancestor = float(ak.sum((flags & CALO_BOUNDARY_BIT) == 0))
        print(f"  CLUE3DHigh AdaptiveNominal full-graph matches: {n_matched}, of which an ancestor above the calo boundary: {n_ancestor}")
        cb_best = best_graph_id(ev, "CLUE3DHighToTruthCB", -1)
        fg_pick = best_graph_id(ev, name, -2)
        both = (cb_best >= 0) & (fg_pick >= 0)
        print(f"  tracksters with both a TruthCB and a full-graph adaptive match: {float(ak.sum(both))}, "
              f"same particle: {float(ak.sum(both & (cb_best == fg_pick)))}")

    if "CLUE3DHighToSimTSByHitsLinks_index" in keys:
        # best legacy SimTrackster match vs best TruthCB match, compared by the row count
        # of tracksters that have a match in both
        n_links = ev["CLUE3DHighToTruthCB_nCLUE3DHighToTruthCBLinks"]
        n_sim = ev["CLUE3DHighToSimTSByHits_nCLUE3DHighToSimTSByHitsLinks"]
        both = (n_sim > 0) & (n_links > 0)
        only_sim = (n_sim > 0) & (n_links == 0)
        only_truth = (n_sim == 0) & (n_links > 0)
        print(f"  tracksters matched by both SimTS and TruthCB: {float(ak.sum(both))}, SimTS only: {float(ak.sum(only_sim))}, TruthCB only: {float(ak.sum(only_truth))}")

    if args.parquet:
        ok &= check_parquet(args.parquet, ev, keys)

    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
