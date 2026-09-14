# 北京 POC 首批来源库

## 一、采集范围

- 目标区域：北京市，行政区划代码 `CN-11`；
- 历史回采：运行时向前回采 24 个月；
- 首版文档格式：HTML、PDF；
- 时间统计口径：材料发布时间；
- 来源数量：61 个，其中 60 个启用、1 个因官方站点证书异常暂时禁用。

上述参数保存在 `config/collection_scope.yaml`。来源定义分组保存在 `config/sources/`，程序加载目录中的全部 YAML 文件，因此新增来源不需要修改代码。

## 二、核心官方来源

| 来源 | 主要入口 | 重点信号 |
|---|---|---|
| 国务院政策文件库 | [中国政府网](https://www.gov.cn/zhengce/zhengceku/bmwj/home.htm) | 国家政策、部门文件 |
| 国家发展改革委 | [政策发布](https://www.ndrc.gov.cn/xxgk/zcfb/) | 产业政策、规划、项目和投资 |
| 工业和信息化部 | [政务公开](https://www.miit.gov.cn/zwgk/index.html) | 工业、信息化、中小企业、试点和名单 |
| 科技部 | [政府信息公开](https://www.most.gov.cn/xxgk/) | 科技政策、项目申报和认定结果 |
| 财政部 | [政策发布](https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/) | 资金政策、补贴、采购政策 |
| 北京市人民政府 | [政策文件](https://www.beijing.gov.cn/zhengce/zhengcefagui/index.html) | 市级和区级政策、申报、认定及项目 |
| 北京市发展改革委 | [政策文件](https://fgw.beijing.gov.cn/fgwzwgk/2024zcwj/) | 产业规划、投资、重大项目和场景 |
| 北京市经济和信息化局 | [通知公告](https://jxj.beijing.gov.cn/jxdt/tzgg/) | 高精尖产业、专精特新、集群和数字化 |
| 北京市科委、中关村管委会 | [项目申报日历](https://kw.beijing.gov.cn/zwgk/zwgksbrl/) | 科技项目、企业认定、应用场景和园区 |
| 北京市中小企业公共服务平台 | [政策服务](https://www.smebj.cn/policy/policy1.html) | 惠企政策、申报活动、服务报告和中小企业动态 |
| 北京市财政局 | [政策文件](https://czj.beijing.gov.cn/zwxx/2024zcwj/index.html) | 财政支持、采购和资金管理 |
| 中国政府采购网 | [采购信息](https://www.ccgp.gov.cn/) | 采购意向、招标、中标和合同 |
| 北京市政府采购网 | [采购信息](https://www.ccgp-beijing.gov.cn/) | 北京政府采购活动 |
| 北京市公共资源交易平台 | [交易平台](https://ggzyfw.beijing.gov.cn/) | 工程、采购及其他公共资源交易 |
| 国家统计局 | [数据发布](https://www.stats.gov.cn/sj/zxfb/index.html) | 全国行业及经济统计 |
| 北京市统计局 | [统计数据](https://tjj.beijing.gov.cn/) | 北京月度、季度、年度及分行业统计 |

## 三、北京市区级政府门户

区政府门户用于补充区级政策、申报通知、认定名单、园区动态和项目落地信息。

| 区域 | 来源入口 | 区域 | 来源入口 |
|---|---|---|---|
| 东城区 | [bjdch.gov.cn](https://www.bjdch.gov.cn/) | 西城区 | [bjxch.gov.cn](https://www.bjxch.gov.cn/) |
| 朝阳区 | [bjchy.gov.cn](https://www.bjchy.gov.cn/) | 丰台区 | [bjft.gov.cn](https://www.bjft.gov.cn/) |
| 石景山区 | [bjsjs.gov.cn](https://www.bjsjs.gov.cn/) | 海淀区 | [bjhd.gov.cn](https://www.bjhd.gov.cn/) |
| 门头沟区 | [bjmtg.gov.cn](https://www.bjmtg.gov.cn/) | 房山区 | [bjfsh.gov.cn](https://www.bjfsh.gov.cn/) |
| 通州区 | [bjtzh.gov.cn](https://www.bjtzh.gov.cn/) | 顺义区 | [bjshy.gov.cn](https://www.bjshy.gov.cn/) |
| 昌平区 | [bjchp.gov.cn](https://www.bjchp.gov.cn/) | 大兴区 | [bjdx.gov.cn](https://www.bjdx.gov.cn/) |
| 怀柔区 | [bjhr.gov.cn](https://www.bjhr.gov.cn/) | 平谷区 | [bjpg.gov.cn](https://www.bjpg.gov.cn/) |
| 密云区 | [bjmy.gov.cn](https://www.bjmy.gov.cn/) | 延庆区 | [bjyq.gov.cn](https://www.bjyq.gov.cn/) |

## 四、产业落地来源

| 来源 | 主要入口 | 重点信号 |
|---|---|---|
| 北京市重大项目办 | [重大项目建设](https://zdb.beijing.gov.cn/zdxmjs/) | 开工、建设、投产和重大工程进展 |
| 北京市投资促进服务中心 | [投资北京](https://invest.beijing.gov.cn/) | 招商、签约、投资项目和企业落地 |
| 北京经济技术开发区 | [政务公开](https://kfqgw.beijing.gov.cn/zwgkkfq/) | 产业政策、申报、名单和项目落地 |
| 北京市商务局 | [通知公告](https://sw.beijing.gov.cn/zwxx/tzgg/) | 商贸服务、消费、外贸、电商和资金申报 |
| 北京市文化和旅游局 | [通知公告](https://whlyj.beijing.gov.cn/zwgk/tzgg/) | 文旅项目、服务消费、活动和扶持政策 |
| 北京市农业农村局 | [通知公告](https://nyncj.beijing.gov.cn/nyj/zwgk/tzgg/) | 都市农业、乡村产业、项目申报和认定名单 |
| 国家标准信息平台 | [标准技术管理司](https://www.samr.gov.cn/bzjss/)、[标准全文公开](https://openstd.samr.gov.cn/bzgk/gb/index) | 标准立项、发布和实施 |
| 中国信息通信研究院 | [权威发布](https://www.caict.ac.cn/kxyj/qwfb/) | ICT 产业研究、白皮书和统计判断 |
| 北京市公共数据开放平台 | [数据目录](https://data.beijing.gov.cn/) | 产业统计、企业和认定名单类数据 |

除首批 P0 来源外，其他行业协会和企业官网不预先批量穷举。系统从已发现的政策、项目、名单和企业实体中识别相关协会或企业，再将其官方网站加入来源注册表；新增后的来源仍需保存来源主体和原始入口，不能把聚合转载页当作原始证据。

### 首批 P0 行业动态来源

| 类别 | 来源 |
|---|---|
| 园区与创业生态 | 中关村发展集团、北京国际科技创新中心园区动态、中关村生命科学园、HICOOL |
| 北京产业组织 | 北京软件和信息服务业协会、北京电子商会、北京医药行业协会、北京机电行业协会、北京先进材料产业促进会 |
| 全国制造业组织 | 中国汽车工业协会、中国机械工业联合会、中国通用机械工业协会、中国医疗器械行业协会、中国电子专用设备工业协会 |
| 市场补充 | 创业邦 |

全国性行业组织的材料默认标记为全国信号；只有正文明确涉及北京企业、项目、产能或区域时，才增加北京影响区域。北京电子商会保留在候选库中，但其官方 HTTPS 证书当前无法通过标准校验，因此暂时禁用自动采集。

## 五、市场补充来源

| 来源 | 主要入口 | 用途 |
|---|---|---|
| 新华网北京频道 | [北京频道](https://www.bj.news.cn/) | 北京产业及重大项目补充信号 |
| 人民网北京频道 | [财经](https://bj.people.com.cn/GB/82839/index.html)、[科技](https://bj.people.com.cn/GB/349239/index.html) | 产业、科技、融资和企业动态 |
| 北京日报/京报网 | [新闻入口](https://news.bjd.com.cn/) | 本地产业及项目落地报道 |
| 北京证券交易所 | [上市公司公告](https://www.bse.cn/disclosure/announcement.html) | 企业融资、投资、产能和重大项目原始披露 |
| 投资界 | [融资与创投资讯](https://www.pedaily.cn/) | 未上市企业融资和新兴产业线索 |

市场媒体只用于发现和交叉验证。能够找到政府、机构、交易所或企业原始披露时，正式证据应指向原始披露。

## 六、搜索发现配置

全网搜索作为长尾发现通道，不替代指定网站采集。当前尚未确定搜索服务供应商，因此配置为：

- `search_enabled: false`；
- `search_provider: null`；
- `enterprise_site_discovery: true`；
- `require_original_source: true`。

后续选定搜索 API 后，只需修改 `config/collection_scope.yaml`。搜索结果进入待抓取 URL 队列，下载后的原始页面按实际发布主体归属 Source；无法回溯原始来源的聚合线索不作为正式证据。

## 七、配置维护方式

修改区域、回采月份或文档格式后执行：

```powershell
uv run track-insight scope-validate
```

新增或修改来源后执行：

```powershell
uv run track-insight source-validate
uv run track-insight source-sync
```

每个来源通过稳定 `code` 识别。修改已有来源时保留 `code`；增加新来源时使用新的 `code`。同步过程会计算配置指纹并保留配置版本。
