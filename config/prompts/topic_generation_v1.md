你负责把一批结构化 Event 直接归纳为最终候选 Topic。

Topic 是近期可观察的具体技术、产品、制造服务或产业化活动方向。它用于连接事实 Event 与中观 Track，不是政策标题、文件类别、事件类型，也不是宽泛行业或单条事实的改写。

归类原则：
- 主要依据 industry_objects、topic_hint、经营对象和产业链环节；
- 指向同一技术/产品生态的政策、研发、中试、制造、项目、融资和经营信号归入同一 Topic；
- 技术对象、产品形态或企业集合明显不同的 Event 保持分离；
- 不要创建“政策支持”“项目建设”“企业经营改善”“年度工作”等只描述动作或现象的 Topic；
- 宽泛、外围或无法指向明确产业对象的 Event 可以不归类；
- 单个 Event 可以形成 Topic，但名称必须是稳定的产业活动方向，而不是该 Event 的标题。

仅输出每个 Topic 的：
- candidate_key：本次响应内唯一的简短英文或拼音键；
- label：简洁的技术、产品或产业化主题；
- definition：稳定边界，说明包含的产业对象、经营活动或产业链环节；
- summary：概括本批 Event 体现的近期事实；
- event_ids：属于该 Topic 的输入 Event ID；
- confidence：0 到 1。

同一 Event 最多归入三个 Topic。只能引用输入 event_id，不补充外部事实。严格输出 JSON，不输出 Markdown 或解释。
