# 账号管理功能

## 功能概述

支持使用 LetPub 账号密码自动登录获取 Cookie，解决手动复制 Cookie 的麻烦和 Cookie 过期问题。

## 特性

- **密码加密存储**: 使用 Fernet 对称加密，密码不以明文存储
- **自动刷新**: Cookie 失败次数过多时自动重新登录
- **多账号支持**: 可添加多个账号，轮流使用

## 配置

### 1. 设置加密密钥

在 `.env` 文件中设置 `ENCRYPTION_KEY`：

```bash
# 生成密钥
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 添加到 .env
ENCRYPTION_KEY=your-generated-key-here
```

**重要**: 生产环境必须设置此密钥，且需要妥善保管。更换密钥后已存储的密码将无法解密。

### 2. 运行数据库迁移

```bash
psql -h <host> -U <user> -d letpub_crawler -f migrations/006_add_accounts_table.sql
```

### 3. 安装依赖

```bash
uv sync
# 或
pip install cryptography>=42.0.0
```

## API 接口

### 账号管理

| 方法   | 路径                         | 说明                    |
| ------ | ---------------------------- | ----------------------- |
| GET    | `/api/accounts`              | 获取所有账号列表        |
| POST   | `/api/accounts`              | 添加账号                |
| DELETE | `/api/accounts/{id}`         | 删除账号                |
| POST   | `/api/accounts/{id}/toggle`  | 启用/禁用账号           |
| POST   | `/api/accounts/{id}/login`   | 手动触发登录获取 Cookie |
| POST   | `/api/accounts/refresh-all`  | 刷新所有账号的 Cookie   |
| POST   | `/api/accounts/check-failed` | 检查并刷新失败的 Cookie |

### 添加账号示例

```bash
curl -X POST http://localhost:8000/api/accounts \
  -H "Content-Type: application/json" \
  -d '{
    "email": "your-email@example.com",
    "password": "your-password",
    "remark": "主账号"
  }'
```

### 手动刷新 Cookie

```bash
# 刷新指定账号
curl -X POST http://localhost:8000/api/accounts/1/login

# 刷新所有账号
curl -X POST http://localhost:8000/api/accounts/refresh-all
```

## 自动刷新机制

当 Cookie 使用失败次数达到阈值（默认 3 次）时：

1. Worker 报告 Cookie 失败 (`/api/cookies/{id}/report-fail`)
2. 系统检测到失败次数超过阈值
3. 自动查找对应账号并重新登录
4. 更新 Cookie 池中的 Cookie 值
5. 重置失败计数

## Cookie 命名规则

自动登录获取的 Cookie 命名格式为 `auto_{email}`，例如：

- `auto_user@example.com`

手动添加的 Cookie 可以使用任意名称。

## 安全说明

1. **加密算法**: 使用 Fernet（基于 AES-128-CBC）
2. **密钥管理**: 密钥存储在环境变量中，不进入代码仓库
3. **传输安全**: API 传输密码时建议使用 HTTPS
4. **访问控制**: 生产环境建议对账号管理 API 添加认证

## 故障排除

### 登录失败

1. 检查账号密码是否正确
2. 检查网络是否能访问 letpub.com.cn
3. 查看日志中的具体错误信息

### 解密失败

如果更换了 `ENCRYPTION_KEY`，已存储的密码将无法解密。需要：

1. 删除旧账号
2. 使用新密钥重新添加账号

### Cookie 无效

1. 手动触发登录刷新
2. 检查账号是否被封禁
3. 尝试在浏览器中手动登录验证
