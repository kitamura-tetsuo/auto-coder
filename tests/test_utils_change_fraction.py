from src.auto_coder.utils import change_fraction


def test_change_fraction_ignores_large_prefix_when_last_20_lines_identical():
    # 30 lines total; first 10 lines differ, last 20 lines identical
    prefix_old = "\n".join([f"old-{i}" for i in range(10)])
    prefix_new = "\n".join([f"new-{i}" for i in range(10)])
    tail = "\n".join([f"same-{i}" for i in range(20)])

    s_old = f"{prefix_old}\n{tail}"
    s_new = f"{prefix_new}\n{tail}"

    frac = change_fraction(s_old, s_new)
    assert frac == 0.0  # tail-only比較なので先頭差分は無視される


def test_change_fraction_ignores_large_prefix_when_last_1000_chars_identical():
    # 長い1行文字列: 先頭側は異なるが末尾1000文字は同一
    common_tail = "x" * 1000
    s_old = ("A" * 5000) + common_tail
    s_new = ("B" * 5000) + common_tail

    frac = change_fraction(s_old, s_new)
    assert frac == 0.0


def test_change_fraction_detects_tail_difference():
    # 同一の大部分 + 末尾20行のうち数行が異なる
    shared_prefix = "\n".join([f"line-{i}" for i in range(200)])
    tail_old = "\n".join([f"end-{i}" for i in range(20)])
    tail_new = "\n".join(
        [f"end-{i if i < 15 else i+1}" for i in range(20)]
    )  # 末尾側に差分

    s_old = f"{shared_prefix}\n{tail_old}"
    s_new = f"{shared_prefix}\n{tail_new}"

    frac = change_fraction(s_old, s_new)
    assert 0.0 < frac < 1.0


def test_change_fraction_none_and_equal_cases():
    assert change_fraction(None, None) == 0.0
    assert change_fraction("", "") == 0.0
    assert change_fraction("a", "a") == 0.0
    assert change_fraction("a", "b") > 0.0
