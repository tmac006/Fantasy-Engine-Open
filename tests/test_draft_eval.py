from api.eval.draft_model import run


def test_representative_draft_scenarios_run_deterministically() -> None:
    first = run()
    second = run()
    assert first == second
    assert [result.scenario for result in first] == [
        "flat-middle-TE",
        "elite-TE-cliff",
        "superflex-QB-demand",
    ]
    elite = first[1]
    assert "(TE)" in elite.recommended
