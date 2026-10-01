# 🎛 灯光渐变时间轴（Lighting Fade Timeline）

供灯光师排演使用的“灯光渐变时间轴” Web 应用。浏览器中以原生 JavaScript 展示
各灯具通道（DMX 通道，取值 0～255）的提示项时间轴与实时读数，后端使用
Python + FastAPI + SQLite 保存工程。

## 功能概览

- **提示项管理**：新建 / 编辑 / 删除提示项，逐项设置灯具、通道、目标值（0～255
  整数）与渐变时长（非负整数毫秒；`0` 表示立即切换）。
- **两种起步方式**：
  - 绝对时间：非负整数毫秒；
  - 引用另一项的 **开始** 或 **结束**，再加非负整数毫秒偏移。
- **保存校验（失败不改变原工程）**：
  - 引用的提示项不存在（缺失引用）→ 拒绝；
  - 引用链存在环（含自引用）→ 拒绝，并给出环路径；
  - 删除仍被其他项引用的提示项 → 拒绝；
  - 同一灯具同一通道在同一实际时刻已有一项起步 → 拒绝；
  - 任何校验失败都在同一数据库事务中回滚，原工程保持不变。
- **自动重算**：保存成功后，按引用 DAG 拓扑顺序重算全部实际开始/结束时刻。
- **渐变求值模型**：
  - 各通道初值为 `0`；
  - 新渐变从起步时刻上一渐变的 **未取整（精确分数）值** 接续，做线性渐变；
  - 零时长立即切换；后开始的提示项会 **打断** 尚未结束的渐变，被打断项在
    时间轴上用红框 + 红色斜纹/覆盖区标出；
  - 支持任意非负毫秒绝对时间求值，**含小数毫秒时刻**（如 `t=1.5`、
    `t=16.6666…`，也接受 `3/2` 这样的分数字符串）；显示值
    **四舍五入，恰好 0.5 向上**（界面同时显示精确分数值）。
- **时间轴界面**：
  - 每灯具通道一条泳道，色块表示渐变、窄竖条表示立即切换；
  - **在时间轴上按住拖动**移动播放头，**播放 / 暂停 / 归零**，或在跳转框输入
    毫秒（接受小数，如 `1.5`）后回车 / 点 Go；
  - 播放头、拖动与跳转的时刻（含 `requestAnimationFrame` 产生的小数毫秒）
    **不取整**地经 `/eval?t=<时刻>` 传给后端；播放时求值请求仍节流在
    约 12 次/秒，并在暂停/停止时补发最后一个位置，因此 **跳转 / 拖动 /
    播放暂停后的读数与对同一时刻直接调用 `/eval` 完全相同**；
  - 选中提示项后，时间轴以紫色描边标出其完整 **引用链**（上游依赖与下游引用者），
    编辑面板可点击引用链上的 `#编号` 跳转；
  - 读数卡片标注“渐变中 / 保持”状态；若当前渐变之后将被打断，给出提示。

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

启动后浏览器访问：

- 应用界面：<http://localhost:8000/>
- API 文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/api/health>
- 精确求值接口：`GET /api/projects/{id}/eval?t=<毫秒>`，`t` 为任意非负数字，
  **小数毫秒原样保留**（由后端 `fractions.Fraction` 精确求值）。演示工程
  创建后可直接验证，例如：

  ```bash
  # 0→10 的 10ms 渐变，1.5ms 时精确值为 3/2，半值向上显示 2
  curl 'http://localhost:8000/api/projects/1/eval?t=1.5'
  # {"time_ms":1.5,"channels":[{"fixture":"…","channel":1,
  #  "raw":"3/2","value":2,"active_item_id":1}]}
  ```

