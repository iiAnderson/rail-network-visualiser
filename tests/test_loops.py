"""Block finder and series-parallel reduction on toy graphs (plans/BETTER_LOOPS.md, step 2)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import loops  # noqa: E402


def chain(*xs):
    return list(zip(xs, xs[1:]))


def one_block(edges):
    bl = loops.blocks(edges)
    assert len(bl) == 1
    return bl[0]


def test_tree_has_no_blocks():
    assert loops.blocks(chain("a", "b", "c", "d")) == []


def test_diamond():
    assert loops.reduce_block(one_block(chain("s", "a", "m") + chain("s", "b", "m"))) == (True, 0)


def test_diamond_with_leaf_on_arm_is_one_block_and_leaf_is_a_bridge():
    e = chain("s", "a", "m") + chain("s", "b", "m") + [("b", "leaf")]
    bl = one_block(e)
    assert all("leaf" not in x for x in bl)
    assert loops.reduce_block(bl)[0]


def test_nested():
    e = chain("s", "a", "m") + chain("s", "b", "c", "m") + chain("b", "x", "c")
    assert loops.reduce_block(one_block(e)) == (True, 0)


def test_two_loops_sharing_a_station():
    e = chain("a", "b", "v", "a") + chain("v", "c", "d", "v")
    assert loops.reduce_block(one_block(e)) == (True, 0)


def test_ladder_with_crossovers_does_not_reduce():
    # 3x3 grid: two crossovers between three parallel tracks is not series-parallel (it has a K4 minor)
    rows = [chain(*("%s%d" % (r, c) for c in range(3))) for r in "abc"]
    rungs = [("a%d" % c, "b%d" % c) for c in range(3)] + [("b%d" % c, "c%d" % c) for c in range(3)]
    ok, core = loops.reduce_block(one_block(sum(rows, []) + rungs))
    assert not ok and core >= 3


def test_k4_does_not_reduce():
    e = [(a, b) for a in "wxyz" for b in "wxyz" if a < b]
    assert loops.reduce_block(one_block(e)) == (False, 4)
