# -*- coding: utf-8 -*-
r"""
apppaths.py — where things live, in the repo AND inside a frozen desktop build.

Three kinds of location, deliberately separate:

  bundle_root()   read-only program assets: config/config.js, static/…
                  · repo checkout   → the LarkTunnel/ folder
                  · PyInstaller exe → sys._MEIPASS (the unpacked bundle)
  data_dir()      per-USER private state that must never sit in a shared
                  program folder: settings.json, DPAPI secrets.bin
                  · %APPDATA%\LarkTunnel  (override: LARK_HOME — tests use it)
  state_path()    machine state the tool writes as it runs (logs/audit.db,
                  logs/ops.jsonl, config/operators-auto.json)
                  · repo checkout   → the repo (git-ignored, as before)
                  · PyInstaller exe → data_dir()   (never inside the bundle)

A teammate running LarkTunnel.exe therefore never writes into the program
folder, and the owner's dev checkout keeps behaving exactly as it did.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_frozen():
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def bundle_root():
    return sys._MEIPASS if is_frozen() else _REPO_ROOT


def repo_root():
    """The source checkout — only meaningful when NOT frozen (dev machine)."""
    return _REPO_ROOT


def exe_dir():
    """Folder holding LarkTunnel.exe (frozen) or the repo root (dev)."""
    return os.path.dirname(os.path.abspath(sys.executable)) if is_frozen() else _REPO_ROOT


def data_dir():
    home = os.environ.get("LARK_HOME")
    if not home:
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        home = os.path.join(appdata, "LarkTunnel")
    os.makedirs(home, exist_ok=True)
    return home


def data_path(*parts):
    """A file path under data_dir(); its parent folder is created."""
    p = os.path.join(data_dir(), *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def state_path(*parts):
    """Machine-written state: repo-relative on a dev checkout, data_dir when
    frozen. Parent folder is created."""
    root = data_dir() if is_frozen() else _REPO_ROOT
    p = os.path.join(root, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p
