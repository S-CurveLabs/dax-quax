"""Fetching the .NET assemblies a live connection needs.

`pip install dax-quax` cannot carry them: they are Microsoft's, whether they may be
redistributed is unsettled, and they are 5 MB that only the live source uses. So there is
a verb, and the point of these tests is that someone who has never seen this project can
find it from the error they hit.
"""

from __future__ import annotations

import pathlib

import pytest

from dax_quax.runtime import ADOMD_DLL, DEFAULT_VERSION, install, user_runtime_dir


def test_the_runtime_goes_to_a_per_user_folder_not_site_packages(monkeypatch):
    """Writing inside an installed package works in a venv and fails on a system install."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    where = user_runtime_dir()
    assert where.name == "runtime"
    assert where.parent.name == "dax-quax"
    assert "site-packages" not in str(where)


def test_the_folder_resolves_without_localappdata(monkeypatch):
    """Not Windows-only: the module has to import and answer anywhere."""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert user_runtime_dir().parts[-2:] == ("dax-quax", "runtime")


def test_the_loader_looks_where_the_verb_installs(monkeypatch, tmp_path):
    """The two halves have to agree, or the download lands somewhere nothing reads.

    The packaged _libs/ takes precedence and exists in a development checkout, so
    `live.__file__` is moved somewhere empty to get past it -- which is also what a
    pip-installed copy looks like before the verb has ever been run.
    """
    from dax_quax.sources import live

    target = tmp_path / "dax-quax" / "runtime"
    target.mkdir(parents=True)
    (target / ADOMD_DLL).write_bytes(b"not a real assembly")
    monkeypatch.setattr("dax_quax.runtime.user_runtime_dir", lambda: target)
    monkeypatch.delenv("DAXQUAX_ADOMD_DIR", raising=False)
    monkeypatch.setattr(live, "__file__", str(tmp_path / "pkg" / "sources" / "live.py"))
    assert live.adomd_dir() == target


def test_a_missing_runtime_names_the_command_to_run(monkeypatch, tmp_path):
    """The old message named tools/fetch_libs.py, which a pip user does not have."""
    from dax_quax.errors import DaxQuaxError
    from dax_quax.sources import live

    monkeypatch.setenv("DAXQUAX_ADOMD_DIR", str(tmp_path / "nothing"))
    monkeypatch.setattr("dax_quax.runtime.user_runtime_dir", lambda: tmp_path / "also-not")
    monkeypatch.setattr(live, "__file__", str(tmp_path / "a" / "b" / "live.py"))

    with pytest.raises(DaxQuaxError) as caught:
        live.adomd_dir()
    message = str(caught.value)
    assert "dax-quax install-runtime" in message
    assert ".pbix" in message, "it should say a file source needs none of this"
    assert "fetch_libs" not in message


def test_the_verb_is_on_the_cli():
    from dax_quax.cli import build_parser

    args = build_parser().parse_args(["install-runtime", "--no-tom"])
    assert args.command == "install-runtime"
    assert args.no_tom is True
    assert args.runtime_version == DEFAULT_VERSION


def test_install_writes_where_it_is_told(monkeypatch, tmp_path):
    """No network: the download is stubbed, the destination logic is the thing tested."""
    import dax_quax.runtime as runtime

    seen = []

    def fake(package, version, dest):
        seen.append((package, version, dest))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"{package}.dll").write_bytes(b"x")
        return [f"{package}.dll"]

    monkeypatch.setattr(runtime, "fetch_package", fake)
    where, names = install(dest=tmp_path / "here", version="1.2.3")
    assert where == tmp_path / "here"
    assert len(names) == 2, "ADOMD and TOM by default"
    assert {v for _, v, _ in seen} == {"1.2.3"}


def test_no_tom_fetches_only_adomd(monkeypatch, tmp_path):
    import dax_quax.runtime as runtime

    packages = []
    monkeypatch.setattr(
        runtime,
        "fetch_package",
        lambda package, version, dest: (packages.append(package), [f"{package}.dll"])[1],
    )
    install(dest=tmp_path, with_tom=False)
    assert packages == [runtime.ADOMD_PACKAGE]


def test_runtime_imports_with_nothing_optional_installed():
    """It has to run before anything optional exists, so it is stdlib only."""
    source = pathlib.Path("src/dax_quax/runtime.py").read_text(encoding="utf-8")
    for package in ("pandas", "networkx", "pbixray", "fastapi", "clr"):
        assert f"import {package}" not in source


# -- offering to fetch on first use --------------------------------------------------


def test_the_env_var_has_three_states(monkeypatch):
    """Unset is not the same answer as 0: one means ask, the other means do not."""
    from dax_quax.runtime import AUTO_INSTALL_ENV, auto_install_allowed

    monkeypatch.delenv(AUTO_INSTALL_ENV, raising=False)
    assert auto_install_allowed() is None
    for yes in ("1", "true", "YES", " on "):
        monkeypatch.setenv(AUTO_INSTALL_ENV, yes)
        assert auto_install_allowed() is True, yes
    for no in ("0", "false", "NO", "off"):
        monkeypatch.setenv(AUTO_INSTALL_ENV, no)
        assert auto_install_allowed() is False, no
    monkeypatch.setenv(AUTO_INSTALL_ENV, "maybe")
    assert auto_install_allowed() is None, "an unreadable value must not mean yes"


def test_a_non_interactive_run_is_never_prompted(monkeypatch):
    """Prompting a build server is a hang that looks like a network stall."""
    from dax_quax.runtime import AUTO_INSTALL_ENV
    from dax_quax.sources import live

    monkeypatch.delenv(AUTO_INSTALL_ENV, raising=False)
    monkeypatch.setattr(live, "_asked_to_install", False)
    monkeypatch.setattr(live.sys, "stdin", None)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("should not have asked"))
    assert live._ask_to_install() is False


def test_the_env_var_answers_without_asking(monkeypatch):
    from dax_quax.runtime import AUTO_INSTALL_ENV
    from dax_quax.sources import live

    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("should not have asked"))
    monkeypatch.setenv(AUTO_INSTALL_ENV, "1")
    assert live._ask_to_install() is True
    monkeypatch.setenv(AUTO_INSTALL_ENV, "0")
    assert live._ask_to_install() is False


def test_an_interactive_run_is_asked_once(monkeypatch, capsys):
    from dax_quax.runtime import AUTO_INSTALL_ENV
    from dax_quax.sources import live

    class Tty:
        def isatty(self):
            return True

    monkeypatch.delenv(AUTO_INSTALL_ENV, raising=False)
    monkeypatch.setattr(live, "_asked_to_install", False)
    monkeypatch.setattr(live.sys, "stdin", Tty())
    asked = []
    monkeypatch.setattr("builtins.input", lambda *_: (asked.append(1), "y")[1])

    assert live._ask_to_install() is True
    assert "nuget.org" in capsys.readouterr().out
    # A second attempt must not re-prompt: a loop of questions is worse than the error.
    assert live._ask_to_install() is False
    assert len(asked) == 1


def test_declining_leaves_the_original_error(monkeypatch, tmp_path):
    """Saying no should read exactly like never being asked."""
    from dax_quax.errors import DaxQuaxError
    from dax_quax.runtime import AUTO_INSTALL_ENV
    from dax_quax.sources import live

    monkeypatch.setenv(AUTO_INSTALL_ENV, "0")
    monkeypatch.setenv("DAXQUAX_ADOMD_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr("dax_quax.runtime.user_runtime_dir", lambda: tmp_path / "neither")
    monkeypatch.setattr(live, "__file__", str(tmp_path / "a" / "b" / "live.py"))

    with pytest.raises(DaxQuaxError, match="install-runtime"):
        live._locate_runtime()


def test_accepting_fetches_then_finds_it(monkeypatch, tmp_path):
    from dax_quax.runtime import ADOMD_DLL, AUTO_INSTALL_ENV
    from dax_quax.sources import live

    target = tmp_path / "runtime"
    monkeypatch.setenv(AUTO_INSTALL_ENV, "1")
    monkeypatch.delenv("DAXQUAX_ADOMD_DIR", raising=False)
    monkeypatch.setattr("dax_quax.runtime.user_runtime_dir", lambda: target)
    monkeypatch.setattr(live, "__file__", str(tmp_path / "a" / "b" / "live.py"))

    def fake_install(**_):
        target.mkdir(parents=True, exist_ok=True)
        (target / ADOMD_DLL).write_bytes(b"x")
        return target, [ADOMD_DLL]

    monkeypatch.setattr("dax_quax.runtime.install", fake_install)
    assert live._locate_runtime() == target


def test_a_present_runtime_is_not_re_fetched(monkeypatch, tmp_path):
    from dax_quax.runtime import ADOMD_DLL, AUTO_INSTALL_ENV
    from dax_quax.sources import live

    here = tmp_path / "already"
    here.mkdir()
    (here / ADOMD_DLL).write_bytes(b"x")
    monkeypatch.setenv(AUTO_INSTALL_ENV, "1")
    monkeypatch.setenv("DAXQUAX_ADOMD_DIR", str(here))
    monkeypatch.setattr(
        "dax_quax.runtime.install", lambda **_: pytest.fail("should not have fetched")
    )
    assert live._locate_runtime() == here
