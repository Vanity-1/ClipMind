"""
敏感字段加密工具

用于在数据库中加密存储 Cookie / Token 等敏感字段（sessdata、bili_jct、douyin_cookie 等）。

设计要点：
1. 使用 Fernet 对称加密（AES-128-CBC + HMAC-SHA256），密钥为 32 字节 base64 urlsafe。
2. 密钥来源（按优先级）：
   a) 环境变量 COOKIE_ENCRYPTION_KEY（生产环境推荐）
   b) data/cookie_key.key 文件（开发环境自动生成并持久化）
3. 加密后的值加前缀 ``enc::``，便于与历史明文数据共存。
4. ``decrypt_secret`` 兼容明文：不以 ``enc::`` 开头的值视为明文原样返回，
   保证存量数据平滑迁移；首次写入时会自动重新加密回写。
5. 加解密失败时一律返回原值并记录 warning，避免登录态完全失效。
"""
from __future__ import annotations

import os
from typing import Optional

from loguru import logger

from app.config import settings


_PREFIX = "enc::"
_fernet = None
_init_error: Optional[str] = None


def _get_key_file() -> str:
    """返回 cookie 加密密钥文件路径，跟随 CLIPMIND_DATA_DIR"""
    from app.config import _data_dir
    return os.path.join(str(_data_dir()), "cookie_key.key")


def _load_or_create_key() -> bytes:
    """加载或生成 Fernet 密钥。"""
    # 1. 优先使用环境变量
    env_key = (settings.cookie_encryption_key or "").strip()
    if env_key:
        return env_key.encode("utf-8")

    # 2. 生产环境强制要求环境变量，拒绝自动生成（避免多实例密钥不一致 / 容器重建丢失密钥）
    if not settings.debug:
        raise RuntimeError(
            "[Crypto] 生产环境（DEBUG=False）必须通过环境变量 COOKIE_ENCRYPTION_KEY "
            "显式提供加密密钥，拒绝自动生成。请在 .env 中配置该变量。"
        )

    # 3. 从文件加载（仅开发环境）
    if os.path.exists(_get_key_file()):
        with open(_get_key_file(), "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key.encode("utf-8")

    # 4. 生成新密钥并持久化（仅开发环境）
    from cryptography.fernet import Fernet
    new_key = Fernet.generate_key().decode("utf-8")
    os.makedirs(os.path.dirname(_get_key_file()), exist_ok=True)
    # 0600 权限，仅当前用户可读写
    fd = os.open(_get_key_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, new_key.encode("utf-8"))
    finally:
        os.close(fd)
    logger.warning(
        "[Crypto] 未配置 COOKIE_ENCRYPTION_KEY，已自动生成密钥并写入 "
        f"{_get_key_file()}。生产环境请通过环境变量显式注入密钥。"
    )
    return new_key.encode("utf-8")


def _get_fernet():
    """惰性初始化 Fernet 单例。"""
    global _fernet, _init_error
    if _fernet is not None:
        return _fernet
    if _init_error is not None:
        # 初始化已失败过，不再重试，避免日志爆炸
        return None
    try:
        from cryptography.fernet import Fernet
        key = _load_or_create_key()
        _fernet = Fernet(key)
        logger.info("[Crypto] Fernet 初始化成功，敏感字段将加密存储")
        return _fernet
    except Exception as e:
        _init_error = str(e)
        # 生产环境未配置密钥：输出明确的 ERROR 提示（保留 _load_or_create_key 抛
        # RuntimeError 拒绝自动生成的安全行为，仅在日志消息上更明确）
        env_key = (settings.cookie_encryption_key or "").strip()
        if not env_key and not settings.debug:
            logger.error(
                "[Crypto] 生产环境未配置 COOKIE_ENCRYPTION_KEY，cookie 将明文存储！"
                "请在 .env 中配置该变量"
            )
        else:
            logger.error(f"[Crypto] Fernet 初始化失败，敏感字段将明文存储: {e}")
        return None


def encrypt_secret(plaintext: Optional[str]) -> Optional[str]:
    """加密敏感字段。

    - None / 空字符串原样返回
    - 已加密（带前缀）原样返回，避免重复加密
    - 加密失败时返回明文，保证流程不中断
    """
    if not plaintext:
        return plaintext
    if plaintext.startswith(_PREFIX):
        # 已加密，幂等返回
        return plaintext
    f = _get_fernet()
    if f is None:
        return plaintext
    try:
        token = f.encrypt(plaintext.encode("utf-8")).decode("utf-8")
        return f"{_PREFIX}{token}"
    except Exception as e:
        logger.warning(f"[Crypto] 加密失败，将以明文存储: {e}")
        return plaintext


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    """解密敏感字段。

    - None / 空字符串原样返回
    - 不带前缀视为历史明文，原样返回（向后兼容）
    - 解密失败时返回原值，避免登录态完全失效
    """
    if not value:
        return value
    if not value.startswith(_PREFIX):
        # 历史明文，原样返回
        return value
    f = _get_fernet()
    if f is None:
        # 密钥不可用，返回去掉前缀的原文（无法解密，但避免泄漏前缀）
        return value[len(_PREFIX):]
    token = value[len(_PREFIX):]
    try:
        return f.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.warning(f"[Crypto] 解密失败，返回原值: {e}")
        return value


def is_encrypted(value: Optional[str]) -> bool:
    """判断值是否已加密。"""
    return bool(value) and value.startswith(_PREFIX)
