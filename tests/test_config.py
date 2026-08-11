import os
import sys

# 让测试能 import 仓库根目录的 main 模块
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import main


def _clear_mail_env(monkeypatch):
    for var in [
        "MAIL_HOST", "MAIL_SERVER", "MAIL_USER", "MAIL_USERNAME",
        "MAIL_PASSWORD", "MAIL_PORT", "OPENAI_API_KEY",
    ]:
        monkeypatch.delenv(var, raising=False)


def test_valid_config_passes(monkeypatch):
    _clear_mail_env(monkeypatch)
    monkeypatch.setenv("MAIL_HOST", "smtp.example.com")
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USER", "bot@example.com")
    monkeypatch.setenv("MAIL_PASSWORD", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")
    main.validate_config()  # 不应抛出


def test_missing_mail_host(monkeypatch):
    _clear_mail_env(monkeypatch)
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USER", "bot@example.com")
    monkeypatch.setenv("MAIL_PASSWORD", "secret")
    with pytest.raises(main.ConfigError):
        main.validate_config()


def test_missing_mail_user(monkeypatch):
    _clear_mail_env(monkeypatch)
    monkeypatch.setenv("MAIL_HOST", "smtp.example.com")
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_PASSWORD", "secret")
    with pytest.raises(main.ConfigError):
        main.validate_config()


def test_missing_mail_password(monkeypatch):
    _clear_mail_env(monkeypatch)
    monkeypatch.setenv("MAIL_HOST", "smtp.example.com")
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USER", "bot@example.com")
    with pytest.raises(main.ConfigError):
        main.validate_config()


def test_invalid_mail_port_raises(monkeypatch):
    """需求 5：非法 MAIL_PORT（非整数）必须导致校验失败。"""
    _clear_mail_env(monkeypatch)
    monkeypatch.setenv("MAIL_HOST", "smtp.example.com")
    monkeypatch.setenv("MAIL_PORT", "not-a-number")
    monkeypatch.setenv("MAIL_USER", "bot@example.com")
    monkeypatch.setenv("MAIL_PASSWORD", "secret")
    with pytest.raises(main.ConfigError):
        main.validate_config()


def test_invalid_mail_port_empty(monkeypatch):
    _clear_mail_env(monkeypatch)
    monkeypatch.setenv("MAIL_HOST", "smtp.example.com")
    monkeypatch.setenv("MAIL_PORT", "")
    monkeypatch.setenv("MAIL_USER", "bot@example.com")
    monkeypatch.setenv("MAIL_PASSWORD", "secret")
    with pytest.raises(main.ConfigError):
        main.validate_config()


def test_openai_key_optional(monkeypatch):
    _clear_mail_env(monkeypatch)
    monkeypatch.setenv("MAIL_HOST", "smtp.example.com")
    monkeypatch.setenv("MAIL_PORT", "587")
    monkeypatch.setenv("MAIL_USER", "bot@example.com")
    monkeypatch.setenv("MAIL_PASSWORD", "secret")
    # 无 OPENAI_API_KEY 不应报错（仅警告）
    main.validate_config()


def test_legacy_alias_mail_server_accepted(monkeypatch):
    """已部署工作流使用旧名 MAIL_SERVER / MAIL_USERNAME，应被兼容接受。"""
    _clear_mail_env(monkeypatch)
    monkeypatch.setenv("MAIL_SERVER", "smtp.example.com")    # 旧名
    monkeypatch.setenv("MAIL_PORT", "465")
    monkeypatch.setenv("MAIL_USERNAME", "bot@example.com")   # 旧名
    monkeypatch.setenv("MAIL_PASSWORD", "secret")
    main.validate_config()  # 不应抛出
