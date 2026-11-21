# 东港水产拍卖结算

给水产市场结算员处理双次称重、扣杂计价、结算更正与应付复核。

七个模块：称重采信、货品换算、毛皮配对、扣杂竞价、更正裁决、应付对账、结算归档。每个导航页均可重跑同一批数据，隔离行保留编号、原值、行号和原因码。

另有第 8 页“证据发布”：面向季度审计生成脱敏证据包。页面展示抽样票、遮盖项、采用的更正版本与封存核对结果。证件或联系方式无法稳定对应、抽样票缺原始依据、封存版本与生效版本不一致时不出包。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

浏览器访问 http://localhost:18240 。默认载入现场脏批，包含53条逻辑输入、重复表头、缺列、空值、重复冲突、缺皮重、毛重小于皮重等问题。切换“核验清批”或“旧编码批”后重跑。进入“更正裁决”核对票00001：原应付1168.50CNY，更正后1285.35CNY，已付1168.50CNY，仅待付116.85CNY。

API：各模块 `POST /api/<导航英文名>`；完整运行 `POST /api/runs`，JSON参数可用 `dataset` 或 `csv_text`。查询历史 `GET /api/runs`，复核 `POST /api/runs/{run_id}/review`，传入当前 `revision`。封存下载只有 `GET /api/export/{run_id}` 一个入口。

脱敏证据包：`POST /api/runs/{run_id}/publish` 发布，`GET /api/runs/{run_id}/packages` 查清单，`GET /api/packages/{package_id}` 下载。包内 zip 由内容哈希命名，同一批次重跑复用同一包，不新增重复记录；发布在一个事务内作废旧版、写入新版，中断即回滚，旧版仍是当前版，历史包只标记 superseded 不删除。包内证件尾号、联系电话、隔离地址一律遮盖为 `***`，另附 HMAC 指纹与船户化名供财务核对；指纹盐取自环境变量 `REDACT_SALT`（默认仅开发用，生产必须更换）。原始凭证、隔离证据与船户档案（`sample_data/owners.json`，可用 `overrides.owners` 覆盖）只留在内部库，不随包发出。

同输入和规则摘要生成相同批号，不新增重复记录；复核版本改变后旧版本提交返回409。SQLite保存在命名卷。可使用新卷名称重建演示数据，原卷保留。

定义见 `docs/DATA-DICTIONARY.md`，模块与测试映射见 `docs/DEPTH.md`。源码在 `app/`，业务样例在 `sample_data/`，测试在 `tests/`。不连接实际秤台和支付渠道。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。