SQLite 数据库保存在 Docker 卷 `lights-data`（容器内 `/data/lights.db`）中，
重启 / 重建容器数据不丢失。首次启动会自动建表并写入一个“演示工程”
（包含引用、立即切换与渐变被打断的例子）。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据卷
```

## 本地运行（不用 Docker）

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
LIGHTS_DB=data/lights.db python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 <http://localhost:8000/>。`LIGHTS_DB` 环境变量可指定 SQLite 文件路径，
默认为 `data/lights.db`（容器中默认为 `/data/lights.db`）。

## 运行测试

核心求值引擎与 API 的单元 / 集成测试共 40 余个：

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：半值向上舍入（含小数时刻恰好半值）、缺失引用 / 引用环 /
同刻冲突拒绝与事务回滚、引用开始/结束偏移、零时长切换、从未取整值接续、
渐变打断与标记、整数时刻回归、**小数毫秒时刻端到端（引擎 / API /
求值一致性 / 非法输入 422）** 等。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/projects` | 工程列表 |
| POST | `/api/projects` | 新建工程 `{"name": "..."}` |
| GET | `/api/projects/{id}` | 工程详情（含各项实际时刻、打断关系、引用闭包） |
| POST | `/api/projects/{id}/items` | 新增提示项 |
| PUT | `/api/projects/{id}/items/{item_id}` | 修改提示项（整项替换） |
| DELETE | `/api/projects/{id}/items/{item_id}` | 删除提示项（被引用时 409） |
| GET | `/api/projects/{id}/eval?t=<ms>` | 对非负毫秒时刻求全部通道值，`t` 可为小数（如 `1.5`、`3/2`） |

提示项 JSON 示例：

```json
// 绝对时间起步
{"fixture": "面光1", "channel": 1, "target": 180, "duration": 2000,
 "start_mode": "abs", "start_ms": 0}

// 引用 #1 的结束 + 0ms 起步
{"fixture": "面光1", "channel": 1, "target": 60, "duration": 1500,
 "start_mode": "ref", "ref_id": 1, "ref_edge": "end", "ref_offset": 0}
```

求值响应同时返回四舍五入后的显示值 `value` 与精确值 `raw`（分数字符串），
以及此刻决定该通道值的提示项 `active_item_id`；`time_ms` 原样回显求值时刻
（整数回显为整数，小数回显为浮点数）。负时间或非数字返回 `422`。

## 目录结构

```
app/
  engine.py    # 纯逻辑：引用 DAG 校验/求解、线性渐变分段、精确求值、半值向上舍入
  db.py        # SQLite 连接、建表、事务（失败回滚，原工程不变）
  schemas.py   # Pydantic 入参校验
  seed.py      # 首次启动的演示工程
  main.py      # FastAPI 路由（工程/提示项 CRUD、/eval、静态页面）
static/
  index.html   # 单页界面
  style.css
  app.js       # 原生 JavaScript：SVG 时间轴、播放/拖动/跳转、读数与引用链
tests/
  test_engine.py
  test_api.py
Dockerfile
docker-compose.yml
```

## 设计说明

- 提示项的开始/结束时刻以非负整数毫秒表示（引用偏移亦为整数）；引用图为
  DAG，实际时刻在保存时按记忆化递归（等价于拓扑序）整体重算。
- **求值时刻**独立于提示项时刻，允许任意非负小数毫秒：API 查询参数按字符串
  交给 `fractions.Fraction` 精确解析（`1.5` → `3/2`，二进制浮点也精确转换），
  与整数分段边界做精确比较，不存在截断或浮点边界误差。
- 渐变值使用 Python `fractions.Fraction` 精确计算，因此多次打断 / 接续不会
  累积浮点或显示舍入误差；显示时才做半值向上舍入。
- 前端播放链（播放头、`requestAnimationFrame` 累计、拖动反算、跳转框）全程
  保留小数，不做 `Math.floor`；节流只控制请求频率（约 12 次/秒），被跳过的
  帧由尾部定时请求补齐，保证暂停/结束时读数等于播放头所在时刻。
- 写操作先在事务内写入“候选结果”，再对整个工程重新校验，任何错误都会回滚，
  保证“失败不得改变原工程”。
