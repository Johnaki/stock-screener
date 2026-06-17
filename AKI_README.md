# AKI 星星量股票筛选器

这是独立的 AKI 版本，不会和你 GitHub 上原来正在跑的筛选器冲突。

已经分开的地方：

- 脚本文件：`AKI_star_volume_scanner.py`
- 依赖文件：`AKI_requirements.txt`
- 配置文件：`AKI_config.example.json`
- 工作流文件：`.github/workflows/AKI_star_volume.yml`
- GitHub Action 名字：`AKI Star Volume Stock Scanner`
- GitHub Secret 名字：`AKI_SERVER_CHAN_SENDKEY`
- 报告目录：`AKI_reports/`
- 报告标题：`AKI星星量股票筛选`

推送分两组：

- 已暴涨验证组：先出现低位星星量，随后已经放量上涨，用来验证模型是否抓对。
- 有暴涨趋势组：当前仍处于低位缩量、波动收窄、指标修复状态，作为观察名单。

详细操作看 `AKI_使用步骤.md`。

