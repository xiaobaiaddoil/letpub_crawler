"""代理服务 - 管理代理池（支持隧道代理和私密代理）"""
import logging
import re
import time
import httpx
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Dict
from urllib.parse import unquote, urlparse
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.proxy_pool import ProxyPool, ProxyConfig
from app.services.crypto import encrypt_password, decrypt_password
from app.config import config

logger = logging.getLogger(__name__)


@dataclass
class ParsedProxy:
    ip: str
    port: int
    protocol: str
    username: Optional[str] = None
    password: Optional[str] = None


def parse_proxy_text_line(
    line: str,
    default_protocol: str = "http",
    default_username: str | None = None,
    default_password: str | None = None,
) -> ParsedProxy:
    """Parse one proxy text line.

    Supported formats:
    - host:port
    - host:port:username:password
    - username:password@host:port
    - http://username:password@host:port
    - host:port username password
    """
    raw = (line or "").strip()
    if not raw:
        raise ValueError("空行")
    if raw.startswith("#"):
        raise ValueError("注释行")

    protocol = default_protocol or "http"
    username = default_username or None
    password = default_password or None

    if "://" in raw:
        parsed = urlparse(raw)
        if not parsed.hostname or not parsed.port:
            raise ValueError("URL格式应包含 host:port")
        protocol = parsed.scheme or protocol
        username = unquote(parsed.username) if parsed.username else username
        password = unquote(parsed.password) if parsed.password else password
        return ParsedProxy(
            ip=parsed.hostname,
            port=parsed.port,
            protocol=protocol,
            username=username,
            password=password,
        )

    tokens = [part for part in re.split(r"[\s,|]+", raw) if part]
    head = tokens[0]
    if len(tokens) >= 3:
        username = tokens[1]
        password = tokens[2]

    if "@" in head:
        auth, host_port = head.rsplit("@", 1)
        if ":" in auth:
            username, password = auth.split(":", 1)
        else:
            username = auth
    else:
        host_port = head

    parts = host_port.split(":")
    if len(parts) >= 4:
        host = parts[0]
        port_text = parts[1]
        username = parts[2]
        password = ":".join(parts[3:])
    elif len(parts) == 2:
        host, port_text = parts
    else:
        raise ValueError("格式应为 host:port 或 host:port:username:password")

    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("端口不是数字") from exc
    if not host or port <= 0 or port > 65535:
        raise ValueError("host或端口无效")

    return ParsedProxy(
        ip=host,
        port=port,
        protocol=protocol,
        username=username or None,
        password=password or None,
    )


