def test_top_level_imports() -> None:
    import jazz_guru
    import jazz_guru.cli
    import jazz_guru.config
    import jazz_guru.db
    import jazz_guru.harness
    import jazz_guru.llm
    import jazz_guru.logging
    import jazz_guru.server
    import jazz_guru.state
    import jazz_guru.worker

    assert jazz_guru.__version__
