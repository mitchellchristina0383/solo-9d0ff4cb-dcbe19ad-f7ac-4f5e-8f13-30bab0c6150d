# PITR 交付包（PostgreSQL 指定时间线 / LSN 恢复）

值班 DBA 切主后，需要在本地样例归档中备齐“恢复到指定时间线和 LSN”所需的全备与
WAL 段，按正确顺序交给恢复脚本，并保证交付的每个字节都与清单 SHA-256 一致。本项目
是一个 Flask 网页 + Docker Compose 启动的小工具，只读本地样例清单，不做任何数据库
连接。

## 它能做什么

1. **选全备**：读取全备清单里的“重放起点（replay_start_lsn）”与“一致位点
   （consistency_lsn）”，在可行候选中挑一致位点**最接近且不晚于目标**的那份。
   可行 = 一致位点 ≤ 目标，且全备所在时间线位于目标时间线的祖先链上、未越过其后
   分叉点。
2. **排 WAL 顺序**：按目标时间线历史从祖先到目标**连续取段**。当分叉点落在某个
   段的**内部**时，先取父时间线在该段槽位的文件（只重放到分叉点前），再取子时间线
   同槽位的文件（从分叉点起），即“前后各取所属时间线的 WAL”。
3. **校验字节**：每个 WAL 段和全备内的每个文件都按清单 SHA-256 重新计算。
4. **坏档替换**：主档校验失败时，自动尝试清单里登记的**同一文件的副本**；副本也不
   可用则该全备被放弃，**重选更早的可用全备**。
5. **断点说明**：没有任何可行链时，逐份全备列出失败原因；缺段会给出**断点 LSN**、
   缺失文件名与应覆盖的 LSN 范围。
6. **整包放行**：所有步骤校验通过才生成 tar.gz；下载前再复核一次归档 SHA-256，不一
   致返回 409。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://127.0.0.1:8000
