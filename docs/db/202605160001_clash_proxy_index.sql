-- 给 ProxyPool 加 (source, is_active, is_valid) 复合索引，
-- 加速 source=clash 路径与 random 选取查询。
CREATE INDEX IF NOT EXISTS idx_proxy_pool_source_active
    ON proxy_pool (source, is_active, is_valid);
