"""ケース定義 1 件 = pytest のテスト 1 件。判定は 11.2 に従う。"""
import pytest

from tests.core.models import Verdict


def test_case(case, runner, record_outcome):
    outcome = runner.run(case)
    record_outcome(outcome)
    if outcome.verdict == Verdict.SKIP:
        pytest.skip(outcome.message)
    if outcome.verdict in (Verdict.FAIL, Verdict.ERROR):
        pytest.fail(outcome.render(), pytrace=False)
    if outcome.warnings or outcome.tolerated:
        print(outcome.render())
