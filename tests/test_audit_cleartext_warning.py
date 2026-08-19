"""Audit A3: boot warns loudly when binding non-loopback over plain HTTP
with web.cookie_secure off (the shipped-unit LAN configuration)."""


def test_cleartext_lan_bind_warns(web_app, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["uvicorn", "--host", "0.0.0.0"])
    web_app()
    err = capsys.readouterr().err
    assert "PLAIN HTTP" in err and "cookie_secure" in err


def test_cleartext_lan_bind_warns_equals_form(web_app, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["uvicorn", "--host=192.168.1.5"])
    web_app()
    assert "PLAIN HTTP" in capsys.readouterr().err


def test_loopback_bind_stays_quiet(web_app, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["uvicorn", "--host", "127.0.0.1"])
    web_app()
    assert "PLAIN HTTP" not in capsys.readouterr().err


def test_no_host_arg_means_uvicorn_default_loopback(web_app, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["uvicorn"])
    web_app()
    assert "PLAIN HTTP" not in capsys.readouterr().err