```

镜像内已烘焙样例归档（`/data`），交付包写入命名卷 `pitr-packages`。若要用宿主机上
的归档，取消 `docker-compose.yml` 里 `./data:/data:ro` 的注释即可。

> 本地没有 Docker 时，可直接用 venv 运行：
> `pip install -r requirements.txt && PITR_DATA_DIR=./data PITR_PACKAGES_DIR=./var/packages gunicorn -b 127.0.0.1:8000 pitr.webapp:app`
> （`var/` 需先 `mkdir -p`。）

## 样例归档

`python3 tools/make_sample.py --out data` 生成（确定性、可重复）。3 条时间线、
2 次分叉、段大小 1 MiB：

| 时间线 | 父 | 分叉 LSN | 说明 |
| --- | --- | --- | --- |
| `00000001` | — | — | 根 |
| `00000002` | `00000001` | `0/300000` | 在段 3 **起始处**分叉 |
| `00000003` | `00000002` | `0/580000` | 在段 5 **内部**（5.5 MiB）分叉 |

全备 5 份，含三种故障样本：

| 全备 | 时间线 | 重放起点 | 一致位点 | 预埋问题 |
| --- | --- | --- | --- | --- |
| `base_20251120_0100` | 1 | 0/80000 | 0/180000 | — |
| `base_20251121_0100` | 1 | 0/200000 | 0/2C0000 | `base/1/2610` 损坏，**无副本** |
| `base_20251122_0100` | 2 | 0/320000 | 0/3C0000 | `global/pg_control` 损坏，**有副本** |
| `base_20251123_0100` | 2 | 0/480000 | 0/540000 | `global/pg_control` 损坏，**无副本**（触发重选） |
| `base_20251123_0900` | 3 | 0/5C0000 | 0/620000 | — |

WAL 段：时间线 1 段 0–3、时间线 2 段 3–5、时间线 3 段 5–6。其中：

* `000000030000000000000005` 主档损坏，`copies/` 下有副本（坏档替换）；
* 时间线 3 **故意缺段 7**，目标 `0/800000` 时用于演示断点。

## 页面与路由

| 路由 | 作用 |
| --- | --- |
| `GET /` | 时间线历史、全备清单、WAL 清单、已有交付包、目标输入 |
| `POST /plan` | 生成重放计划：重放顺序、分叉前后标注、副本替换、断点/重选说明 |
| `POST /build` | 校验通过后打包并跳转详情 |
| `GET /packages` | 已生成交付包列表 |
| `GET /packages/<id>` | 包详情（含 SHA-256 与包内重放顺序） |
| `GET /packages/<id>/download` | 下载（下载前复核归档摘要，不符 409） |
| `GET /healthz` | Compose 健康检查 |
| `POST /reload` | 重新加载清单 |

## 实现要点

* `pitr/core.py` —— 纯逻辑：LSN 解析/格式化、WAL 文件名与段号换算、清单校验、
  时间线祖先链、全备选择、`build_segment_chain()` 连续取段与段内分叉处理。
  目标语义为**重放区间 `[replay_start, target_lsn)`**，即最后需要的是包含
  `target_lsn - 1` 的段；目标恰好落在段边界时不会多取下一段。
* `pitr/store.py` —— 文件系统层：加载清单、按 SHA-256 解析主档/副本、生成
  tar.gz（边复制边复算摘要，失败即删除临时文件）、包登记与过期清理。
* `pitr/webapp.py` —— Flask 路由与模板过滤器；`/data`、`/packages` 路径、
  包 TTL 由环境变量控制。
* 包内结构：`README.txt`（恢复步骤 + 重放顺序）、`pitr_plan.json`（机器可读计划
  与每文件摘要）、`recovery.signal`、`postgresql.auto.conf`
  （`recovery_target_timeline` / `recovery_target_lsn`）、`basebackup/…`、`wal/…`。
* 安全：清单内路径做相对路径白名单校验（禁止 `..`、绝对路径、反斜杠），所有读
  文件都经 `_full_path()` 锚定在数据根目录内；备份 id/包 id 校验字符集。

## 自测

```bash
pip install -r requirements-dev.txt
python -m pytest                      # 61 项单元/集成/HTTP 测试
python tools/selftest.py --data data  # 12 项端到端核对（见下）
```

实测（本机 Python 3.11 + Flask 3.1.3，未安装 Docker）：

* `pytest` —— **61 passed**。
* `tools/selftest.py` —— **12/12 checks passed**，覆盖：
  * 段内分叉 `2→3`：`000000020000000000000005`（0/500000–0/57FFFF）
    紧接 `000000030000000000000005`（0/580000–0/5FFFFF），同一 segment 槽位；
  * 坏档替换：WAL 主档与全备 `pg_control` 均改用 `copies/` 副本；
  * 重选全备：目标 `0/560000` 时最近全备无副本损坏，改用 `base_20251122_0100`；
  * 断档：目标 `0/800000` 报断点 `0/700000` + 缺失段
    `000000030000000000000007`；
  * 整包 SHA-256 与记录一致、全 WAL 入包、包内 WAL 字节非损坏主档；
  * 篡改归档后下载复核能识别。

## 未做 / 余项

* **未在容器内实测**：本机没有 Docker，`Dockerfile` / `docker-compose.yml`
  只做了 YAML 结构校验与同等命令（gunicorn + 真实 HTTP 请求）的本地验证；
  首次 `docker compose up --build` 需要在有 Docker 的机器上确认。
* 只支持样例中的**单条目标时间线**规划；多分支同时恢复、跨 timeline 的
  `recovery_target_timeline=latest` 语义未实现。
* 未解析真实 PostgreSQL 的 `backup_label` / `pg_control` / WAL 头，清单中的
  时间线与 LSN 作为可信输入（样例数据是模拟字节）。
* 段大小固定为清单声明的值；未处理 `.partial` / `.backup` 等 WAL 附属文件。
* 交付包未签名；仅校验 SHA-256，未接入 GPG 或 HMAC。
* 包清理为进程内 TTL 清理（访问首页时触发），多副本部署需要外部定时任务。
