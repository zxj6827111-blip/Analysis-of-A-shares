# AStock 服务上阿里云 / 多应用服务器部署说明
#
# 本应用 **不是** Docker 项目，**默认也不依赖** PM2。
# 进程就是: python -m wtpy.apps.astock serve
# 可用 PM2 或 systemd 做常驻，**无需改业务代码**。
#
# 你服务器上已有很多应用时，请按下面顺序做。

## 1. 上传代码与数据

建议目录: `/opt/wtpy/wtpy-master`

至少包含:
- `wtpy/`、`requirements.txt`、`setup.py`、`deploy/`
- `storage/astock/`（行情缓存；本机约 csv+npz+复权 ~1GB 级）
- 可选: `指标/`、`outputs/astock/`

可用 git clone、scp、rsync 或 OSS。

## 2. 先探测现状（必做）

SSH 登录服务器后:

```bash
cd /opt/wtpy/wtpy-master
# Windows 上传后可能没执行权限:
chmod +x deploy/*.sh
bash deploy/probe_server.sh --port 8765
```

脚本会检查:
- 内存 / 磁盘
- 是否已有 **PM2 / Docker / systemd / Nginx**
- 端口 **8765** 是否被占用
- Python 版本是否合适（建议 3.9/3.10）
- `storage/astock` 是否有数据
- 输出推荐: `pm2` 或 `systemd`
- 结果 JSON: `deploy/.probe_result.json`

**把终端完整输出发回**，再最终定安装方式（若你已熟悉，可直接看推荐项）。

## 3. 安装与常驻

### 自动（推荐）

```bash
bash deploy/install_astock.sh --mode auto --port 8765 --host 127.0.0.1
```

- 若探测到 **PM2** → 用 PM2 启动 `astock-serve`
- 若仅有 **systemd** → 生成 unit，root 下 enable
- 都没有 → 只建 venv，前台脚本 `deploy/start_foreground.sh`

### 强制 PM2（与现有多应用统一）

```bash
# 需已安装: node + npm i -g pm2
bash deploy/install_astock.sh --mode pm2 --port 8765 --host 127.0.0.1
pm2 status
pm2 logs astock-serve
pm2 startup   # 开机自启，按提示执行
pm2 save
```

生成的配置: `deploy/ecosystem.config.cjs`（绝对路径，由 install 写入）。

### 强制 systemd

```bash
bash deploy/install_astock.sh --mode systemd --host 127.0.0.1 --port 8765
sudo cp deploy/astock-serve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now astock-serve
```

## 4. 端口与多应用共存

| 情况 | 做法 |
|------|------|
| 8765 已被占用 | `install ... --port 8766` |
| 已有 Nginx | 反代到 `127.0.0.1:端口`，安全组只开 80/443 |
| 临时直连调试 | `--host 0.0.0.0` + 安全组临时放行，测完改回 |

**不要** 用 PM2 `instances > 1` 扩多进程（当前单进程任务/作业状态）。

## 5. 依赖说明

- `requirements.txt`: numpy, pandas==1.3.5, fastapi, uvicorn, ...
- 业务另需 **openpyxl**（Excel 导入导出）；install 脚本会补装，requirements 已列入
- 不需要 Docker 镜像；也不需要改 `api.py` 才能被 PM2 拉起

## 6. 常用命令

```bash
# PM2
pm2 restart astock-serve
pm2 stop astock-serve
pm2 logs astock-serve --lines 200

# systemd
sudo systemctl restart astock-serve
journalctl -u astock-serve -f

# 前台调试
bash deploy/start_foreground.sh
```

## 7. 是否需要改代码？

| 需求 | 是否改代码 |
|------|------------|
| 常驻 / 开机启动 | 否（PM2/systemd） |
| 换端口、绑 host | 否（CLI 参数） |
| 指定 storage / tdx | 否（`--storage` / `--tdx-root`） |
| 登录鉴权 / HTTPS | 否（Nginx / 网关层） |
| 多机分布式回测 | 才可能要改架构（当前未做） |

## 8. 回传信息清单（方便远程判断）

请在服务器执行 `probe_server.sh` 后提供:

1. 完整终端输出或 `deploy/.probe_result.json`
2. `pm2 list` 或 `docker ps`（若有）
3. 计划目录路径（默认 `/opt/wtpy/wtpy-master`）
4. 是否已有 Nginx、是否有域名
5. 安全组是否允许公网访问 Web
