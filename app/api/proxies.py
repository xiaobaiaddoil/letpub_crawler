"""代理池API"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from app.database import get_db
from app.models.proxy_pool import ProxyPool, ProxyConfig
from app.services.proxy_service import ProxyService

router = APIRouter(prefix="/api/proxies", tags=["proxies"])


# ========== 请求模型 ==========

class ProxyCreate(BaseModel):
    ip: str
    port: int
    protocol: str = "http"
    proxy_type: str = "direct"  # direct 或 tunnel
    username: Optional[str] = None
    password: Optional[str] = None
    remark: Optional[str] = None


class ProxyImportRequest(BaseModel):
    text: str
    protocol: str = "http"
    proxy_type: str = "private"
    source: str = "manual"
    username: Optional[str] = None
    password: Optional[str] = None
    remark: Optional[str] = None


class TunnelProxyCreate(BaseModel):
    """快捷添加隧道代理"""
    tunnel: str  # 如 "m684.kdltps.com:15818"
    username: str
    password: str
    remark: Optional[str] = None


class PrivateProxyCreate(BaseModel):
    """快捷添加私密代理"""
    api_url: str  # 提取API地址
    username: str
    password: str
    remark: Optional[str] = None


class ConfigCreate(BaseModel):
    name: str
    provider: str = "kuaidaili"
    proxy_type: str = "private"  # tunnel 或 private
    api_url: Optional[str] = None  # 私密代理API地址
    tunnel_addr: Optional[str] = None  # 隧道地址
    username: Optional[str] = None
    password: Optional[str] = None
    fetch_num: int = 10  # 每次获取数量
    auto_refresh: bool = False  # 是否自动刷新
    refresh_interval: int = 300  # 刷新间隔（秒）


# ========== 代理池接口 ==========

@router.get("")
def list_proxies(
    page: int = 1,
    page_size: int = 20,
    is_active: Optional[bool] = None,
    is_valid: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """获取代理列表"""
    query = db.query(ProxyPool)
    
    if is_active is not None:
        query = query.filter(ProxyPool.is_active == is_active)
    if is_valid is not None:
        query = query.filter(ProxyPool.is_valid == is_valid)
    
    total = query.count()
    proxies = query.order_by(ProxyPool.id.desc()).offset(
        (page - 1) * page_size
    ).limit(page_size).all()
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": p.id,
                "ip": p.ip,
                "port": p.port,
                "protocol": p.protocol,
                "proxy_type": p.proxy_type or "direct",
                "source": p.source,
                "area": p.area,
                "username": p.username,
                "has_password": bool(p.password),
                "remark": p.remark,
                "is_active": p.is_active,
                "is_valid": p.is_valid,
                "success_count": p.success_count,
                "fail_count": p.fail_count,
                "total_fail_count": p.total_fail_count,
                "response_time": p.response_time,
                "last_used_at": p.last_used_at,
                "created_at": p.created_at
            }
            for p in proxies
        ]
    }


@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    """获取代理池统计"""
    service = ProxyService(db)
    return service.get_stats()


@router.get("/random")
async def get_random_proxy(exclude_ids: str = "", db: Session = Depends(get_db)):
    """获取随机可用代理"""
    service = ProxyService(db)
    excluded = []
    for item in (exclude_ids or "").split(","):
        item = item.strip()
        if item.isdigit():
            excluded.append(int(item))
    proxy = await service.get_random_proxy(exclude_ids=excluded)
    if proxy:
        result = {
            "id": proxy.id,
            "ip": proxy.ip,
            "port": proxy.port,
            "protocol": proxy.protocol,
            "proxy_type": proxy.proxy_type or "direct",
            "source": proxy.source,
            "area": proxy.area,
            "remark": proxy.remark,
            "success_count": proxy.success_count,
            "fail_count": proxy.fail_count,
            "total_fail_count": proxy.total_fail_count,
            "username": proxy.username,
        }
        # 解密密码返回给 worker
        if proxy.password:
            from app.services.crypto import decrypt_password
            try:
                result["password"] = decrypt_password(proxy.password)
            except:
                pass
        return result
    return {}


@router.post("")
def add_proxy(data: ProxyCreate, db: Session = Depends(get_db)):
    """手动添加代理"""
    service = ProxyService(db)
    proxy = service.add_proxy(
        ip=data.ip,
        port=data.port,
        protocol=data.protocol,
        source="manual",
        proxy_type=data.proxy_type,
        username=data.username,
        password=data.password,
        remark=data.remark
    )
    return {"success": True, "id": proxy.id}


@router.post("/import")
def import_proxies(data: ProxyImportRequest, db: Session = Depends(get_db)):
    """批量文本导入代理"""
    service = ProxyService(db)
    result = service.import_proxies_from_text(
        text=data.text,
        protocol=data.protocol,
        source=data.source,
        proxy_type=data.proxy_type,
        username=data.username,
        password=data.password,
        remark=data.remark,
    )
    return {"success": True, **result}


@router.get("/export")
def export_proxies(
    fmt: str = "hostport_auth",
    only_active: bool = True,
    only_valid: bool = False,
    include_auth: bool = True,
    db: Session = Depends(get_db)
):
    """导出代理文本"""
    if fmt not in {"hostport", "hostport_auth", "url"}:
        raise HTTPException(status_code=400, detail="fmt 仅支持 hostport/hostport_auth/url")
    service = ProxyService(db)
    content = service.export_proxies_as_text(
        fmt=fmt,
        only_active=only_active,
        only_valid=only_valid,
        include_auth=include_auth,
    )
    return Response(
        content=content + ("\n" if content else ""),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="proxies.txt"'},
    )


@router.post("/tunnel")
def add_tunnel_proxy(data: TunnelProxyCreate, db: Session = Depends(get_db)):
    """快捷添加隧道代理"""
    service = ProxyService(db)
    try:
        proxy = service.add_tunnel_proxy(
            tunnel=data.tunnel,
            username=data.username,
            password=data.password,
            remark=data.remark
        )
        return {"success": True, "id": proxy.id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/private")
async def add_private_proxy(data: PrivateProxyCreate, db: Session = Depends(get_db)):
    """从API获取私密代理（批量）"""
    service = ProxyService(db)
    count = await service.fetch_private_proxies(
        api_url=data.api_url,
        username=data.username,
        password=data.password,
        remark=data.remark
    )
    if count > 0:
        return {"success": True, "added": count}
    raise HTTPException(status_code=400, detail="获取私密代理失败")


@router.delete("/{proxy_id}")
def delete_proxy(proxy_id: int, db: Session = Depends(get_db)):
    """删除代理"""
    service = ProxyService(db)
    if service.delete_proxy(proxy_id):
        return {"success": True}
    raise HTTPException(status_code=404, detail="代理不存在")


@router.post("/{proxy_id}/toggle")
def toggle_proxy(proxy_id: int, db: Session = Depends(get_db)):
    """切换代理启用状态"""
    service = ProxyService(db)
    proxy = service.toggle_proxy(proxy_id)
    if proxy:
        return {"success": True, "is_active": proxy.is_active}
    raise HTTPException(status_code=404, detail="代理不存在")


@router.post("/{proxy_id}/check")
async def check_proxy(proxy_id: int, db: Session = Depends(get_db)):
    """验证单个代理"""
    proxy = db.query(ProxyPool).filter(ProxyPool.id == proxy_id).first()
    if not proxy:
        raise HTTPException(status_code=404, detail="代理不存在")
    
    service = ProxyService(db)
    is_valid = await service.check_proxy(proxy)
    return {
        "success": True,
        "is_valid": is_valid,
        "response_time": proxy.response_time
    }


@router.post("/{proxy_id}/report")
def report_proxy_result(proxy_id: int, success: bool = True, db: Session = Depends(get_db)):
    """报告代理使用结果"""
    service = ProxyService(db)
    service.report_proxy_result(proxy_id, success)
    return {"success": True}


@router.post("/check-all")
async def check_all_proxies(db: Session = Depends(get_db)):
    """验证所有代理"""
    service = ProxyService(db)
    result = await service.check_all_proxies()
    return {"success": True, **result}


@router.post("/clear-invalid")
def clear_invalid(db: Session = Depends(get_db)):
    """清理无效代理"""
    service = ProxyService(db)
    count = service.clear_invalid_proxies()
    return {"success": True, "deleted": count}


@router.post("/fetch")
async def fetch_proxies(config_id: Optional[int] = None, db: Session = Depends(get_db)):
    """从代理服务商获取代理"""
    service = ProxyService(db)
    if config_id:
        count = await service.fetch_from_config(config_id)
    else:
        # 如果没有指定配置，刷新所有启用自动刷新的配置
        result = await service.auto_refresh_proxies()
        count = result["added_proxies"]
    return {"success": True, "added": count}


# ========== 配置接口 ==========

@router.get("/configs")
def list_configs(db: Session = Depends(get_db)):
    """获取代理配置列表"""
    service = ProxyService(db)
    configs = service.get_configs()
    return {
        "items": [
            {
                "id": c.id,
                "name": c.name,
                "provider": c.provider,
                "proxy_type": c.protocol,  # tunnel 或 private
                "api_url": c.api_url,
                "tunnel_addr": c.area,
                "username": c.secret_id,
                "has_password": bool(c.secret_key),
                "fetch_num": c.fetch_num,
                "auto_refresh": c.is_active,  # is_active 复用为自动刷新开关
                "refresh_interval": c.fetch_interval,
                "last_fetch_at": c.last_fetch_at
            }
            for c in configs
        ]
    }


@router.post("/configs")
def add_config(data: ConfigCreate, db: Session = Depends(get_db)):
    """添加代理配置"""
    service = ProxyService(db)
    config = service.add_config(
        name=data.name,
        provider=data.provider,
        proxy_type=data.proxy_type,
        api_url=data.api_url,
        username=data.username,
        password=data.password,
        tunnel_addr=data.tunnel_addr,
        fetch_num=data.fetch_num,
        auto_refresh=data.auto_refresh,
        refresh_interval=data.refresh_interval
    )
    return {"success": True, "id": config.id}


@router.post("/auto-refresh")
async def auto_refresh_proxies(db: Session = Depends(get_db)):
    """自动刷新所有启用自动刷新的配置"""
    service = ProxyService(db)
    result = await service.auto_refresh_proxies()
    return {"success": True, **result}


@router.post("/configs/{config_id}/fetch")
async def fetch_from_config(config_id: int, db: Session = Depends(get_db)):
    """根据配置获取代理"""
    service = ProxyService(db)
    count = await service.fetch_from_config(config_id)
    return {"success": True, "added": count}


@router.delete("/configs/{config_id}")
def delete_config(config_id: int, db: Session = Depends(get_db)):
    """删除代理配置"""
    service = ProxyService(db)
    if service.delete_config(config_id):
        return {"success": True}
    raise HTTPException(status_code=404, detail="配置不存在")
