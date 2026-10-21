# 赛道整理工具

此目录负责生成和维护归类流程使用的赛道定义：

- `extract_tracks.py`：从原始赛道看板提取结构化数据。
- `audit_tracks.py`：检查赛道字段和证据质量。
- `curate_tracks.py`：整理、合并、补充和删除不合理赛道。
- `curate_smb_product_mappings.py`：生成当前确定性SMB产品映射。
- `remap_smb_products.py`：使用API重做SMB产品映射。
- `render_track_dashboard.py`：生成赛道看板。

企业归类统一入口直接接收已经整理好的classification-ready赛道文件，不会自动重做赛道整理。

