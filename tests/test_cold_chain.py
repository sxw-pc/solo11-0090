from pathlib import Path
from pagedrift.pipeline import run_demo
from pagedrift.catalog_intake import intake_catalog
from pagedrift.cold_chain import inspect_cold_chain

ROOT=Path(__file__).resolve().parents[1]/"pagedrift"/"fixtures"


def test_cold_chain_gap_witness_without_decision(tmp_path):
    run_demo(tmp_path/"demo")
    catalog=intake_catalog(tmp_path/"demo"/"snapshot",ROOT/"warehouse"/"offers.json")
    result=inspect_cold_chain(catalog,ROOT/"warehouse"/"promise-valid.json",ROOT/"cold_chain"/"BY-gap.json")
    assert result["batch_id"]=="BY"
    assert result["linear_interpolation_exposure"]=="0"
    assert result["illustrative_hot_path_exposure"]=="1"
    assert {(x["order_id"],x["route"]) for x in result["candidate_arcs_for_batch"]}=={("O2","east->west"),("O3","local")}
    assert result["certified_exposure_upper"] is None
    assert result["release_decisions"] is None


def test_slower_physical_rate_removes_hot_witness(tmp_path):
    run_demo(tmp_path/"demo")
    catalog=intake_catalog(tmp_path/"demo"/"snapshot",ROOT/"warehouse"/"offers.json")
    result=inspect_cold_chain(catalog,ROOT/"warehouse"/"promise-valid.json",ROOT/"cold_chain"/"BY-slow.json")
    assert result["max_rate_c_per_hour"]=="2"
    assert result["illustrative_hot_path_exposure"]=="0"
    assert result["certified_exposure_upper"] is None
