from src.executor.file_dialog import FileDialogHandler


def test_foreground_file_dialog_uses_standard_dialog_class(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.executor.file_dialog.win32gui.GetForegroundWindow",
        lambda: 123,
    )
    monkeypatch.setattr(
        "src.executor.file_dialog.win32gui.GetClassName",
        lambda hwnd: "#32770",
    )

    assert FileDialogHandler._foreground_file_dialog() == 123


def test_foreground_file_dialog_rejects_other_windows(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.executor.file_dialog.win32gui.GetForegroundWindow",
        lambda: 456,
    )
    monkeypatch.setattr(
        "src.executor.file_dialog.win32gui.GetClassName",
        lambda hwnd: "Qt51514QWindowIcon",
    )

    assert FileDialogHandler._foreground_file_dialog() is None
