"""代理池模型 - 用于管理代理IP供爬虫使用"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, Float
from app.database import Base


class ProxyPool(Base):
    """代理IP池"""
    __tablename__ = "proxy_pool"

    id = Column(Integer, primary_key=True, index=True)
    ip = Column(String(100), nullable=False)  # IP地址或域名
    port = Column(Integer, nullable=False)  # 端口
    protocol = Column(String(10), default="http")  # 协议: http/https/socks5
    proxy_type = Column(String(20), default="direct")  # 代理类型: direct/tunnel
    
    # 认证信息（隧道代理需要）
    username = Column(String(100))  # 用户名
    password = Column(String(500))  # 密码（加密存储）
    
    # 来源信息
    source = Column(String(50), default="manual")  # 来源: kuaidaili/manual/other
    area = Column(String(100))  # 地区
    
    # 状态
    is_active = Column(Boolean, default=True)  # 是否启用
    is_valid = Column(Boolean, default=True)  # 是否有效（验证通过）
    success_count = Column(Integer, default=0)  # 成功使用次数
    fail_count = Column(Integer, default=0)  # 连续失败次数
    total_fail_count = Column(Integer, default=0)  # 总失败次数
    response_time = Column(Float)  # 响应时间(ms)
    
    # 时间
    last_used_at = Column(DateTime)  # 最后使用时间
    last_check_at = Column(DateTime)  # 最后验证时间
    expire_at = Column(DateTime)  # 过期时间
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 备注
    remark = Column(Text)

    @property
    def proxy_url(self) -> str:
        """返回代理URL格式"""
        return f"{self.protocol}://{self.ip}:{self.port}"
    
    @property
    def proxy_url_with_auth(self) -> str:
        """返回带认证的代理URL格式"""
        if self.username and self.password:
            from app.services.crypto import decrypt_password
            try:
                pwd = decrypt_password(self.password)
                return f"{self.protocol}://{self.username}:{pwd}@{self.ip}:{self.port}"
            except:
                pass
        return self.proxy_url
    
    @property
    def proxy_dict(self) -> dict:
        """返回requests/httpx使用的代理字典"""
        url = self.proxy_url
        return {"http://": url, "https://": url}


class ProxyConfig(Base):
    """代理配置 - 存储快代理等服务的API配置"""
    __tablename__ = "proxy_config"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)  # 配置名称
    provider = Column(String(50), nullable=False)  # 提供商: kuaidaili
    
    # API配置
    api_url = Column(String(500))  # API地址
    secret_id = Column(String(200))  # SecretId
    secret_key = Column(String(200))  # SecretKey (加密存储)
    
    # 提取配置
    fetch_num = Column(Integer, default=10)  # 每次提取数量
    fetch_interval = Column(Integer, default=60)  # 提取间隔(秒)
    protocol = Column(String(10), default="http")  # 代理协议
    area = Column(String(200))  # 地区筛选
    
    # 状态
    is_active = Column(Boolean, default=True)
    last_fetch_at = Column(DateTime)  # 最后提取时间
    
    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
