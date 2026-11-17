from __future__ import annotations
from pathlib import Path
import pytest
from pagedrift.pipeline import DEFAULT_FROZEN_TIME, DEFAULT_PLAN, run_demo
@pytest.fixture(scope="session")
def demo_bundle(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("pagedrift-demo")
    summary = run_demo(root, plan_path=DEFAULT_PLAN, frozen_time=DEFAULT_FROZEN_TIME)
    return {"root": Path(root), "summary": summary}
