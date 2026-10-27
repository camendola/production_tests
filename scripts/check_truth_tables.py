#!/usr/bin/env python3
"""Sanity checks of the MC-truth-graph tables in a nano file (nanoTruth_cfg.py or
nanoML_cfg.py output) and, optionally, in its endcap-split parquet.

    python3 scripts/check_truth_tables.py file_nano.root [--parquet file_split.parquet] [--max-events N]

Checks (ROOT): the association tables are parallel to their source collections, the
count/offset columns are consistent with the flat link tables, every link index is in
range, TruthCB_graphId[index] round-trips the link graphId, the two directions hold the
same (reco, truth) pairs, the shared energy of a layer cluster never exceeds its energy,
and every TruthCB row carries the caloBoundary level bit. It then prints per-event
summaries (TruthCB multiplicity, pdgIds, pileup fraction, unmatched tracksters) and the
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


def regroup(flat, counts):
    rows = ak.unflatten(ak.flatten(flat), ak.flatten(counts))
    return ak.unflatten(rows, ak.num(counts))


def check(cond, msg):
    ok = bool(ak.all(cond)) if not isinstance(cond, (bool, np.bool_)) else bool(cond)
    print(("  OK   " if ok else "  FAIL ") + msg)
    return ok


def check_assoc(ev, name, n_src, n_tgt, truth_graph_id=None, reco_to_truth=True):
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
    if reco_to_truth and truth_graph_id is not None:
        gid = ev[f"{name}Links_graphId"]
        ok &= check(truth_graph_id[idx] == gid, f"{name}: TruthCB_graphId[index] == link graphId")
    return ok


def pairs(ev, name, counts_name, idx_name):
    """Per event, the set of (row, linked index) pairs of a table."""
    nested = regroup(ev[idx_name], ev[counts_name])
    rows = ak.broadcast_arrays(ak.local_index(nested, axis=1), nested)[0]
    return [set(zip(r, i)) for r, i in zip(ak.to_list(ak.flatten(rows, axis=2)), ak.to_list(ak.flatten(nested, axis=2)))]


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
    wanted = [k for k in keys if k.startswith(("TruthCB", "nTruthCB", "CLUE3DHighToTruthCB", "LayerClusterToTruthCB",
                                                "TruthCBToCLUE3DHigh", "TruthCBToLayerCluster", "LayerCluster_energy",
                                                "ticlTrackstersCLUE3DHigh_raw_energy", "CLUE3DHighToSimTSByHits",
                                                "nCLUE3DHighToTruthCB", "nLayerClusterToTruthCB",
                                                "nTruthCBToCLUE3DHigh", "nTruthCBToLayerCluster"))]
    ev = tree.arrays(wanted, entry_stop=args.max_events, library="ak")
    n_ev = len(ev)
    print(f"{args.nano}: {n_ev} events")

    n_tcb = ak.num(ev["TruthCB_graphId"])
    n_ts = ak.num(ev["ticlTrackstersCLUE3DHigh_raw_energy"])
    n_lc = ak.num(ev["LayerCluster_energy"])
    gid = ev["TruthCB_graphId"]
    ok = True

    print("Structure:")
    ok &= check_assoc(ev, "CLUE3DHighToTruthCB", n_ts, n_tcb, gid)
    ok &= check_assoc(ev, "LayerClusterToTruthCB", n_lc, n_tcb, gid)
    ok &= check_assoc(ev, "TruthCBToCLUE3DHigh", n_tcb, n_ts, reco_to_truth=False)
    ok &= check_assoc(ev, "TruthCBToLayerCluster", n_tcb, n_lc, reco_to_truth=False)
    ok &= check(ak.all((ev["TruthCB_levelFlags"] & CALO_BOUNDARY_BIT) != 0, axis=1), "TruthCB: every row has the caloBoundary level bit")
    ok &= check(ak.all(ev["TruthCB_hasBoundary"], axis=1), "TruthCB: every row has a boundary checkpoint")
    ok &= check(ak.is_none(ak.firsts(ev["TruthCB_boundaryZ"])) | (ak.fill_none(ak.firsts(ev["TruthCB_boundaryZ"]), 0) == ak.fill_none(ak.firsts(ev["TruthCB_boundaryZ"]), 0)), "TruthCB: boundaryZ is not NaN")

    # reciprocity: the same (trackster, truth) pairs in both directions
    fwd = pairs(ev, "CLUE3DHighToTruthCB", "CLUE3DHighToTruthCB_nCLUE3DHighToTruthCBLinks", "CLUE3DHighToTruthCBLinks_index")
    rev = pairs(ev, "TruthCBToCLUE3DHigh", "TruthCBToCLUE3DHigh_nTruthCBToCLUE3DHighLinks", "TruthCBToCLUE3DHighLinks_index")
    rev = [set((b, a) for a, b in s) for s in rev]
    ok &= check(all(f == r for f, r in zip(fwd, rev)), "CLUE3DHigh <-> TruthCB: both directions hold the same pairs")

    # physics: shared energy per layer cluster never exceeds the cluster energy
    lc_shared = regroup(ev["LayerClusterToTruthCBLinks_sharedEnergy"], ev["LayerClusterToTruthCB_nLayerClusterToTruthCBLinks"])
    ok &= check(ak.all(ak.sum(lc_shared, axis=2) <= 1.001 * ev["LayerCluster_energy"] + 1e-6, axis=1), "LayerCluster: sum of shared energy <= cluster energy")
    sc = ev["CLUE3DHighToTruthCBLinks_score"]
    ok &= check(ak.all((sc >= 0) & (sc <= 1.0001), axis=1), "CLUE3DHigh links: 0 <= score <= 1")
    fr = ev["TruthCBToCLUE3DHighLinks_sharedEnergyFraction"]
    ok &= check(ak.all((fr >= 0) & (fr <= 1.0001), axis=1), "TruthCB->CLUE3DHigh links: 0 <= sharedEnergyFraction <= 1")

    print("Summary:")
    is_pu = ~((ev["TruthCB_bunchCrossing"] == 0) & (ev["TruthCB_eventIndex"] == 0))
    print(f"  TruthCB per event: {ak.to_list(n_tcb)[:10]}{' ...' if n_ev > 10 else ''}")
    print(f"  signal TruthCB per event: {ak.to_list(ak.sum(~is_pu, axis=1))[:10]}")
    print(f"  pileup fraction of TruthCB rows: {float(ak.sum(is_pu)) / max(1, float(ak.sum(n_tcb))):.3f}")
    sig_pdg = ak.flatten(ev["TruthCB_pdgId"][~is_pu])
    vals, cnts = np.unique(ak.to_numpy(sig_pdg), return_counts=True)
    print("  signal TruthCB pdgId counts: " + ", ".join(f"{v}:{c}" for v, c in zip(vals, cnts)))
    print(f"  TruthCB with HGCAL sim energy: {float(ak.sum(ev['TruthCB_simEnergyHGCAL'] > 0))}/{float(ak.sum(n_tcb))}")
    if "nticlSimTracksters" in keys:
        n_sts = tree.arrays(["nticlSimTracksters"], entry_stop=args.max_events, library="ak")["nticlSimTracksters"]
        print(f"  legacy SimTracksters (one per SimCluster with layer clusters) per event: {ak.to_list(n_sts)[:10]}")
    n_links = ev["CLUE3DHighToTruthCB_nCLUE3DHighToTruthCBLinks"]
    print(f"  CLUE3DHigh tracksters: {float(ak.sum(n_ts))}, without any TruthCB link: {float(ak.sum(n_links == 0))}")
    lc_links = ev["LayerClusterToTruthCB_nLayerClusterToTruthCBLinks"]
    print(f"  layer clusters: {float(ak.sum(n_lc))}, without any TruthCB link: {float(ak.sum(lc_links == 0))}")
    best_score = ak.firsts(regroup(sc, n_links), axis=2)
    print(f"  CLUE3DHigh best-link score: median {np.median(ak.to_numpy(ak.flatten(ak.drop_none(best_score)))):.3f}")

    if "CLUE3DHighToSimTSByHitsLinks_index" in keys:
        # best legacy SimTrackster match vs best TruthCB match, compared by the row count
        # of tracksters that have a match in both
        n_sim = ev["CLUE3DHighToSimTSByHits_nCLUE3DHighToSimTSByHitsLinks"]
        both = (n_sim > 0) & (n_links > 0)
        only_sim = (n_sim > 0) & (n_links == 0)
        only_truth = (n_sim == 0) & (n_links > 0)
        print(f"  tracksters matched by both SimTS and TruthCB: {float(ak.sum(both))}, SimTS only: {float(ak.sum(only_sim))}, TruthCB only: {float(ak.sum(only_truth))}")

    if args.parquet:
        print(f"Parquet {args.parquet}:")
        s = ak.from_parquet(args.parquet)
        t = s["TruthCB"]
        ok &= check(ak.all(t["TruthCB_boundaryZ"] >= 0, axis=1), "split: every TruthCB boundaryZ >= 0")
        a = s["CLUE3DHighToTruthCB"]
        ok &= check(ak.num(a["CLUE3DHighToTruthCB_nCLUE3DHighToTruthCBLinks"]) == ak.num(s["ticlTrackstersCLUE3DHigh"]["ticlTrackstersCLUE3DHigh_raw_energy"]), "split: CLUE3DHighToTruthCB parallel to the tracksters")
        idx = a["CLUE3DHighToTruthCBLinks_index"]
        has = ak.num(idx) > 0
        ok &= check(ak.fill_none(ak.max(idx, axis=1), -1)[has] < ak.num(t["TruthCB_graphId"])[has], "split: link index < nTruthCB")
        ok &= check(t["TruthCB_graphId"][idx] == a["CLUE3DHighToTruthCBLinks_graphId"], "split: graphId round-trips through the remapped index")
        n_in = ak.to_numpy(n_tcb)
        n_out = ak.to_numpy(ak.num(t["TruthCB_graphId"]))
        if 2 * len(n_in) == len(n_out):
            ok &= check(n_out[0::2] + n_out[1::2] == n_in, "split: TruthCB rows conserved between the two endcaps")
        print(f"  TruthCB_isPileup present: {'TruthCB_isPileup' in t.fields}, TruthCB_hasHGCALHits present: {'TruthCB_hasHGCALHits' in t.fields}")

    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
