from app.run import build_collectors


def test_github_path_omits_server_owned_gongkao(monkeypatch) -> None:
    monkeypatch.setenv("GONGKAO_ON_SERVER", "true")
    sources = [collector.source for collector in build_collectors()]
    assert "gongkao" not in sources


def test_local_path_keeps_gongkao(monkeypatch) -> None:
    monkeypatch.delenv("GONGKAO_ON_SERVER", raising=False)
    sources = [collector.source for collector in build_collectors()]
    assert "gongkao" in sources
