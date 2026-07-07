"""加密服务 - 用于账号密码的加密存储"""
import base64
import hashlib
from cryptography.fernet import Fernet
from app.config import config

# 从配置获取加密密钥，如果没有则使用 DATABASE_URL 派生一个
# 生产环境必须设置 ENCRYPTION_KEY 环境变量
_ENCRYPTION_KEY = config.ENCRYPTION_KEY

if not _ENCRYPTION_KEY:
    # 使用 DATABASE_URL 的哈希作为默认密钥（不推荐生产使用）
    key_source = config.DATABASE_URL.encode()
    key_hash = hashlib.sha256(key_source).digest()
    _ENCRYPTION_KEY = base64.urlsafe_b64encode(key_hash)
else:
    # 确保密钥是正确的格式
    if len(_ENCRYPTION_KEY) == 32:
        _ENCRYPTION_KEY = base64.urlsafe_b64encode(_ENCRYPTION_KEY.encode())
    else:
        _ENCRYPTION_KEY = _ENCRYPTION_KEY.encode()

_fernet = Fernet(_ENCRYPTION_KEY)


def encrypt_password(password: str) -> str:
    """加密密码"""
    encrypted = _fernet.encrypt(password.encode())
    return encrypted.decode()


def decrypt_password(encrypted_password: str) -> str:
    """解密密码"""
    decrypted = _fernet.decrypt(encrypted_password.encode())
    return decrypted.decode()


def generate_encryption_key() -> str:
    """生成新的加密密钥（用于初始化）"""
    return Fernet.generate_key().decode()
