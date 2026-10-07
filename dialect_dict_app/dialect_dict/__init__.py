"""方言词典审校与检索系统。

模块划分：
- db           SQLite 模式与连接
- normalize    规范化检索键（简繁/标点/音标组合字符折叠）与偏移映射
- romanization 标音方案无损转换（不可转换时绝不猜读）
- services     词条/义项/例句/关系边/合并拆分/工作流等业务逻辑
- publish      版本化关系闭包发布与缓存
- indexer      异步索引服务（版本守卫，容忍乱序完成）
- search       规范化键 + 原文分字段双索引检索与可解释高亮
- api          HTTP 接口（stdlib http.server）
- seed         演示数据
"""
