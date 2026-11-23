import stat

import pytest

from tools import setup_env


def test_secret_generation_is_private_and_never_overwrites(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".env.example").write_text(
        "SECRET_KEY=\nADMIN_PASSWORD=\nADMIN_TOTP_SECRET=\n"
    )
    monkeypatch.setattr(setup_env, "__file__", str(tmp_path / "tools" / "setup_env.py"))
    setup_env.main()
    path = tmp_path / ".env"
    contents = path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(line.split("=", 1)[1] for line in contents.splitlines())
    assert "otpauth://totp/" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        setup_env.main()
    assert path.read_text() == contents
