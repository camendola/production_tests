#!/bin/bash
#
# Full taus production chain with pileup: MINBIAS -> GSD -> RECO -> nanoML -> parquet.
# useFineCalo=0 (standard calorimeter truth: GSD_fragment_PU), 30 average pileup
# interactions per event.
#
# This is generate_taus_noFineCalo.sh with --pileup 30 preset. A minbias GEN-SIM
# library (Pythia8 SoftQCD at 14 TeV, CP5 tune) of n_events events is produced
# first with MINBIAS_GENSIM.py (seed = signal seed + 500000) and mixed into the
# GSD step by GSD_fragment_PU (Poisson average, 25 ns, bunch crossings -12..+3).
#   --pu FILE       reuse an existing minbias library instead of generating one
#   --nminbias N    size of the generated library (default n_events; keep it
#                   >= the average pileup, e.g. --nminbias 100 for a 10-event test)
#   --pileup N      override the preset 30
# Output files are tagged taus_stdCalo_PU30_* (the PFTruth nanoML sequence stays
# off: it is broken with pileup).
#
# Usage:   bash generate_taus_noFineCalo_30PU.sh <output_directory> [n_events] [options]
# Example: bash generate_taus_noFineCalo_30PU.sh /path/to/output/folder 1000
# Run with --help for the full option list.

PARTICLE=15   # tau (tau-)
PARTNAME="taus"
USE_FINE_CALO=0
PILEUP=30     # average pileup; the minbias library is generated unless --pu is given

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source "$SCRIPT_DIR/chain_common.sh"

run_chain "$@"