class ProxyService:
    """代理服务"""
    
    # 代理验证URL
    CHECK_URL = "https://dev.kdlapi.com/testproxy"
    CHECK_TIMEOUT = 15
    
    # 失败阈值
    MAX_FAIL_COUNT = 3
    
    def __init__(self, db: Session):
        self.db = db
    
    async def get_proxy(self) -> Optional[ProxyPool]:
        """获取一个可用代理（优先选择成功率高的）"""
        proxy = self.db.query(ProxyPool).filter(
            ProxyPool.is_active == True,
            ProxyPool.is_valid == True,
            ProxyPool.fail_count < self.MAX_FAIL_COUNT
        ).order_by(
            ProxyPool.fail_count.asc(),
            ProxyPool.success_count.desc(),
            ProxyPool.last_used_at.asc().nullsfirst()
        ).first()
        
        if proxy:
            proxy.last_used_at = datetime.now(timezone.utc)
            self.db.commit()
        
        return proxy
    
    async def get_random_proxy(self) -> Optional[ProxyPool]:
        """随机获取一个可用代理"""
        # 先统计各状态的代理数量
        total = self.db.query(ProxyPool).count()
        active = self.db.query(ProxyPool).filter(ProxyPool.is_active == True).count()
        valid = self.db.query(ProxyPool).filter(
            ProxyPool.is_active == True,
            ProxyPool.is_valid == True
        ).count()
        available = self.db.query(ProxyPool).filter(
            ProxyPool.is_active == True,
            ProxyPool.is_valid == True,
            ProxyPool.fail_count < self.MAX_FAIL_COUNT
        ).count()
        
        if available == 0:
            logger.warning(f"[代理池] 无可用代理 - 总数:{total}, 启用:{active}, 有效:{valid}, 可用:{available}")
        
        proxy = self.db.query(ProxyPool).filter(
            ProxyPool.is_active == True,
            ProxyPool.is_valid == True,
            ProxyPool.fail_count < self.MAX_FAIL_COUNT
        ).order_by(func.random()).first()
        
        if proxy:
            proxy.last_used_at = datetime.now(timezone.utc)
            self.db.commit()
        
        return proxy
    
    def report_proxy_result(self, proxy_id: int, success: bool):
        """报告代理使用结果。

        source='clash' 的条目代表 mihomo load-balance 入口，
        节点健康检查由 mihomo 内核负责，应用层不打分、不下架。
        """
        proxy = self.db.query(ProxyPool).filter(ProxyPool.id == proxy_id).first()
        if not proxy:
            return

        if proxy.source == "clash":
            if success:
                proxy.success_count += 1
            self.db.commit()
            return

        if success:
            proxy.success_count += 1
            proxy.fail_count = 0
        else:
            proxy.fail_count += 1
            proxy.total_fail_count += 1
            # 失败一次即标记为无效
            proxy.is_valid = False
            logger.warning(f"[代理] {proxy.ip}:{proxy.port} 请求失败，已标记无效")

        self.db.commit()

    async def check_proxy(self, proxy: ProxyPool) -> bool:
        """验证代理是否可用"""
        if proxy.source == "clash":
            proxy.is_valid = True
            proxy.last_check_at = datetime.now(timezone.utc)
            self.db.commit()
            logger.info(f"[代理] Clash入口 {proxy.ip}:{proxy.port} 跳过应用层外网探测")
            return True

        try:
            start_time = time.time()
            
            # 构建代理URL
            if proxy.username and proxy.password:
                try:
                    pwd = decrypt_password(proxy.password)
                    proxy_url = f"http://{proxy.username}:{pwd}@{proxy.ip}:{proxy.port}"
                except:
                    proxy_url = f"http://{proxy.ip}:{proxy.port}"
            else:
                proxy_url = f"http://{proxy.ip}:{proxy.port}"
            
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=self.CHECK_TIMEOUT,
                verify=False
            ) as client:
                response = await client.get(self.CHECK_URL)
                
                if response.status_code == 200:
                    response_time = (time.time() - start_time) * 1000
                    proxy.response_time = response_time
                    proxy.is_valid = True
                    proxy.last_check_at = datetime.now(timezone.utc)
                    self.db.commit()
                    logger.info(f"代理验证成功: {proxy.ip}:{proxy.port}, 响应: {response_time:.0f}ms")
                    return True
            
            proxy.is_valid = False
            self.db.commit()
            return False
        
        except Exception as e:
            logger.warning(f"代理验证失败 {proxy.ip}:{proxy.port}: {e}")
            proxy.is_valid = False
            self.db.commit()
            return False
    
    async def check_all_proxies(self) -> Dict[str, int]:
        """验证所有代理"""
        proxies = self.db.query(ProxyPool).filter(ProxyPool.is_active == True).all()
        
        valid = 0
        invalid = 0
        
        for proxy in proxies:
            if await self.check_proxy(proxy):
                valid += 1
            else:
                invalid += 1
        
        return {"valid": valid, "invalid": invalid, "total": len(proxies)}
    
    def add_proxy(self, ip: str, port: int, protocol: str = "http",
                  source: str = "manual", proxy_type: str = "direct",
                  username: str = None, password: str = None,
                  remark: str = None) -> ProxyPool:
        """手动添加代理"""
        existing = self.db.query(ProxyPool).filter(
            ProxyPool.ip == ip,
            ProxyPool.port == port
        ).first()
        
        if existing:
            existing.is_active = True
            existing.is_valid = True
            existing.fail_count = 0
            existing.protocol = protocol
            existing.source = source
            existing.proxy_type = proxy_type
            if username:
                existing.username = username
            if password:
                existing.password = encrypt_password(password)
            if remark:
                existing.remark = remark
            self.db.commit()
            return existing
        
        proxy = ProxyPool(
            ip=ip,
            port=port,
            protocol=protocol,
            source=source,
            proxy_type=proxy_type,
            username=username,
            password=encrypt_password(password) if password else None,
            remark=remark
        )
        self.db.add(proxy)
        self.db.commit()
        return proxy

    def import_proxies_from_text(
        self,
        text: str,
        protocol: str = "http",
        source: str = "manual",
        proxy_type: str = "private",
        username: str | None = None,
        password: str | None = None,
        remark: str | None = None,
    ) -> Dict:
        """Import proxies from pasted text."""
        added = 0
        updated = 0
        skipped = 0
        errors = []

        for line_number, line in enumerate((text or "").splitlines(), start=1):
            if not line.strip() or line.strip().startswith("#"):
                skipped += 1
                continue
            try:
                parsed = parse_proxy_text_line(
                    line,
                    default_protocol=protocol,
                    default_username=username,
                    default_password=password,
                )
                existed = self.db.query(ProxyPool).filter(
                    ProxyPool.ip == parsed.ip,
                    ProxyPool.port == parsed.port,
                ).first() is not None
                self.add_proxy(
                    ip=parsed.ip,
                    port=parsed.port,
                    protocol=parsed.protocol,
                    source=source,
                    proxy_type=proxy_type,
                    username=parsed.username,
                    password=parsed.password,
                    remark=remark,
                )
                if existed:
                    updated += 1
                else:
                    added += 1
            except Exception as exc:
                errors.append({
                    "line": line_number,
                    "content": line.strip(),
                    "error": str(exc),
                })

        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "failed": len(errors),
            "errors": errors[:50],
        }

    def export_proxies_as_text(
        self,
        fmt: str = "hostport_auth",
        only_active: bool = True,
        only_valid: bool = False,
        include_auth: bool = True,
    ) -> str:
        """Export proxy pool as plain text."""
        query = self.db.query(ProxyPool)
        if only_active:
            query = query.filter(ProxyPool.is_active == True)
        if only_valid:
            query = query.filter(ProxyPool.is_valid == True)

        lines = []
        for proxy in query.order_by(ProxyPool.id.asc()).all():
            password = None
            if include_auth and proxy.password:
                try:
                    password = decrypt_password(proxy.password)
                except Exception:
                    password = None
            username = proxy.username if include_auth else None
            auth_available = bool(username and password)

            if fmt == "url":
                if auth_available:
                    lines.append(f"{proxy.protocol}://{username}:{password}@{proxy.ip}:{proxy.port}")
                else:
                    lines.append(f"{proxy.protocol}://{proxy.ip}:{proxy.port}")
            elif fmt == "hostport":
                lines.append(f"{proxy.ip}:{proxy.port}")
            else:
                if auth_available:
                    lines.append(f"{proxy.ip}:{proxy.port}:{username}:{password}")
                else:
                    lines.append(f"{proxy.ip}:{proxy.port}")

        return "\n".join(lines)
    
    def add_tunnel_proxy(self, tunnel: str, username: str = None, password: str = None, 
                         remark: str = None) -> ProxyPool:
        """添加隧道代理（固定地址）
        
        Args:
            tunnel: 隧道地址 host:port
            username: 用户名（白名单模式可为空）
            password: 密码（白名单模式可为空）
            remark: 备注
        """
        if ":" in tunnel:
            host, port = tunnel.rsplit(":", 1)
            port = int(port)
        else:
            raise ValueError("隧道地址格式错误，应为 host:port")
        
        return self.add_proxy(
            ip=host,
            port=port,
            protocol="http",
            source="kuaidaili",
            proxy_type="tunnel",
            username=username,
            password=password,
            remark=remark or "隧道代理"
        )
    
    async def fetch_private_proxies(self, api_url: str, username: str, password: str,
                                    remark: str = None) -> int:
        """从API获取私密代理IP并批量添加到池中
        
        Args:
            api_url: 提取代理的API地址
            username: 认证用户名
            password: 认证密码
            remark: 备注
            
        Returns:
            添加的代理数量
        """
        try:
            # 确保使用 JSON 格式
            if "format=json" not in api_url:
                if "?" in api_url:
                    api_url += "&format=json"
                else:
                    api_url += "?format=json"
            
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(api_url)
                
                # 尝试解析 JSON
                try:
                    data = response.json()
                    
                    if data.get("code") != 0:
                        logger.error(f"获取私密代理失败: {data.get('msg')}")
                        return 0
                    
                    proxy_list = data.get("data", {}).get("proxy_list", [])
                    if not proxy_list:
                        logger.error("私密代理列表为空")
                        return 0
                    
                except:
                    # 如果不是 JSON，按文本解析（每行一个）
                    proxy_list = response.text.strip().split('\n')
                
                # 批量添加所有代理
                added = 0
                for proxy_str in proxy_list:
                    proxy_str = proxy_str.strip()
                    if not proxy_str or "ERROR" in proxy_str:
                        continue
                    
                    # 解析 IP:PORT
                    if ":" not in proxy_str:
                        logger.warning(f"私密代理格式错误: {proxy_str}")
                        continue
                    
                    try:
                        last_colon = proxy_str.rfind(":")
                        ip = proxy_str[:last_colon]
                        port = int(proxy_str[last_colon + 1:])
                        
                        # 添加到代理池
                        self.add_proxy(
                            ip=ip,
                            port=port,
                            protocol="http",
                            source="kuaidaili",
                            proxy_type="private",
                            username=username,
                            password=password,
                            remark=remark or "快代理私密"
                        )
                        added += 1
                        logger.info(f"添加私密代理: {ip}:{port}")
                        
                    except Exception as e:
                        logger.warning(f"解析代理失败 {proxy_str}: {e}")
                        continue
                
                logger.info(f"批量获取私密代理完成，共添加 {added} 个")
                return added
                
        except Exception as e:
            logger.error(f"获取私密代理失败: {e}")
            return 0
    
    def delete_proxy(self, proxy_id: int) -> bool:
        """删除代理"""
        proxy = self.db.query(ProxyPool).filter(ProxyPool.id == proxy_id).first()
        if proxy:
            self.db.delete(proxy)
            self.db.commit()
            return True
        return False
    
    def toggle_proxy(self, proxy_id: int) -> Optional[ProxyPool]:
        """切换代理启用状态"""
        proxy = self.db.query(ProxyPool).filter(ProxyPool.id == proxy_id).first()
        if proxy:
            proxy.is_active = not proxy.is_active
            self.db.commit()
        return proxy
    
    def clear_invalid_proxies(self) -> int:
        """清理无效代理"""
        count = self.db.query(ProxyPool).filter(ProxyPool.is_valid == False).delete()
        self.db.commit()
        return count
    
    def get_stats(self) -> Dict:
        """获取代理池统计"""
        total = self.db.query(ProxyPool).count()
        active = self.db.query(ProxyPool).filter(ProxyPool.is_active == True).count()
        valid = self.db.query(ProxyPool).filter(
            ProxyPool.is_active == True,
            ProxyPool.is_valid == True
        ).count()
        tunnel_count = self.db.query(ProxyPool).filter(
            ProxyPool.proxy_type == "tunnel",
            ProxyPool.is_active == True
        ).count()
        private_count = self.db.query(ProxyPool).filter(
            ProxyPool.proxy_type == "private",
            ProxyPool.is_active == True
        ).count()
        
        # 使用统计
        total_success = self.db.query(func.sum(ProxyPool.success_count)).scalar() or 0
        total_fail = self.db.query(func.sum(ProxyPool.total_fail_count)).scalar() or 0
        total_requests = total_success + total_fail
        success_rate = round(total_success / total_requests * 100, 1) if total_requests > 0 else 0
        
        # 平均响应时间
        avg_response = self.db.query(func.avg(ProxyPool.response_time)).filter(
            ProxyPool.response_time.isnot(None)
        ).scalar() or 0
        
        return {
            "total": total,
            "active": active,
            "valid": valid,
            "invalid": active - valid,
            "tunnel": tunnel_count,
            "private": private_count,
            "total_success": int(total_success),
            "total_fail": int(total_fail),
            "total_requests": int(total_requests),
            "success_rate": success_rate,
            "avg_response_time": round(avg_response, 0) if avg_response else 0
        }
    
    # ========== 配置管理 ==========
    
    def add_config(self, name: str, provider: str, proxy_type: str,
                   api_url: str = None, tunnel_addr: str = None,
                   username: str = None, password: str = None,
                   fetch_num: int = 10, auto_refresh: bool = False,
                   refresh_interval: int = 300) -> ProxyConfig:
        """添加代理配置
        
        Args:
            name: 配置名称
            provider: 提供商（kuaidaili）
            proxy_type: 代理类型（tunnel/private）
            api_url: 私密代理API地址
            tunnel_addr: 隧道地址
            username: 认证用户名
            password: 认证密码
            fetch_num: 每次获取数量
            auto_refresh: 是否自动刷新
            refresh_interval: 刷新间隔（秒）
        """
        proxy_config = ProxyConfig(
            name=name,
            provider=provider,
            protocol=proxy_type,  # tunnel 或 private
            api_url=api_url,
            area=tunnel_addr,  # 复用存储隧道地址
            secret_id=username,
            fetch_num=fetch_num,
            fetch_interval=refresh_interval,
            is_active=auto_refresh,  # 复用为自动刷新开关
        )
        
        if password:
            proxy_config.secret_key = encrypt_password(password)
        
        self.db.add(proxy_config)
        self.db.commit()
        return proxy_config
    
    def update_config(self, config_id: int, **kwargs) -> Optional[ProxyConfig]:
        """更新代理配置"""
        proxy_config = self.db.query(ProxyConfig).filter(
            ProxyConfig.id == config_id
        ).first()
        
        if not proxy_config:
            return None
        
        field_mapping = {
            'proxy_type': 'protocol',
            'tunnel_addr': 'area',
            'username': 'secret_id',
            'refresh_interval': 'fetch_interval',
            'auto_refresh': 'is_active',
        }
        
        for key, value in kwargs.items():
            if key == 'password' and value:
                proxy_config.secret_key = encrypt_password(value)
            else:
                db_field = field_mapping.get(key, key)
                if hasattr(proxy_config, db_field):
                    setattr(proxy_config, db_field, value)
        
        self.db.commit()
        return proxy_config

    async def fetch_from_config(self, config_id: int) -> int:
        """根据配置获取代理"""
        config = self.db.query(ProxyConfig).filter(ProxyConfig.id == config_id).first()
        if not config:
            return 0
        
        proxy_type = config.protocol  # tunnel 或 private
        username = config.secret_id
        password = decrypt_password(config.secret_key) if config.secret_key else None
        
        if not username or not password:
            logger.warning(f"配置 {config.name} 缺少用户名或密码")
            return 0
        
        count = 0
        
        if proxy_type == "tunnel":
            # 隧道代理：直接添加固定地址
            tunnel_addr = config.area
            if tunnel_addr:
                self.add_tunnel_proxy(tunnel_addr, username, password, config.name)
                count = 1
        
        elif proxy_type == "private":
            # 私密代理：从API批量获取
            api_url = config.api_url
            if api_url:
                # 替换 num 参数
                if "num=" in api_url and config.fetch_num:
                    import re
                    api_url = re.sub(r'num=\d+', f'num={config.fetch_num}', api_url)
                
                count = await self.fetch_private_proxies(api_url, username, password, config.name)
        
        if count > 0:
            config.last_fetch_at = datetime.now(timezone.utc)
            self.db.commit()
            logger.info(f"配置 {config.name} 获取了 {count} 个代理")
        
        return count
    
    async def auto_refresh_proxies(self) -> Dict[str, int]:
        """自动刷新所有启用自动刷新的配置"""
        configs = self.db.query(ProxyConfig).filter(
            ProxyConfig.is_active == True  # is_active 表示自动刷新开关
        ).all()
        
        total_added = 0
        refreshed_configs = 0
        
        for config in configs:
            # 检查是否需要刷新
            if config.last_fetch_at:
                elapsed = (datetime.now(timezone.utc) - config.last_fetch_at.replace(tzinfo=timezone.utc)).total_seconds()
                if elapsed < config.fetch_interval:
                    continue  # 还没到刷新时间
            
            count = await self.fetch_from_config(config.id)
            if count > 0:
                total_added += count
                refreshed_configs += 1
        
        return {"refreshed_configs": refreshed_configs, "added_proxies": total_added}
    
    def delete_config(self, config_id: int) -> bool:
        """删除代理配置"""
        proxy_config = self.db.query(ProxyConfig).filter(
            ProxyConfig.id == config_id
        ).first()
        if proxy_config:
            self.db.delete(proxy_config)
            self.db.commit()
            return True
        return False
    
    def get_configs(self) -> List[ProxyConfig]:
        """获取所有代理配置"""
        return self.db.query(ProxyConfig).all()

    async def init_from_env(self) -> Dict[str, int]:
        """从配置文件初始化代理配置
        
        Returns:
            {"tunnel": 隧道代理数, "private": 私密代理数}
        """
        result = {"tunnel": 0, "private": 0}
        proxy_cfg = config.proxy_config
        
        if not proxy_cfg:
            return result
        
        # 初始化隧道代理
        tunnel_cfg = proxy_cfg.get("tunnel", {})
        if tunnel_cfg.get("enabled") and tunnel_cfg.get("addr") and tunnel_cfg.get("username"):
            try:
                existing = self.db.query(ProxyConfig).filter(
                    ProxyConfig.name == "config_tunnel"
                ).first()
                
                if not existing:
                    self.add_config(
                        name="config_tunnel",
                        provider="kuaidaili",
                        proxy_type="tunnel",
                        tunnel_addr=tunnel_cfg.get("addr"),
                        username=tunnel_cfg.get("username"),
                        password=tunnel_cfg.get("password"),
                        auto_refresh=False
                    )
                    logger.info(f"从配置文件添加隧道代理: {tunnel_cfg.get('addr')}")
                
                cfg = self.db.query(ProxyConfig).filter(
                    ProxyConfig.name == "config_tunnel"
                ).first()
                if cfg:
                    count = await self.fetch_from_config(cfg.id)
                    result["tunnel"] = count
                    
            except Exception as e:
                logger.error(f"初始化隧道代理失败: {e}")
        
        # 初始化私密代理
        private_cfg = proxy_cfg.get("private", {})
        auto_refresh_cfg = proxy_cfg.get("auto_refresh", {})
        
        if private_cfg.get("enabled") and private_cfg.get("api_url") and private_cfg.get("username"):
            try:
                existing = self.db.query(ProxyConfig).filter(
                    ProxyConfig.name == "config_private"
                ).first()
                
                auto_refresh = auto_refresh_cfg.get("enabled", False)
                refresh_interval = auto_refresh_cfg.get("interval", 300)
                
                if not existing:
                    self.add_config(
                        name="config_private",
                        provider="kuaidaili",
                        proxy_type="private",
                        api_url=private_cfg.get("api_url"),
                        username=private_cfg.get("username"),
                        password=private_cfg.get("password"),
                        fetch_num=private_cfg.get("fetch_num", 10),
                        auto_refresh=auto_refresh,
                        refresh_interval=refresh_interval
                    )
                    logger.info("从配置文件添加私密代理配置")
                else:
                    self.update_config(
                        existing.id,
                        api_url=private_cfg.get("api_url"),
                        username=private_cfg.get("username"),
                        password=private_cfg.get("password"),
                        fetch_num=private_cfg.get("fetch_num", 10),
                        auto_refresh=auto_refresh,
                        refresh_interval=refresh_interval
                    )
                
                cfg = self.db.query(ProxyConfig).filter(
                    ProxyConfig.name == "config_private"
                ).first()
                if cfg:
                    count = await self.fetch_from_config(cfg.id)
                    result["private"] = count
                    
            except Exception as e:
                logger.error(f"初始化私密代理失败: {e}")
        
        return result

    async def fetch_proxies_from_config(self) -> Dict[str, int]:
        """直接从 proxy.yaml 配置文件获取代理（不依赖数据库配置）
        
        Returns:
            {"tunnel": 隧道代理数, "overseas_tunnel": 海外隧道数, "private": 私密代理数}
        """
        result = {"tunnel": 0, "overseas_tunnel": 0, "private": 0}
        proxy_cfg = config.proxy_config
        
        if not proxy_cfg:
            logger.debug("[代理] proxy.yaml 配置为空")
            return result
        
        # 获取隧道代理
        tunnel_cfg = proxy_cfg.get("tunnel") or {}
        if tunnel_cfg.get("enabled"):
            addr = tunnel_cfg.get("addr")
            if addr:
                try:
                    username = tunnel_cfg.get("username")
                    password = tunnel_cfg.get("password")
                    # 有用户名密码则使用密码认证，否则白名单模式
                    self.add_tunnel_proxy(addr, username, password, "yaml_tunnel")
                    result["tunnel"] = 1
                    mode = "密码认证" if (username and password) else "白名单"
                    logger.info(f"[代理] 隧道代理已添加({mode}): {addr}")
                except Exception as e:
                    logger.error(f"[代理] 添加隧道代理失败: {e}")
        
        # 获取海外隧道代理
        overseas_cfg = proxy_cfg.get("overseas_tunnel") or {}
        if overseas_cfg.get("enabled"):
            addr = overseas_cfg.get("addr")
            if addr:
                try:
                    username = overseas_cfg.get("username")
                    password = overseas_cfg.get("password")
                    self.add_tunnel_proxy(addr, username, password, "yaml_overseas")
                    result["overseas_tunnel"] = 1
                    mode = "密码认证" if (username and password) else "白名单"
                    logger.info(f"[代理] 海外隧道代理已添加({mode}): {addr}")
                except Exception as e:
                    logger.error(f"[代理] 添加海外隧道代理失败: {e}")
        
        # 获取私密代理
        private_cfg = proxy_cfg.get("private") or {}
        if private_cfg.get("enabled"):
            api_url = private_cfg.get("api_url")
            username = private_cfg.get("username")
            password = private_cfg.get("password")
            
            if api_url and username and password:
                try:
                    count = await self.fetch_private_proxies(
                        api_url=api_url,
                        username=username,
                        password=password,
                        remark="yaml_private"
                    )
                    result["private"] = count
                    logger.info(f"[代理] 私密代理已获取: {count} 个")
                except Exception as e:
                    logger.error(f"[代理] 获取私密代理失败: {e}")
        
        return result
