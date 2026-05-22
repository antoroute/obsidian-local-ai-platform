from whisper_worker.__main__ import main


def test_main_is_callable() -> None:
    assert callable(main)
