from tally import tally
def test_clear():
    rows, top = tally([(1,2,3),(1,3,2),(2,1,3)], [1,2,3])
    assert top == [1] and rows[0]["points"] == 8
def test_tiebreak_firsts():
    # 1 and 2 both finish on 4 points; 1 has the first-place vote
    rows, top = tally([(1,2,None),(3,2,1)], [1,2,3])
    assert top == [1]


def test_full_tie():
    rows, top = tally([(1,2),(2,1)], [1,2])
    assert sorted(top)==[1,2]
def test_no_votes():
    assert tally([], [1,2])[1]==[]
