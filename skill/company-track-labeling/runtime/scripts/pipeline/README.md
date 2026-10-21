# 生产流程模块

本目录只存放`run_company_track_pipeline.py`调用的生产阶段：

1. `prepare_companies_for_classification.py`：读取企业XLSX并生成统一企业记录。
2. `prepare_tracks_for_retrieval.py`：将整理好的赛道转成召回结构。
3. `generate_semantic_embeddings.py`：生成并缓存企业、赛道向量。
4. `retrieve_hybrid_candidates_operational.py`：执行固定参数的BM25与E0_WEIGHTED融合召回。
5. `verify_candidate_evidence_api.py`：按“一个赛道+4—8家企业”批量核验本地证据。
6. `web_recheck_unresolved_companies.py`：对未决企业补档并复判，支持basic与advanced两级搜索。
7. `export_company_track_results_excel.py`：在输入企业工作簿末尾追加最终结果列。

日常使用应从上级目录的统一入口启动，不需要逐个运行这些模块。

