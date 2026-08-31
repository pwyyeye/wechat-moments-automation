from src.agent.wechat_identity import _is_wechat_main_candidate


def test_known_minimized_wechat_window_is_not_rejected_by_placeholder_size():
    accepted, score = _is_wechat_main_candidate(
        "WeChatMainWndForPC",
        "",
        "",
        160,
        28,
        True,
    )

    assert accepted
    assert score > 100_000_000


def test_process_based_detection_accepts_new_window_class_without_version_rules():
    accepted, _ = _is_wechat_main_candidate(
        "Chrome_WidgetWin_0",
        "",
        "weixin.exe",
        1200,
        800,
        False,
    )

    assert accepted


def test_unrelated_small_helper_window_is_rejected():
    accepted, _ = _is_wechat_main_candidate(
        "HelperWindow",
        "",
        "weixin.exe",
        120,
        80,
        False,
    )

    assert not accepted
